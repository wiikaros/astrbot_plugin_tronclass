"""畅课 SSO 登录与 Session 管理。"""

import re
import time
import asyncio
from typing import Optional, Dict, Tuple
from dataclasses import dataclass, field

import aiohttp
from astrbot.api import logger

from ..config import (
    KV_SESSION_PREFIX,
    LOGIN_STATE_TTL_SECONDS,
    ENDPOINT_TODOS,
    ENDPOINT_ROLLCALLS,
)


@dataclass
class LoginState:
    """登录状态机上下文。"""
    step: str = "wait_username"  # wait_username | wait_password | wait_captcha | done
    username: str = ""
    password: str = ""
    captcha_type: str = ""       # "image" | "sms" | ""
    execution: str = ""          # CAS execution ID
    lt_token: str = ""           # CAS login ticket
    login_url: str = ""          # CAS login POST URL
    captcha_url: str = ""        # 验证码图片 URL（图片验证码时）
    expires_at: float = 0.0


@dataclass
class TronClassSession:
    """已登录的畅课会话。"""
    cookies: Dict[str, str] = field(default_factory=dict)
    session_id: str = ""
    role_token: str = ""
    base_url: str = ""
    expires_at: float = 0.0       # 预估过期时间戳
    created_at: float = field(default_factory=time.time)


