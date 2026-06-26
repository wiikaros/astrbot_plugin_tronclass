"""畅课 SSO 登录与 Session 管理。"""

import re
import time
import asyncio
from typing import Optional, Dict
from dataclasses import dataclass, field

import aiohttp
from yarl import URL
from astrbot.api import logger

from ._utils import decode_jwt_expiry
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
    login_url: str = ""          # 当前要 POST 的登录 URL
    captcha_url: str = ""        # 验证码图片 URL（图片验证码时）
    sms_action_url: str = ""     # 短信验证码表单的 action URL
    sms_form_inputs: dict = None # 短信验证页面的 hidden input 字段
    sms_captcha_field: str = ""  # 短信验证码输入框的 name
    sms_trigger_url: str = ""    # 触发发送短信的 API 端点
    expires_at: float = 0.0

    def __post_init__(self):
        if self.sms_form_inputs is None:
            self.sms_form_inputs = {}


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
                        URL(self.base_url),
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
            # 只检查最终的 final_url，不检查 history
            # （history 中含有原始 URL /login?next=/user/index，不是成功标志）
            if "/user/index" in final_url:
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
            redirect_urls = [str(h.url) for h in resp.history]
            logger.info(
                f"登录 POST 完成：final_url={final_url}, "
                f"history={redirect_urls}"
            )

            # 仅检查最终的 final_url，不检查 history
            if "/user/index" in final_url:
                # 登录成功
                state.step = "done"
                await self._extract_session_from_response(resp)
                logger.info(
                    f"登录成功：{state.username}, "
                    f"session_id={self._tron_session.session_id if self._tron_session else 'None'}, "
                    f"cookies={list(self._tron_session.cookies.keys()) if self._tron_session else []}"
                )
                return state

            # 登录未成功，检查原因
            if "验证码" in html or "captcha" in html.lower():
                state.step = "wait_captcha"
                if "短信" in html or "sms" in html.lower() or "手机" in html:
                    state.captcha_type = "sms"
                    logger.info(f"需要短信验证码：{state.username}")
                else:
                    state.captcha_type = "image"
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
            elif "密码" in html or "错误" in html:
                state.step = "error"
                logger.info(f"登录失败（凭据错误）：{state.username}")
            else:
                # 检查是否需要短信/手机验证
                sms_keywords = ["verify", "sms", "mobile", "phone", "验证", "短信", "手机"]
                if any(kw in final_url.lower() for kw in sms_keywords) or \
                   any(kw in html.lower() for kw in sms_keywords):
                    state.step = "wait_captcha"
                    state.captcha_type = "sms"
                    state.sms_action_url, state.sms_form_inputs, state.sms_captcha_field = \
                        self._extract_form_info(html, resp.url)
                    if not state.sms_action_url:
                        state.sms_action_url = final_url
                    state.sms_trigger_url = self._extract_sms_trigger(html, resp.url)
                    logger.info(
                        f"进入短信验证步骤：{state.username}, URL={final_url}, "
                        f"captcha_field={state.sms_captcha_field}, "
                        f"trigger_url={state.sms_trigger_url}"
                    )
                else:
                    state.step = "error"
                    logger.warning(
                        f"登录遇到未识别状态：{state.username}, "
                        f"URL={final_url}, html_len={len(html)}"
                    )

        except asyncio.TimeoutError:
            state.step = "error"
            logger.error(f"登录 POST 超时：{state.username}")
        except Exception as e:
            state.step = "error"
            logger.error(f"登录 POST 异常：{e}")

        return state

    def _extract_form_info(self, html: str, base_url) -> tuple:
        """从 HTML 中提取 form 信息。

        Returns:
            (action_url, hidden_inputs, captcha_field_name)
            - action_url: form 的 action URL
            - hidden_inputs: {name: value} 所有隐藏/预填字段
            - captcha_field_name: 验证码输入框的 name（推测），无则为 ""
        """
        action_url = ""
        hidden_inputs = {}
        captcha_field_name = ""

        # 提取 form action
        form_match = re.search(
            r'<form[^>]*action="([^"]*)"[^>]*>', html, re.IGNORECASE
        )
        if form_match:
            action = form_match.group(1)
            if action.startswith("/"):
                host = f"{base_url.scheme}://{base_url.host}"
                if base_url.port:
                    host += f":{base_url.port}"
                action_url = host + action
            else:
                action_url = action

        # 遍历所有 <input> 标签
        for m in re.finditer(
            r'<input\s+([^>]*)>', html, re.IGNORECASE
        ):
            attrs = m.group(1)
            name_m = re.search(r'name="([^"]*)"', attrs)
            if not name_m:
                continue
            name = name_m.group(1)

            type_m = re.search(r'type="([^"]*)"', attrs)
            input_type = type_m.group(1).lower() if type_m else "text"

            value_m = re.search(r'value="([^"]*)"', attrs)
            value = value_m.group(1) if value_m else ""

            # hidden 类型 → 预填值
            if input_type == "hidden":
                hidden_inputs[name] = value
                continue

            # 密码框跳过（不是验证码）
            if input_type == "password":
                continue

            # 可见输入框（text/num/tel等），如果名含验证码关键词，则是 captcha
            if any(kw in name.lower() for kw in (
                "captcha", "smscode", "verifycode", "code", "验证码", "sms",
            )):
                captcha_field_name = name
            # 如果没有 value 且还没有找到 captcha 字段，可能是验证码输入框
            elif not value and not captcha_field_name:
                captcha_field_name = name

        logger.info(
            f"提取表单：action={action_url}, "
            f"hidden_fields={list(hidden_inputs.keys())}, "
            f"captcha_field={captcha_field_name}"
        )

        return action_url, hidden_inputs, captcha_field_name

    def _extract_sms_trigger(self, html: str, base_url) -> str:
        """从 HTML 的 JS 中提取发送短信验证码的 API 端点。

        匹配常见模式：
        - sendDynamicCodeByPhone / getDynamicCode 等函数中的 URL
        - /cas/sendSms, /authserver/sendDynamicCode 等路径
        """
        host = f"{base_url.scheme}://{base_url.host}"
        if base_url.port:
            host += f":{base_url.port}"

        # 尝试从 <script> 中提取 URL
        patterns = [
            # jQuery: $.post("/path", ...) or $.get("/path", ...)
            r'''['"]((?:https?:)?//[^'"]*send\w*(?:dynamic|sms|code|phone)[^'"]*|/(?:cas/|authserver/)?\w*send\w*(?:dynamic|sms|code|phone)[^'"]*)['"]''',
            # 直接路径
            r'''url:\s*['"]((?:/cas/|/authserver/)?\w*(?:send|sms|dynamic|code|phone)\w*[^'"]*)['"]''',
            # fetch 调用
            r'''fetch\(['"]((?:/cas/|/authserver/)?\w*(?:send|sms|dynamic|code|phone)\w*[^'"]*)['"]''',
        ]
        for pattern in patterns:
            match = re.search(pattern, html, re.IGNORECASE)
            if match:
                url = match.group(1)
                if url.startswith("/"):
                    return host + url
                if url.startswith("//"):
                    return f"{base_url.scheme}:{url}"
                return url

        # 通过 onclick 函数名反推 — CUC 的 sendDynamicCodeByPhone
        # 常见 CAS 短信端点
        guesses = [
            "/authserver/sendDynamicCode",
            "/cas/sendDynamicCode",
            "/authserver/sendSms",
            "/cas/sendSms",
        ]
        for guess in guesses:
            # 在 JS 中检查是否有匹配的函数
            if re.search(
                r'(?:sendDynamicCode|sendSms|getDynamicCode)\s*\(',
                html, re.IGNORECASE
            ):
                return host + guess

        return ""

    async def login_step_trigger_sms(self, state: LoginState) -> bool:
        """触发发送短信验证码（模拟点击"获取验证码"按钮）。

        Returns:
            是否成功触发。
        """
        if not state.sms_trigger_url:
            logger.warning("未找到短信触发 URL，跳过")
            return False

        await self._ensure_session()

        try:
            async with self._session.post(
                state.sms_trigger_url,
                data=state.sms_form_inputs,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                text = await resp.text()
                logger.info(
                    f"触发短信发送：url={state.sms_trigger_url}, "
                    f"status={resp.status}, resp_len={len(text)}"
                )
                # 通常返回 JSON: {"success": true} 或类似
                return resp.status == 200
        except Exception as e:
            logger.error(f"触发短信发送失败：{e}")
            return False

    async def login_step_submit_sms(
        self, state: LoginState, sms_code: str
    ) -> LoginState:
        """提交短信验证码。

        Args:
            state: 当前登录状态（含 SMS 表单信息）。
            sms_code: 用户输入的短信验证码。

        Returns:
            更新后的 LoginState。
        """
        await self._ensure_session()

        form_data = dict(state.sms_form_inputs)
        code_field = state.sms_captcha_field or "captcha"
        form_data[code_field] = sms_code

        post_url = state.sms_action_url or state.login_url

        try:
            async with self._session.post(
                post_url,
                data=form_data,
                allow_redirects=True,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                html = await resp.text()
                final_url = str(resp.url)

            redirect_urls = [str(h.url) for h in resp.history]
            logger.info(
                f"SMS 提交完成：final_url={final_url}, history={redirect_urls}"
            )

            if "/user/index" in final_url:
                state.step = "done"
                await self._extract_session_from_response(resp)
                logger.info(
                    f"SMS 验证成功：{state.username}, "
                    f"session_id={self._tron_session.session_id if self._tron_session else 'None'}"
                )
                return state

            if "错误" in html or "error" in html.lower() or "无效" in html:
                state.step = "wait_captcha"
                logger.info(f"SMS 验证码错误：{state.username}")
                return state

            # 可能还需要更多验证
            sms_keywords = ["verify", "sms", "mobile", "验证", "短信"]
            if any(kw in final_url.lower() for kw in sms_keywords) or \
               any(kw in html.lower() for kw in sms_keywords):
                state.step = "wait_captcha"
                state.sms_action_url, state.sms_form_inputs, state.sms_captcha_field = \
                    self._extract_form_info(html, resp.url)
                if not state.sms_action_url:
                    state.sms_action_url = final_url
                logger.info(f"仍需短信验证：{state.username}")
                return state

            state.step = "error"
            logger.warning(f"SMS 提交后未识别状态：URL={final_url}")

        except asyncio.TimeoutError:
            state.step = "error"
            logger.error(f"SMS 提交超时：{state.username}")
        except Exception as e:
            state.step = "error"
            logger.error(f"SMS 提交异常：{e}")

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
        expires_at = decode_jwt_expiry(role_token) if role_token else 0.0

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
        """发送 API 请求，自动携带 session cookie 和 x-session-id 头。

        自动检测 302 重定向到登录页 → 标记 session 过期。
        """
        await self._ensure_session()

        url = f"{self.base_url}{path}"
        timeout = kwargs.pop("timeout", aiohttp.ClientTimeout(total=30))

        headers = kwargs.pop("headers", {})
        if self._tron_session and self._tron_session.session_id:
            headers["x-session-id"] = self._tron_session.session_id
        headers.setdefault(
            "User-Agent",
            "Mozilla/5.0 (Linux; Android 12; wv) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Version/4.0 Chrome/110.0.5481.154 Mobile Safari/537.36 "
            "TronClass/common",
        )

        resp = await self._session.request(
            method, url, timeout=timeout, headers=headers, **kwargs
        )

        # 检测响应是否被重定向到了登录页（session 过期）
        if resp.status in (301, 302, 303):
            loc = resp.headers.get("Location", "")
            if any(kw in loc for kw in ("/login", "/sso", "/auth", "identity")):
                logger.warning(f"Session 已过期（API 重定向到登录页：{loc[:80]}）")
                if self._tron_session:
                    self._tron_session.expires_at = 0  # 强制标记过期

        return resp

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

    async def get_homework_activities(self, course_id: int) -> list[dict]:
        """获取指定课程的作业活动列表（含截止时间）。"""
        data = await self.get_json(
            f"/api/courses/{course_id}/homework-activities"
        )
        return data.get("homework_activities", data.get("results", []))


async def check_session_valid(client: TronClassClient) -> bool:
    """检查 TronClassClient 的 session 是否仍然有效。

    先检查 is_expired，再发一个轻量 API 请求验证。
    返回 True 表示有效，False 表示应重新登录。

    Args:
        client: 已创建的 TronClassClient 实例。
    """
    if client.is_expired:
        return False

    try:
        # 轻量验证：HEAD /user/index，只需检查是否被重定向
        resp = await client._request("HEAD", "/user/index", allow_redirects=False)
        async with resp:
            if resp.status in (301, 302):
                loc = resp.headers.get("Location", "")
                if "login" in loc or "sso" in loc:
                    return False
        return True
    except Exception:
        # 网络异常时保守处理：不标记过期
        return True
