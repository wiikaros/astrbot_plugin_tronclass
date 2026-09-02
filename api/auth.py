"""畅课 SSO 登录与 Session 管理。

登录链路（与真实抓包对齐，见 示例数据包/登录过程/）：
  1. TronClass (/login?next=/user/index) → CAS 登录页（sso.cuc.edu.cn）
  2. 从登录页提取 pwdEncryptSalt / lt / execution（salt 每次动态下发）
  3. checkNeedCaptcha 检测滑块验证码（isNeed=true 时无法自动化）
  4. AES 加密密码并 POST（字段含 cllt=userNameLogin / dllt=generalLogin）
  5. 响应 302 → reAuthCheck/reAuthLoginView.do?isMultifactor=true（强制 MFA）
  6. 触发短信（getDynamicCodeByReauth.do）→ 用户输入短信码
  7. 提交短信（reAuthSubmit.do）→ 带 CASTGC 回到 CAS → identity broker → TronClass
  8. TronClass 设置 session / role_token cookie（courses 域）
"""

import re
import json
import html
import time
import base64
import secrets
import asyncio
from typing import Optional, Dict
from dataclasses import dataclass, field
from urllib.parse import quote, urljoin

import aiohttp
from yarl import URL
from astrbot.api import logger

from ._utils import decode_jwt_expiry, filter_cookies_for_base
from ..config import (
    ENDPOINT_TODOS,
    ENDPOINT_ROLLCALLS,
    SSO_HOST,
)


# ========== 异常 ==========

class SessionInvalidError(Exception):
    """会话无效或被重定向到登录页。"""


class LoginCancelledError(Exception):
    """登录流程需要用户介入或不可继续（滑块验证码等）。"""


# ========== 密码加密（与 CUC encrypt.js 一致） ==========

# CUC encrypt.js 的 $aes_chars（排除易混淆字符 I/L/O/u/v/0/1/9）
AES_CHARS = "ABCDEFGHJKMNPQRSTWXYZabcdefhijkmnprstwxyz2345678"


def _random_string(n: int) -> str:
    """与 CUC encrypt.js randomString 同字符集的随机串。"""
    return "".join(secrets.choice(AES_CHARS) for _ in range(n))


def _cookie_value_ci(cookies: dict, name: str, default: str = "") -> str:
    """大小写不敏感地从 cookie 字典取值（cookie 名理论上区分大小写）。

    服务器固定下发小写 `session`/`role_token`，此处兼容任意大小写防御变化。
    """
    for k, v in cookies.items():
        if k.lower() == name.lower():
            return v
    return default


def encrypt_password(password: str, salt: str) -> str:
    """模拟 CUC CAS encryptPassword（encrypt.js 真实算法）。

    明文 = randomString(64) + password；key = salt(Utf8)；iv = randomString(16)；
    AES-CBC + PKCS7，纯密文 base64 输出（无 "Salted__" 前缀）。
    每次加密结果随机（64 前缀 + iv 均随机）。

    缺失 pycryptodome 时 raise RuntimeError（绝不回退明文）。
    """
    try:
        from Crypto.Cipher import AES as AESCipher
        from Crypto.Util.Padding import pad
    except ImportError as e:
        raise RuntimeError(
            "缺少 pycryptodome 依赖，无法加密密码。"
            "请安装 pycryptodome 或改用 /微信登录。"
        ) from e
    if not salt:
        raise ValueError("pwdEncryptSalt 为空，无法加密密码")
    key = salt.encode("utf-8")
    iv = _random_string(16).encode("utf-8")
    plaintext = (_random_string(64) + password).encode("utf-8")
    cipher = AESCipher.new(key, AESCipher.MODE_CBC, iv)
    return base64.b64encode(cipher.encrypt(pad(plaintext, 16))).decode("utf-8")


@dataclass
class LoginState:
    """登录状态机上下文（CUC CAS + MFA 短信）。

    step 取值：wait_username | wait_password | wait_mfa_sms |
               need_slider_captcha | done | error
    """
    step: str = "wait_username"
    username: str = ""
    password: str = ""          # 仅内存暂存；进入 wait_mfa_sms 前从 KV 清除
    # ---- CAS 登录页提取 ----
    cas_url: str = ""           # CAS login URL（含 service 参数），POST 目标
    sso_host: str = ""          # CAS host（如 https://sso.cuc.edu.cn）
    pwd_salt: str = ""          # pwdEncryptSalt（AES key）
    lt_token: str = ""          # lt（真实抓包为空串）
    execution: str = ""         # execution（真实抓包为 e3s1）
    # ---- MFA ----
    mfa_url: str = ""           # reAuthLoginView.do 页面 URL
    mfa_service: str = ""       # MFA 页提取的 service（broker endpoint）
    sms_sent: bool = False      # 短信是否已触发成功
    # ---- 结果 ----
    error_msg: str = ""         # 友好错误信息
    expires_at: float = 0.0