class TronClassClient:
    """畅课 API 客户端。

    每个用户应使用独立的 TronClassClient 实例，
    通过独立的 aiohttp.ClientSession 维护 Cookie。
    """

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self._session: Optional[aiohttp.ClientSession] = None
        self._tron_session: Optional[TronClassSession] = None

    async def _ensure_session(self):
        """确保 HTTP session 已创建。"""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
            if self._tron_session:
                # 恢复 cookie
                for name, value in self._tron_session.cookies.items():
                    self._session.cookie_jar.update_cookies(
                        {name: value},
                        aiohttp.URL(self.base_url),
                    )

    async def close(self):
        """关闭 HTTP session。"""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    def get_session_data(self) -> Optional[dict]:
        """导出当前 session 数据（用于 KV 存储）。"""
        if self._tron_session is None:
            return None
        return {
            "cookies": self._tron_session.cookies,
            "session_id": self._tron_session.session_id,
            "role_token": self._tron_session.role_token,
            "base_url": self.base_url,
            "expires_at": self._tron_session.expires_at,
            "created_at": self._tron_session.created_at,
        }

    @classmethod
    def from_session_data(cls, data: dict) -> "TronClassClient":
        """从 KV 存储的 session 数据恢复客户端。"""
        client = cls(base_url=data.get("base_url", ""))
        client._tron_session = TronClassSession(
            cookies=data.get("cookies", {}),
            session_id=data.get("session_id", ""),
            role_token=data.get("role_token", ""),
            base_url=data.get("base_url", ""),
            expires_at=data.get("expires_at", 0.0),
            created_at=data.get("created_at", 0.0),
        )
        return client

    @property
    def is_logged_in(self) -> bool:
        """是否已登录。"""
        return self._tron_session is not None and self._tron_session.session_id != ""

    @property
    def is_expired(self) -> bool:
        """Session 是否已过期。"""
        if self._tron_session is None:
            return True
        if self._tron_session.expires_at == 0:
            return False
        return time.time() > self._tron_session.expires_at

    # ========== 登录流程 ==========

    async def login_step_get_login_page(self, username: str, password: str) -> LoginState:
        """登录第一步：获取 CAS 登录页面，提取 token。

        Returns:
            LoginState: 包含下一步操作指引的状态。
        """
        await self._ensure_session()
        state = LoginState(username=username, password=password)

        login_page_url = f"{self.base_url}/login?next=/user/index"

        try:
            async with self._session.get(
                login_page_url,
                allow_redirects=True,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                html = await resp.text()

            # 最终 URL 可能是 CAS 登录页面
            final_url = str(resp.url)

            # 如果直接登录成功了（已有有效 cookie，不常见但可能）
            if "/user/index" in final_url or resp.history and any(
                "/user/index" in str(h.url) for h in resp.history
            ):
                state.step = "done"
                await self._extract_session_from_response(resp)
                return state

            # 提取 CAS 登录表单信息
            lt_match = re.search(r'name="lt"\s+value="([^"]*)"', html)
            execution_match = re.search(r'name="execution"\s+value="([^"]*)"', html)

            if lt_match:
                state.lt_token = lt_match.group(1)
            state.execution = execution_match.group(1) if execution_match else "e1s1"

            # 提取 CAS 登录提交 URL
            form_action = re.search(r'action="([^"]*)"', html)
            if form_action:
                action_url = form_action.group(1)
                if action_url.startswith("/"):
                    # 解析 CAS host
                    cas_url = f"{resp.url.scheme}://{resp.url.host}"
                    if resp.url.port:
                        cas_url += f":{resp.url.port}"
                    state.login_url = cas_url + action_url
                else:
                    state.login_url = action_url
            else:
                state.login_url = str(resp.url)

            # 检测是否已有验证码要求（图片 or 短信）
            state.step = "wait_password"
            logger.info(f"登录状态机：{username} -> wait_password")

        except asyncio.TimeoutError:
            state.step = "error"
            logger.error(f"登录 GET 超时：{login_page_url}")
        except Exception as e:
            state.step = "error"
            logger.error(f"登录 GET 异常：{e}")

        return state

    async def login_step_submit(
        self,
        state: LoginState,
        captcha: str = "",
    ) -> LoginState:
        """登录第二步：提交登录表单（含可选验证码）。

        Args:
            state: 当前的登录状态
            captcha: 验证码（图片或短信），无验证码时为空

        Returns:
            更新后的 LoginState
        """
        await self._ensure_session()

        form_data = {
            "username": state.username,
            "password": state.password,
            "lt": state.lt_token,
            "execution": state.execution,
            "_eventId": "submit",
            "submit": "登录",
        }

        if captcha:
            form_data["captcha"] = captcha

        try:
            async with self._session.post(
                state.login_url,
                data=form_data,
                allow_redirects=True,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                html = await resp.text()
                final_url = str(resp.url)

            # 判断登录结果
            if "/user/index" in final_url or resp.history and any(
                "/user/index" in str(h.url) for h in resp.history
            ):
                # 登录成功
                state.step = "done"
                await self._extract_session_from_response(resp)
                logger.info(f"登录成功：{state.username}")
                return state

            # 登录未成功，检查原因
            if "验证码" in html or "captcha" in html.lower():
                state.step = "wait_captcha"
                if "短信" in html or "sms" in html.lower() or "手机" in html:
                    state.captcha_type = "sms"
                    logger.info(f"需要短信验证码：{state.username}")
                else:
                    state.captcha_type = "image"
                    # 尝试提取图片验证码 URL
                    captcha_img = re.search(r'<img[^>]*src="([^"]*captcha[^"]*)"', html, re.IGNORECASE)
                    if captcha_img:
                        captcha_src = captcha_img.group(1)
                        if captcha_src.startswith("/"):
                            cas_host = resp.url.scheme + "://" + resp.url.host
                            if resp.url.port:
                                cas_host += f":{resp.url.port}"
                            state.captcha_url = cas_host + captcha_src
                        else:
                            state.captcha_url = captcha_src
                    logger.info(f"需要图片验证码：{state.username}")
            elif "密码" in html or "password" in html.lower() or "错误" in html:
                state.step = "error"
                logger.info(f"登录失败（凭据错误）：{state.username}")
            else:
                # 可能有新的验证步骤（如短信验证码页面）
                if "verify" in final_url.lower() or "sms" in final_url.lower() or "mobile" in final_url.lower():
                    state.step = "wait_captcha"
                    state.captcha_type = "sms"
                    logger.info(f"进入短信验证步骤：{state.username}")
                else:
                    state.step = "error"
                    logger.warning(f"登录遇到未识别状态：URL={final_url}")

        except asyncio.TimeoutError:
            state.step = "error"
            logger.error(f"登录 POST 超时：{state.username}")
        except Exception as e:
            state.step = "error"
            logger.error(f"登录 POST 异常：{e}")

        return state

    async def _extract_session_from_response(self, resp: aiohttp.ClientResponse):
        """从 HTTP 响应中提取并保存 session 信息。"""
        cookies = {}
        session_id = ""
        role_token = ""

        # 提取 header 中的 set-cookie
        for name, cookie in resp.cookies.items():
            cookies[name] = cookie.value
            if name.lower() == "session":
                session_id = cookie.value
            elif name.lower() == "role_token":
                role_token = cookie.value

        # 也检查重定向链中的 cookie
        for hist_resp in resp.history:
            for name, cookie in hist_resp.cookies.items():
                if name not in cookies:
                    cookies[name] = cookie.value
                    if name.lower() == "session":
                        session_id = cookie.value
                    elif name.lower() == "role_token":
                        role_token = cookie.value

        # 估算过期时间（role_token 是 JWT，含 exp 字段）
        expires_at = 0.0
        if role_token:
            try:
                import base64
                import json as json_mod
                payload = role_token.split(".")[1]
                # 补齐 padding
                payload += "=" * (4 - len(payload) % 4)
                decoded = json_mod.loads(base64.urlsafe_b64decode(payload))
                if "exp" in decoded:
                    expires_at = float(decoded["exp"])
            except Exception:
                expires_at = time.time() + 3600  # 默认 1 小时

        self._tron_session = TronClassSession(
            cookies=cookies,
            session_id=session_id,
            role_token=role_token,
            base_url=self.base_url,
            expires_at=expires_at,
        )

    # ========== API 请求 ==========

    async def _request(
        self,
        method: str,
        path: str,
        **kwargs,
    ) -> aiohttp.ClientResponse:
        """发送 API 请求，自动携带 session cookie。"""
        await self._ensure_session()

        url = f"{self.base_url}{path}"
        timeout = kwargs.pop("timeout", aiohttp.ClientTimeout(total=30))

        return await self._session.request(
            method, url, timeout=timeout, **kwargs
        )

    async def get_json(self, path: str, **kwargs) -> dict:
        """发送 GET 请求并返回 JSON。"""
        resp = await self._request("GET", path, **kwargs)
        async with resp:
            resp.raise_for_status()
            return await resp.json()

    async def get_todos(self) -> list[dict]:
        """获取待办事项列表（含作业）。"""
        data = await self.get_json(f"{ENDPOINT_TODOS}?no-intercept=true")
        return data.get("todo_list", data.get("results", []))

    async def get_rollcalls(self) -> list[dict]:
        """获取当前点名列表。"""
        data = await self.get_json(f"{ENDPOINT_ROLLCALLS}?api_version=1.1.0")
        return data.get("rollcalls", data.get("results", []))
