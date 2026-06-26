"""微信扫码登录 — 基于 CAS combinedLogin 的完整流程。

独立于 TronClassClient（密码登录），提供：
1. CAS session 初始化
2. 获取 WeChat QR 二维码
3. 轮询扫码状态
4. 回调 CAS 获取 TronClass session
"""

import re
import asyncio
from typing import Optional
from urllib.parse import urlparse, parse_qs, quote

import aiohttp
from astrbot.api import logger

from ._utils import decode_jwt_expiry
from ..config import (
    WECHAT_POLL_URL,
    WECHAT_POLL_INTERVAL,
    WECHAT_POLL_TIMEOUT,
)


class WeChatLoginFlow:
    """微信扫码登录流程。每个用户实例化一个。"""

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self._session: Optional[aiohttp.ClientSession] = None
        self._sso_host: str = ""

    async def _ensure_session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    @staticmethod
    def _extract_cookies(resp: aiohttp.ClientResponse) -> dict:
        """从响应中提取所有 cookie。"""
        cookies = {}
        for hist in resp.history:
            for name, ck in hist.cookies.items():
                cookies[name] = ck.value
        for name, ck in resp.cookies.items():
            cookies[name] = ck.value
        return cookies

    # ======== Step 1: 初始化 CAS session ========

    async def step1_init_cas_session(self) -> Optional[str]:
        """GET TronClass → 跟重定向到 CAS → 获取 JSESSIONID 和 service URL。

        Returns:
            service 参数（TronClass 回调 URL），失败返回 None。
        """
        await self._ensure_session()
        url = f"{self.base_url}/login?next=/user/index"

        try:
            async with self._session.get(
                url, allow_redirects=True, timeout=aiohttp.ClientTimeout(15)
            ) as resp:
                cas_url = str(resp.url)
        except Exception as e:
            logger.error(f"[微信登录] 初始化 CAS session 失败: {e}")
            return None

        # 解析 SSO host
        parsed = urlparse(cas_url)
        self._sso_host = f"{parsed.scheme}://{parsed.hostname}"
        if parsed.port:
            self._sso_host += f":{parsed.port}"

        # 提取 service 参数
        service = parse_qs(parsed.query).get("service", [""])[0]
        logger.info(f"[微信登录] SSO={self._sso_host}, service={service[:80]}...")
        return service

    # ======== Step 2: 获取 WeChat QR ========

    async def step2_get_wechat_qr(self, service: str) -> Optional[dict]:
        """通过 CAS combinedLogin.do 获取 WeChat QR 信息。

        Returns:
            {"uuid": str, "qr_url": str, "state": str}，失败返回 None。
        """
        await self._ensure_session()

        combined_url = (
            f"{self._sso_host}/authserver/combinedLogin.do"
            f"?type=weixin&success={quote(service, safe='')}"
        )

        try:
            # 不跟跳，只取 CAS 返回的 WeChat URL
            async with self._session.get(
                combined_url, allow_redirects=False, timeout=aiohttp.ClientTimeout(15)
            ) as resp:
                wechat_url = resp.headers.get("Location", "")

            if not wechat_url or resp.status not in (301, 302, 303):
                logger.error(f"[微信登录] combinedLogin 未返回重定向: {resp.status}")
                return None

            # 从 WeChat URL 提取 state
            parsed = urlparse(wechat_url)
            qs = parse_qs(parsed.query)
            wechat_state = qs.get("state", [""])[0]
            logger.info(f"[微信登录] CAS state={wechat_state}")

            # 请求 WeChat QR 页面提取 UUID
            async with self._session.get(
                wechat_url, allow_redirects=True, timeout=aiohttp.ClientTimeout(15)
            ) as resp:
                html = await resp.text()

            uuid_m = re.search(r'/connect/qrcode/([^"&\s]+)', html)
            if not uuid_m:
                logger.error("[微信登录] 无法从 WeChat 页面提取 UUID")
                return None

            uuid = uuid_m.group(1)
            qr_url = f"https://open.weixin.qq.com/connect/qrcode/{uuid}"
            logger.info(f"[微信登录] UUID={uuid}")

            return {"uuid": uuid, "qr_url": qr_url, "state": wechat_state}

        except Exception as e:
            logger.error(f"[微信登录] 获取 QR 失败: {e}")
            return None

    # ======== Step 3: 轮询扫码 ========

    async def step3_poll_scan(self, uuid: str) -> Optional[str]:
        """轮询 WeChat 服务器等待用户扫码。

        Generator: 每次轮询 yield 当前的轮询次数（用于 main.py 的 while 循环）。

        Returns:
            wx_code（扫码成功）或 None（超时）。
        """
        poll_url = WECHAT_POLL_URL.format(uuid=uuid)
        max_retries = WECHAT_POLL_TIMEOUT // WECHAT_POLL_INTERVAL

        for i in range(max_retries):
            await asyncio.sleep(WECHAT_POLL_INTERVAL)

            try:
                async with self._session.get(
                    poll_url, timeout=aiohttp.ClientTimeout(10)
                ) as resp:
                    text = await resp.text()

                ec = re.search(r"window\.wx_errcode=(\d+)", text)
                wc = re.search(r"window\.wx_code='([^']*)'", text)
                errcode = int(ec.group(1)) if ec else 0
                wx_code = wc.group(1) if wc else ""

                if errcode == 405 and wx_code:
                    logger.info(f"[微信登录] 扫码成功，wx_code={wx_code[:10]}...")
                    return wx_code
                elif errcode == 408:
                    logger.warning("[微信登录] QR 码已过期")
                    return None

            except Exception as e:
                logger.warning(f"[微信登录] 轮询异常 [{i}]: {e}")
                # 继续重试

        logger.warning("[微信登录] 轮询超时")
        return None

    # ======== Step 4: 回调 CAS 获取 TronClass session ========

    async def step4_callback_and_get_session(
        self, wx_code: str, wechat_state: str
    ) -> Optional[dict]:
        """回调 CAS，跟随重定向到 TronClass，提取 session cookies。

        Returns:
            session 数据字典 (cookies, session_id, role_token, ...)，失败返回 None。
        """
        await self._ensure_session()

        callback_url = (
            f"{self._sso_host}/authserver/callback"
            f"?code={wx_code}&state={wechat_state}"
        )

        try:
            async with self._session.get(
                callback_url, allow_redirects=True, timeout=aiohttp.ClientTimeout(15)
            ) as resp:
                final_url = str(resp.url)

            # 检查是否到了 TronClass
            base_host = urlparse(self.base_url).hostname or ""
            if base_host not in final_url:
                async with self._session.get(
                    final_url, allow_redirects=True, timeout=aiohttp.ClientTimeout(15)
                ) as resp2:
                    final_url = str(resp2.url)

            if base_host not in final_url:
                logger.error(f"[微信登录] 未到达 TronClass: {final_url[:100]}")
                return None

            # 提取 session cookies
            cookies = {}
            for cookie in self._session.cookie_jar:
                cookies[cookie.key] = cookie.value

            session_id = cookies.get("session", "")
            role_token = cookies.get("role_token", "")

            expires_at = decode_jwt_expiry(role_token) if role_token else 0.0

            session_data = {
                "cookies": cookies,
                "session_id": session_id,
                "role_token": role_token,
                "base_url": self.base_url,
                "expires_at": expires_at,
            }
            logger.info(f"[微信登录] ✅ 成功，session_id={session_id[:30]}...")
            return session_data

        except Exception as e:
            logger.error(f"[微信登录] 回调失败: {e}")
            return None