@dataclass
class TronClassSession:
    """已登录的畅课会话。

    cookies 仅包含“会随 base_url 发送”的业务域 cookie（见 filter_cookies_for_base），
    不再混入 sso/identity 等无关域条目。
    """
    cookies: Dict[str, str] = field(default_factory=dict)
    session_id: str = ""
    role_token: str = ""
    base_url: str = ""
    expires_at: float = 0.0       # 预估过期时间戳
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)  # 最近一次凭证刷新/同步时刻


class TronClassClient:
    """畅课 API 客户端。

    每个用户应使用独立的 TronClassClient 实例，
    通过独立的 aiohttp.ClientSession 维护 Cookie。
    """

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self._session: Optional[aiohttp.ClientSession] = None
        self._tron_session: Optional[TronClassSession] = None
        # 会话凭证写回回调（由创建方注入，如 storage.save_session 闭包）
        self._writeback: Optional[callable] = None
        self._last_persist_at: float = 0.0

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

    def attach_session_persister(self, callback: callable) -> None:
        """注入会话凭证写回回调。

        回调签名为 async (data: dict) -> None，data 为 get_session_data() 输出。
        每次响应带新 cookie / 新 x-session-id（会话被服务器滚动续期）时，
        经节流后调用，用于把最新凭证持久化回 KV。
        """
        self._writeback = callback

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
            "updated_at": self._tron_session.updated_at,
        }

    @classmethod
    def from_session_data(cls, data: dict) -> "TronClassClient":
        """从 KV 存储的 session 数据恢复客户端。"""
        client = cls(base_url=data.get("base_url", ""))
        created_at = data.get("created_at", 0.0)
        client._tron_session = TronClassSession(
            cookies=data.get("cookies", {}),
            session_id=data.get("session_id", ""),
            role_token=data.get("role_token", ""),
            base_url=data.get("base_url", ""),
            expires_at=data.get("expires_at", 0.0),
            created_at=created_at,
            updated_at=data.get("updated_at", created_at or time.time()),
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

    # ========== 工具 ==========

    @staticmethod
    def _abs_url(loc: str, base: URL) -> str:
        """把 Location 转绝对 URL（urljoin 正确处理相对路径）。

        例如相对路径 `reAuthCheck/reAuthLoginView.do` 相对
        `https://sso.cuc.edu.cn/authserver/login` 解析为
        `https://sso.cuc.edu.cn/authserver/reAuthCheck/reAuthLoginView.do`。
        """
        return urljoin(str(base), loc)

    @staticmethod
    def _extract_input_value(html_text: str, attr_value: str) -> str:
        """定位属性值恰为 attr_value 的 <input> 标签并提取其 value。

        兼容 id/name 属性、属性顺序、单双引号。例如真实登录页：
          <input type="hidden" id="pwdEncryptSalt" value="926k..." />
        注意按属性"精确值"匹配，避免误命中子串（如 "lt" 不会命中 "cllt"）。
        """
        m = re.search(
            r'<input\b[^>]*(?:id|name)\s*=\s*["\']'
            + re.escape(attr_value)
            + r'["\'][^>]*>',
            html_text,
            re.IGNORECASE,
        )
        if not m:
            return ""
        v = re.search(
            r'value\s*=\s*["\']([^"\']*)["\']',
            m.group(0),
            re.IGNORECASE,
        )
        return v.group(1) if v else ""

    def _extract_login_error(self, html_text: str) -> str:
        """从登录失败页面提取友好错误文案。"""
        if not html_text:
            return "登录失败（未知原因）"
        for pat in (
            r'<span[^>]*class="[^"]*error[^"]*"[^>]*>(.*?)</span>',
            r'<div[^>]*class="[^"]*msg[^"]*"[^>]*>(.*?)</div>',
            r'"errorMsg"\s*:\s*"([^"]*)"',
            r'<p[^>]*class="[^"]*(?:error|msg)[^"]*"[^>]*>(.*?)</p>',
        ):
            m = re.search(pat, html_text, re.S)
            if m:
                txt = re.sub(r"<[^>]+>", "", m.group(1)).strip()
                if txt:
                    return txt[:100]
        if "密码" in html_text and ("错误" in html_text or "不正确" in html_text):
            return "用户名或密码错误"
        if "验证码" in html_text and ("错误" in html_text or "不正确" in html_text):
            return "验证码错误"
        return "登录失败（服务器未返回明确原因）"

    # ========== 登录流程 ==========

    async def login_with_password(self, username: str, password: str) -> LoginState:
        """账号密码登录主流程（一个函数内同步完成，不跨用户输入）。

        步骤：GET /login?next=/user/index 跟跳至 CAS 登录页
            → 提取 pwdEncryptSalt / lt / execution
            → checkNeedCaptcha 检测（isNeed=true → need_slider_captcha）
            → AES 加密密码并 POST（含 cllt/dllt/captcha 字段）
            → 判定：/user/index→done | reAuth/isMultifactor→wait_mfa_sms（自动触发短信）
                    | 其余→error（提取错误文案）
        """
        state = LoginState(username=username, password=password)
        await self._ensure_session()

        try:
            # 1) TronClass → CAS 登录页（跟随重定向）
            start_url = f"{self.base_url}/login?next=/user/index"
            async with self._session.get(
                start_url,
                allow_redirects=True,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                html_text = await resp.text()
                final_url = str(resp.url)

            # 罕见：已有有效 cookie 直达主页
            if "/user/index" in final_url:
                state.step = "done"
                await self._extract_session_from_response()
                return state

            cas_url = URL(final_url)
            sso_host = f"{cas_url.scheme}://{cas_url.host}"
            if cas_url.port:
                sso_host += f":{cas_url.port}"
            state.cas_url = final_url
            state.sso_host = sso_host

            # 2) 提取登录表单参数（salt 每次动态下发，不可硬编码）
            #    真实页面是 id="pwdEncryptSalt"（非 name），须兼容 id/name
            state.pwd_salt = self._extract_input_value(html_text, "pwdEncryptSalt")
            state.lt_token = self._extract_input_value(html_text, "lt")
            state.execution = self._extract_input_value(html_text, "execution") or "e1s1"

            if not state.pwd_salt:
                # 记录诊断信息（不含敏感内容），便于区分"未到登录页"与"页面结构变化"
                logger.warning(
                    f"无法提取 pwdEncryptSalt：final_url={final_url[:120]}, "
                    f"html_len={len(html_text)}"
                )
                state.step = "error"
                state.error_msg = (
                    "无法获取加密盐（pwdEncryptSalt），登录页可能已变更。"
                )
                return state

            # 3) 滑块验证码检测（无法自动化 → 引导改用微信登录）
            if await self._check_need_captcha(username, cas_url):
                state.step = "need_slider_captcha"
                state.error_msg = "该账号需要滑块验证码，无法自动完成登录。"
                return state

            # 4) AES 加密密码并 POST（字段与抓包 [117] 一致）
            form_data = {
                "username": username,
                "password": encrypt_password(password, state.pwd_salt),
                "captcha": "",
                "_eventId": "submit",
                "cllt": "userNameLogin",
                "dllt": "generalLogin",
                "lt": state.lt_token,
                "execution": state.execution,
            }
            async with self._session.post(
                state.cas_url,
                data=form_data,
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                status = resp.status
                loc = resp.headers.get("Location", "")
                if status not in (301, 302, 303, 307, 308):
                    html_text = await resp.text()
                else:
                    html_text = ""

            # 5) 判定结果
            if status in (301, 302, 303, 307, 308):
                if "isMultifactor" in loc or "reAuth" in loc:
                    # 进入 MFA 二次认证（抓包 [118]）
                    mfa_page_url = self._abs_url(loc, cas_url)
                    state.mfa_url = mfa_page_url
                    async with self._session.get(
                        mfa_page_url,
                        allow_redirects=True,
                        timeout=aiohttp.ClientTimeout(total=15),
                    ) as resp2:
                        mfa_html = await resp2.text()
                    cfg = self._extract_mfa_config(mfa_html)
                    state.mfa_service = cfg.get("mfa_service", "")
                    if not state.mfa_service:
                        state.step = "error"
                        state.error_msg = "无法提取 MFA 配置，登录流程可能已变更。"
                        return state
                    state.step = "wait_mfa_sms"
                    state.sms_sent = await self._trigger_mfa_sms(username, sso_host)
                    return state

                if "/user/index" in loc:
                    # 无 MFA，直接跟到完成
                    final_url2, _ = await self._follow_to_final(
                        self._abs_url(loc, cas_url)
                    )
                    if "/user/index" in final_url2:
                        state.step = "done"
                        await self._extract_session_from_response()
                    else:
                        state.step = "error"
                        state.error_msg = f"登录后跳转未完成（{final_url2[:80]}）"
                    return state

                state.step = "error"
                state.error_msg = "登录流程异常（服务器返回未预期的重定向）。"
                return state

            # 6) 非 302：留在登录页 → 提取错误文案
            state.step = "error"
            state.error_msg = self._extract_login_error(html_text)
            return state

        except asyncio.TimeoutError:
            state.step = "error"
            state.error_msg = "请求超时，请稍后重试。"
        except Exception as e:
            state.step = "error"
            logger.error(f"登录异常：{e}")
            state.error_msg = f"登录过程出现异常：{type(e).__name__}"

        return state

    async def login_submit_mfa_sms(self, state: LoginState, sms_code: str) -> LoginState:
        """提交 MFA 短信码 → 带 CASTGC 回到 CAS 完成最终跳转 → 提取 session。

        步骤：POST /authserver/reAuthCheck/reAuthSubmit.do（抓包 [138]）
            → 响应含 "reAuth_success" → GET {sso_host}/authserver/login?service={broker}
            → 跟跳到 courses 落 session cookie → /user/index → done
            → 验证码错误 → wait_mfa_sms（保留状态供重试）
        """
        await self._ensure_session()

        try:
            sso_host = state.sso_host or SSO_HOST
            submit_url = f"{sso_host}/authserver/reAuthCheck/reAuthSubmit.do"
            body = {
                "service": state.mfa_service,
                "reAuthType": "3",
                "isMultifactor": "true",
                "password": "",
                "dynamicCode": sms_code,
                "uuid": "",
                "answer1": "",
                "answer2": "",
                "otpCode": "",
                "skipTmpReAuth": "false",
            }
            headers = {
                "x-requested-with": "XMLHttpRequest",
                "content-type": "application/x-www-form-urlencoded;charset=UTF-8",
                # 抓包 [138] 携带 referer（MFA 页面 URL），部分部署会校验来源
                "referer": state.mfa_url or f"{sso_host}/authserver/reAuthCheck/reAuthLoginView.do",
            }
            async with self._session.post(
                submit_url,
                data=body,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                status = resp.status
                text = await resp.text()

            if "reAuth_success" not in text:
                # 记录诊断（响应不含验证码，无敏感信息）
                logger.warning(
                    f"MFA 短信提交未成功：status={status}, "
                    f"resp_len={len(text)}, resp_head={text[:200]!r}"
                )
                if "验证码" in text or "error" in text.lower() or "错误" in text:
                    state.step = "wait_mfa_sms"  # 验证码错误，可重试
                    return state
                state.step = "error"
                state.error_msg = "短信验证提交失败，请重试。"
                return state

            # 带 CASTGC 回到 CAS login 完成最终跳转（抓包 [140] → [142] → [143]）
            cas_login_url = f"{sso_host}/authserver/login?service={quote(state.mfa_service, safe='')}"
            final_url, _ = await self._follow_to_final(cas_login_url)

            if "/user/index" in final_url:
                state.step = "done"
                await self._extract_session_from_response()
            else:
                state.step = "error"
                state.error_msg = f"MFA 后跳转未完成（{final_url[:80]}）"

        except asyncio.TimeoutError:
            state.step = "error"
            state.error_msg = "请求超时，请稍后重试。"
        except Exception as e:
            state.step = "error"
            logger.error(f"MFA 短信提交异常：{e}")
            state.error_msg = f"MFA 提交异常：{type(e).__name__}"

        return state

    async def _check_need_captcha(self, username: str, cas_url: URL) -> bool:
        """GET /authserver/checkNeedCaptcha.htl，返回是否需滑块验证码。"""
        try:
            check_url = self._abs_url(
                f"/authserver/checkNeedCaptcha.htl?username={quote(username)}&_={int(time.time() * 1000)}",
                cas_url,
            )
            async with self._session.get(
                check_url, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                text = await resp.text()
            data = json.loads(text)
            return bool(data.get("isNeed"))
        except Exception:
            return False  # 检测失败按无需处理

    async def _trigger_mfa_sms(self, username: str, sso_host: str) -> bool:
        """POST /authserver/dynamicCode/getDynamicCodeByReauth.do 触发短信（抓包 [137]）。"""
        try:
            url = f"{sso_host}/authserver/dynamicCode/getDynamicCodeByReauth.do"
            headers = {
                "x-requested-with": "XMLHttpRequest",
                "content-type": "application/x-www-form-urlencoded;charset=UTF-8",
            }
            body = {
                "userName": username,
                "authCodeTypeName": "reAuthDynamicCodeType",
            }
            async with self._session.post(
                url, data=body, headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                text = await resp.text()
            return '"res":"success"' in text or '"success"' in text
        except Exception:
            return False

    def _extract_mfa_config(self, html_text: str) -> dict:
        """从 MFA 页 HTML 提取 {mfa_service, reauth_type, reauth_user_id}。

        注意：MFA 页的 service 在 JS 配置中，且是 JSON 转义形式
        （如 `"service":"https:\\\\/\\\\/identity...\\\\/..."`），
        必须还原 `\\/` → `/`，否则提交 reAuthSubmit.do 时服务器无法解析。
        """
        cfg = {}
        svc_m = re.search(r'name="service"\s+value="([^"]*)"', html_text)
        if svc_m:
            cfg["mfa_service"] = html.unescape(svc_m.group(1)).replace("\\/", "/")
        else:
            js_m = re.search(r'"service"\s*:\s*"([^"]*)"', html_text)
            if js_m:
                # JSON 转义还原：\/ → /，再处理 HTML 实体（&amp; 等）
                cfg["mfa_service"] = html.unescape(
                    js_m.group(1).replace("\\/", "/")
                )
            else:
                cfg["mfa_service"] = ""
        rt_m = re.search(r'"reAuthType"\s*:\s*"?(\d+)"?', html_text)
        cfg["reauth_type"] = rt_m.group(1) if rt_m else "3"
        ru_m = re.search(r'"reAuthUserId"\s*:\s*"([^"]*)"', html_text)
        cfg["reauth_user_id"] = ru_m.group(1) if ru_m else ""
        return cfg

    async def _follow_to_final(
        self, start_url: str, max_hops: int = 10
    ) -> tuple:
        """手动逐跳跟重定向（最多 max_hops 跳），cookie 由 CookieJar 自动收集。

        Returns:
            (final_url, html)
        """
        url = start_url
        for _ in range(max_hops):
            async with self._session.get(
                url,
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                loc = resp.headers.get("Location", "")
                if resp.status in (301, 302, 303, 307, 308) and loc:
                    url = self._abs_url(loc, resp.url)
                    continue
                html_text = await resp.text()
                return str(resp.url), html_text
        return url, ""

    async def _extract_session_from_response(self):
        """从 aiohttp CookieJar 提取业务域 cookie，并取 session/role_token。

        仅保留会随 base_url 发送的 cookie（filter_cookies_for_base），
        避免把 sso/identity 域 cookie 平铺后错配到业务域。
        """
        cookies = filter_cookies_for_base(self._session.cookie_jar, self.base_url)
        session_id = _cookie_value_ci(cookies, "session")
        role_token = _cookie_value_ci(cookies, "role_token")

        # 估算过期时间（role_token 是 JWT，含 exp 字段）
        expires_at = decode_jwt_expiry(role_token) if role_token else 0.0

        self._tron_session = TronClassSession(
            cookies=cookies,
            session_id=session_id,
            role_token=role_token,
            base_url=self.base_url,
            expires_at=expires_at,
        )

    # ========== 会话凭证滚动续期（响应 Set-Cookie / x-session-id 消费） ==========

    SESSION_WRITEBACK_MIN_INTERVAL = 60.0  # 持久化节流：两次写回最小间隔（秒）

    def _apply_response_credentials(self, resp: aiohttp.ClientResponse) -> bool:
        """消费一次响应中的会话续期凭证，同步到内存快照。

        服务器（courses 域）在几乎每个响应中滚动重签 session/role_token cookie，
        并回传同值的 x-session-id 响应头（见 示例数据包/[506] 等）。
        本方法以“当前 CookieJar 中会随 base_url 发送的 cookie”为最新快照源，
        以响应头 X-Session-ID 为 session_id 的权威源（header 优先于 cookie）。

        Returns:
            True 表示快照发生实质变化（调用方可据此决定是否持久化）。
        """
        if self._tron_session is None or self._session is None:
            return False
        session = self._tron_session
        jar_cookies = filter_cookies_for_base(
            self._session.cookie_jar, self.base_url
        )
        new_sid = resp.headers.get("X-Session-ID", "") or _cookie_value_ci(
            jar_cookies, "session", session.session_id
        )
        new_role = _cookie_value_ci(jar_cookies, "role_token", session.role_token)

        changed = (
            jar_cookies != session.cookies
            or new_sid != session.session_id
            or new_role != session.role_token
        )
        if not changed:
            return False

        session.cookies = jar_cookies
        if new_sid:
            session.session_id = new_sid
        if new_role:
            session.role_token = new_role
            session.expires_at = decode_jwt_expiry(new_role)
        session.updated_at = time.time()
        return True

    async def _persist_if_due(self):
        """将最新凭证写回存储（节流：SESSION_WRITEBACK_MIN_INTERVAL 内至多一次）。"""
        if self._writeback is None or self._tron_session is None:
            return
        now = time.time()
        if now - self._last_persist_at < self.SESSION_WRITEBACK_MIN_INTERVAL:
            return
        data = self.get_session_data()
        if data is None:
            return
        try:
            await self._writeback(data)
            self._last_persist_at = now
        except Exception as e:
            # 写回失败不影响业务请求，下个续期响应会重试
            logger.warning(f"会话凭证写回存储失败：{e}")

    # ========== API 请求 ==========

    async def _request(
        self,
        method: str,
        path: str,
        **kwargs,
    ) -> aiohttp.ClientResponse:
        """发送 API 请求，自动携带 session cookie 和 x-session-id 头。

        默认不跟随重定向；若 3xx 且 Location 指向登录页 → 标记过期并抛 SessionInvalidError。
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

        allow_redirects = kwargs.pop("allow_redirects", False)

        resp = await self._session.request(
            method,
            url,
            timeout=timeout,
            headers=headers,
            allow_redirects=allow_redirects,
            **kwargs,
        )

        # 检测响应是否被重定向到了登录页（session 过期）
        if resp.status in (301, 302, 303, 307, 308):
            loc = resp.headers.get("Location", "")
            if any(kw in loc for kw in ("/login", "/sso", "/auth", "identity")):
                logger.warning(f"Session 已过期（API 重定向到登录页：{loc[:80]}）")
                if self._tron_session:
                    self._tron_session.expires_at = 0  # 强制标记过期
                resp.release()
                raise SessionInvalidError(
                    "会话已失效，请重新登录（响应被重定向到登录页）"
                )

        # 消费服务端滚动续期凭证（Set-Cookie 新 session/role_token + x-session-id 头）。
        # 已在上面排除“重定向到登录页”的失效路径，此处为正常/业务响应。
        if self._tron_session is not None:
            try:
                if self._apply_response_credentials(resp):
                    await self._persist_if_due()
            except Exception as e:
                logger.debug(f"会话续期凭证同步失败：{e}")

        return resp

    async def get_json(self, path: str, **kwargs) -> dict:
        """发送 GET 请求并返回 JSON；被重定向/非 JSON 时抛 SessionInvalidError。"""
        resp = await self._request("GET", path, **kwargs)
        async with resp:
            if resp.status >= 400:
                resp.raise_for_status()
            final_url = str(resp.url)
            if any(kw in final_url for kw in ("/login", "/sso", "authserver")):
                raise SessionInvalidError(
                    "会话已失效，请重新登录（响应被重定向到登录页）"
                )
            ctype = resp.content_type or ""
            if "json" not in ctype:
                raise SessionInvalidError(
                    f"会话已失效，请重新登录（响应类型异常：{ctype}）"
                )
            return await resp.json()

    async def verify_session(self) -> bool:
        """真实自检：GET /api/todos。

        session 失效（重定向/非 JSON/非 200）→ False；
        网络异常保守返回 True（避免误杀，由下次请求兜底）。
        """
        try:
            resp = await self._request(
                "GET", f"{ENDPOINT_TODOS}?no-intercept=true"
            )
        except SessionInvalidError:
            return False
        except Exception:
            return True  # 网络异常保守：不标记过期

        async with resp:
            if resp.status != 200:
                return False
            if "json" not in (resp.content_type or ""):
                return False
            final_url = str(resp.url)
            if any(kw in final_url for kw in ("/login", "/sso", "authserver")):
                return False
            try:
                data = await resp.json()
                return isinstance(data, dict)
            except Exception:
                return False

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

    先检查 is_expired，再调 verify_session 做真实 API 自检。
    返回 True 表示有效，False 表示应重新登录。
    """
    if client.is_expired:
        return False
    return await client.verify_session()
