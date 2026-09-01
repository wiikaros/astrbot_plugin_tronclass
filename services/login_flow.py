"""登录流程模块：密码登录（session_waiter 多轮交互）+ 微信扫码登录（后台轮询）。

将登录相关逻辑从 main.py 拆分至此，遵循 AstrBot 官方 session_waiter 范式：
- 密码登录：多轮会话（用户名 → 密码 → 可选短信验证码），
  由 `session_waiter` 按 sender_id 分发私聊消息，KV 仅存登录进度快照；
- 微信登录：单次命令 + 后台 `asyncio.Task` 轮询（无多轮交互，不引入 session_waiter）；
- `_login_clients`（登录中的 aiohttp ClientSession）是会话唯一跨消息持有物，
  统一由 `cleanup_login()`（幂等）在流程结束/超时/取消时关闭，避免泄漏。

行为变化（有意为之，见 README）：登录会话激活期间，该用户其他命令会被
waiter 拦截（回调内提示"登录进行中"），发送「退出」可取消登录。
"""

import asyncio
import re
import time

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import StarTools
from astrbot.core.utils.session_waiter import (
    session_waiter,
    SessionController,
    SessionFilter,
)

from ..api.auth import LoginState, TronClassClient
from ..api.homework import fetch_homeworks
from ..api.wechat_login import WeChatLoginFlow
from ..services.identity import build_friend_origin, get_user_key
from ..config import (
    PLUGIN_NAME,
    LOGIN_STATE_TTL_SECONDS,
    MAX_LOGIN_ATTEMPTS_PER_HOUR,
    KV_LOGIN_ATTEMPTS_PREFIX,
)


class PrivateChatSessionFilter(SessionFilter):
    """仅私聊消息进入登录会话；群聊消息返回空串被框架静默丢弃（隐私保护 R6）。

    部分平台私聊/群聊的 sender_id 可能相同，若不隔离，
    群聊中的普通消息会被当作登录输入（如误把群聊内容当密码）。

    P0-3：匹配 key 使用统一用户标识（platform_id:sender_id），
    避免跨平台同号 sender_id 串会话。
    """

    def filter(self, event: AstrMessageEvent) -> str:
        try:
            gid = event.get_group_id()
            if gid is None or gid == "":
                return get_user_key(event)
        except Exception:
            return get_user_key(event)
        return ""


class LoginFlowManager:
    """登录流程管理器（密码 session_waiter 交互 + 微信后台轮询）。"""

    def __init__(self, plugin):
        self._plugin = plugin
        # 登录中的 ClientSession（cookie 会话），唯一跨消息持有物
        self._login_clients: dict[str, TronClassClient] = {}
        # 同一用户回调串行化锁，防连发消息并发写 CookieJar（R7）
        self._login_locks: dict[str, asyncio.Lock] = {}
        # 微信登录后台轮询任务
        self._wechat_tasks: dict[str, asyncio.Task] = {}
        # 进行中的密码登录（防重入，R3）
        self._active_login_users: set[str] = set()

    # ========== 资源清理 ==========

    async def cleanup_login(self, user_id: str):
        """幂等清理：close client + 删 KV 登录状态 + 出索引。"""
        await self._plugin._storage.delete_login_state(user_id)
        await self._plugin._storage.mark_login_finished(user_id)
        client = self._login_clients.pop(user_id, None)
        self._login_locks.pop(user_id, None)
        if client:
            try:
                await client.close()
            except Exception:
                pass

    async def close_all(self):
        """插件卸载时清理全部登录资源（terminate 调用）。"""
        for task in self._wechat_tasks.values():
            if not task.done():
                task.cancel()
        if self._wechat_tasks:
            await asyncio.gather(*self._wechat_tasks.values(), return_exceptions=True)
        self._wechat_tasks.clear()
        for client in list(self._login_clients.values()):
            try:
                await client.close()
            except Exception:
                pass
        self._login_clients.clear()
        self._login_locks.clear()
        self._active_login_users.clear()

    async def check_login_rate_limit(self, user_id: str) -> bool:
        """检查登录频率限制（KV 持久，M9 脏数据防御）。"""
        key = f"{KV_LOGIN_ATTEMPTS_PREFIX}:{user_id}"
        attempts = await self._plugin.get_kv_data(key, default=[])
        if not isinstance(attempts, list):
            attempts = []
        now = time.time()
        recent = [t for t in attempts if now - t < 3600]
        if len(recent) >= MAX_LOGIN_ATTEMPTS_PER_HOUR:
            return False
        recent.append(now)
        await self._plugin.put_kv_data(key, recent)
        return True

    # ========== 密码登录（session_waiter 多轮交互） ==========

    async def start_password_login(self, event: AstrMessageEvent):
        """密码登录全流程（async generator，由命令 handler 转发 yield）。

        会话清理遵循"唯一出口"原则：所有终止路径（成功/失败/超时/退出/异常）
        最终由外层 finally 的 cleanup_login() 统一 close client + 删 KV。
        """
        plugin = self._plugin
        user_id = plugin._get_user_id(event)

        # ---- 前置守卫 ----
        if not plugin._is_private_chat(event):
            yield event.plain_result(
                "🔐 登录涉及账号密码，请在**私聊**中发送 /登录畅课"
            )
            return
        if not await self.check_login_rate_limit(user_id):
            yield event.plain_result("⚠️ 登录尝试过于频繁，请 1 小时后再试。")
            return
        if user_id in self._active_login_users:
            yield event.plain_result(
                "⏳ 登录流程正在进行中，请先完成或发送「退出」取消。"
            )
            return

        self._active_login_users.add(user_id)
        try:
            # 清理旧状态（幂等）
            await self.cleanup_login(user_id)
            await plugin._storage.save_login_state(user_id, {
                "step": "wait_username",
                "username": "",
                "expires_at": time.time() + LOGIN_STATE_TTL_SECONDS,
                "retries": 0,
            })
            await plugin._storage.mark_login_started(user_id)
            yield event.plain_result("🔐 请输入你的畅课用户名")

            @session_waiter(timeout=LOGIN_STATE_TTL_SECONDS, record_history_chains=False)
            async def login_flow(controller: SessionController, evt: AstrMessageEvent):
                uid = plugin._get_user_id(evt)
                text = (evt.message_str or "").strip()

                # ---- 会话控制关键字 ----
                if text in ("退出", "取消"):
                    await evt.send(evt.plain_result("已取消登录。"))
                    controller.stop()
                    return
                if text.startswith("/"):
                    # waiter 会拦截该 sender 的命令消息，显式提示并续命
                    await evt.send(evt.plain_result(
                        "登录流程进行中，请输入登录信息，或发送「退出」取消。"
                    ))
                    controller.keep(timeout=LOGIN_STATE_TTL_SECONDS, reset_timeout=True)
                    return

                state = await plugin._storage.get_login_state(uid) or {
                    "step": "wait_username",
                    "username": "",
                    "expires_at": 0,
                    "retries": 0,
                }
                step = state.get("step", "")

                # ① 用户名
                if step == "wait_username":
                    if not text:
                        controller.keep(timeout=LOGIN_STATE_TTL_SECONDS, reset_timeout=True)
                        return
                    state["username"] = text
                    state["step"] = "wait_password"
                    state["expires_at"] = time.time() + LOGIN_STATE_TTL_SECONDS
                    await plugin._storage.save_login_state(uid, state)
                    await evt.send(evt.plain_result("🔑 请输入密码（密码不会被记录）"))
                    controller.keep(timeout=LOGIN_STATE_TTL_SECONDS, reset_timeout=True)
                    return

                # ② 密码（创建/复用 client，跨消息持有）
                if step == "wait_password":
                    username = state.get("username", "")
                    if not username or not text:
                        await evt.send(evt.plain_result(
                            "⚠️ 用户名或密码不能为空，请重新发送 /登录畅课"
                        ))
                        controller.stop()
                        return
                    client = self._login_clients.get(uid)
                    if client is None:
                        client = TronClassClient(plugin._get_base_url())
                        self._login_clients[uid] = client
                    lock = self._login_locks.setdefault(uid, asyncio.Lock())
                    try:
                        async with lock:
                            r = await client.login_with_password(username, text)
                    except RuntimeError as e:
                        # pycryptodome 缺失等
                        await evt.send(evt.plain_result(f"❌ {e}"))
                        controller.stop()
                        return
                    except Exception as e:
                        logger.error(f"登录异常 [{uid}]：{e}")
                        await evt.send(evt.plain_result("❌ 登录过程出现异常，请稍后重试。"))
                        controller.stop()
                        return

                    if r.step == "done":
                        await self._handle_login_success(evt, uid, client, controller)
                        return
                    if r.step == "wait_mfa_sms":
                        # 进入短信二次认证：只存轻量状态，password 不入 KV
                        state.update({
                            "step": "wait_mfa_sms",
                            "mfa_url": r.mfa_url,
                            "mfa_service": r.mfa_service,
                            "sso_host": r.sso_host,
                        })
                        state.pop("password", None)
                        state["retries"] = 0
                        state["expires_at"] = time.time() + LOGIN_STATE_TTL_SECONDS
                        await plugin._storage.save_login_state(uid, state)
                        if r.sms_sent:
                            await evt.send(evt.plain_result(
                                "📱 需要短信二次认证，验证码已发送，请输入收到的验证码："
                            ))
                        else:
                            await evt.send(evt.plain_result(
                                "📱 需要短信二次认证，但短信发送可能失败，"
                                "请稍后重试或重新发送 /登录畅课"
                            ))
                        controller.keep(timeout=LOGIN_STATE_TTL_SECONDS, reset_timeout=True)
                        return
                    if r.step == "need_slider_captcha":
                        await evt.send(evt.plain_result(
                            f"⚠️ {r.error_msg}\n建议改用 /微信登录"
                        ))
                        controller.stop()
                        return
                    await evt.send(evt.plain_result(
                        f"❌ {r.error_msg}\n请重新发送 /登录畅课"
                    ))
                    controller.stop()
                    return

                # ③ 短信验证码（沿用 wait_password 创建的 client）
                if step == "wait_mfa_sms":
                    client = self._login_clients.get(uid)
                    if client is None:
                        # 进程重启/被误清理 → client 已失，无法续传
                        await evt.send(evt.plain_result(
                            "⚠️ 登录状态已失效（进程可能已重启），请重新发送 /登录畅课"
                        ))
                        controller.stop()
                        return
                    mfa_state = LoginState(
                        username=state.get("username", ""),
                        mfa_service=state.get("mfa_service", ""),
                        sso_host=state.get("sso_host", ""),
                        mfa_url=state.get("mfa_url", ""),
                    )
                    lock = self._login_locks.setdefault(uid, asyncio.Lock())
                    try:
                        async with lock:
                            r = await client.login_submit_mfa_sms(mfa_state, text)
                    except Exception as e:
                        logger.error(f"MFA 短信提交异常 [{uid}]：{e}")
                        await evt.send(evt.plain_result(
                            "❌ 短信验证提交异常，请重新发送 /登录畅课"
                        ))
                        controller.stop()
                        return

                    if r.step == "done":
                        await self._handle_login_success(evt, uid, client, controller)
                        return
                    if r.step == "wait_mfa_sms":
                        # 验证码错误 → 重试（keep 续命）
                        state["retries"] = state.get("retries", 0) + 1
                        if state["retries"] >= 3:
                            await evt.send(evt.plain_result(
                                "❌ 验证码错误次数过多，登录已取消。\n请重新发送 /登录畅课"
                            ))
                            controller.stop()
                            return
                        state["expires_at"] = time.time() + LOGIN_STATE_TTL_SECONDS
                        await plugin._storage.save_login_state(uid, state)
                        await evt.send(evt.plain_result(
                            f"❌ 验证码错误，请重新输入（剩余 {3 - state['retries']} 次尝试）："
                        ))
                        controller.keep(timeout=LOGIN_STATE_TTL_SECONDS, reset_timeout=True)
                        return
                    await evt.send(evt.plain_result(
                        f"❌ {r.error_msg or '短信验证失败'}\n请重新发送 /登录畅课"
                    ))
                    controller.stop()
                    return

                # 未知/异常步骤 → 清理重新开始
                await evt.send(evt.plain_result(
                    "⚠️ 登录流程状态异常，请重新发送 /登录畅课"
                ))
                controller.stop()

            try:
                await login_flow(event, session_filter=PrivateChatSessionFilter())
            except TimeoutError:
                yield event.plain_result("⏰ 登录流程已超时。请重新发送 /登录畅课")
            except Exception as e:
                logger.error(f"登录流程异常 [{user_id}]：{e}")
                yield event.plain_result("❌ 登录流程出现异常，请稍后重试。")
            finally:
                # 唯一清理出口（幂等）
                await self.cleanup_login(user_id)
                event.stop_event()
        finally:
            self._active_login_users.discard(user_id)

    async def _handle_login_success(
        self,
        evt: AstrMessageEvent,
        user_id: str,
        client: TronClassClient,
        controller: SessionController,
    ):
        """登录成功统一收尾：自检 → 保存 session → 登记 → 拉取作业填充缓存。

        注意：get_session_data()/verify_session() 必须在 client 存活期间调用，
        本方法先完成保存再 controller.stop()，由外层 finally 才关闭 client。
        """
        session_data = client.get_session_data()
        if not session_data:
            await evt.send(evt.plain_result(
                "❌ 登录未完成（未获取到 session），请重新发送 /登录畅课"
            ))
            controller.stop()
            return

        try:
            valid = await client.verify_session()
        except Exception:
            valid = False
        if not valid:
            logger.error(f"登录后 session 自检失败 [{user_id}]，不保存")
            await evt.send(evt.plain_result(
                "❌ 登录未完成（会话校验未通过），请重新发送 /登录畅课"
            ))
            controller.stop()
            return

        await self._plugin._storage.save_session(user_id, session_data)
        await self._plugin._storage.register_user(user_id)
        await self._plugin._storage.save_session_origin(
            user_id,
            evt.get_platform_name(),
            evt.get_platform_id(),
            getattr(evt, "session_id", "") or evt.get_sender_id(),
        )

        # 登录成功即拉取一次作业，保证 /作业列表 立即可查
        try:
            fresh = await fetch_homeworks(client)
            await self._plugin._storage.save_homeworks(user_id, fresh)
        except Exception as e:
            logger.warning(f"登录后拉取作业失败（不影响登录）[{user_id}]：{e}")

        await evt.send(evt.plain_result(
            "✅ 登录成功！你可以使用 /作业列表 查看作业了。"
        ))
        controller.stop()

    # ========== 微信登录（后台轮询） ==========

    async def start_wechat_login(self, event: AstrMessageEvent):
        """微信扫码登录全流程（async generator，保持后台 asyncio.Task 轮询架构）。"""
        plugin = self._plugin
        user_id = plugin._get_user_id(event)

        # 取消旧的轮询任务
        old_task = self._wechat_tasks.get(user_id)
        if old_task and not old_task.done():
            old_task.cancel()
        self._wechat_tasks.pop(user_id, None)

        base_url = plugin._get_base_url()
        flow = WeChatLoginFlow(base_url)

        # Step 1: 初始化 CAS session
        yield event.plain_result("🔐 正在准备微信登录，请稍候...")

        service = await flow.step1_init_cas_session()
        if not service:
            yield event.plain_result("❌ 无法连接畅课服务器，请稍后重试。")
            await flow.close()
            return

        # Step 2: 获取微信二维码
        qr_info = await flow.step2_get_wechat_qr(service)
        if not qr_info:
            yield event.plain_result("❌ 获取微信二维码失败，请稍后重试。")
            await flow.close()
            return

        uuid = qr_info["uuid"]
        qr_url = qr_info["qr_url"]
        wechat_state = qr_info["state"]

        # Step 3: 发送二维码图片
        # 优先下载为本地文件发送（Image.fromFileSystem，官方推荐本地路径方式）：
        # 部分平台适配器（如 QQ 官方 API）无法拉取微信外链图片，会退化为链接。
        # 下载失败时降级为 URL 图片 + 链接文本兜底，保证用户仍可扫码。
        qrcode_path = (
            StarTools.get_data_dir(PLUGIN_NAME)
            / "qrcode"
            / f"{re.sub(r'[^\w.-]', '_', user_id or '')}.png"
        )
        if await flow.download_qr_image(qr_url, qrcode_path):
            qr_chain = [Image.fromFileSystem(str(qrcode_path))]
            fallback_tip = ""
        else:
            qr_chain = [Image.fromURL(qr_url)]
            fallback_tip = f"\n（二维码图片获取失败，可打开链接扫码：{qr_url}）"

        msg_chain = [
            Plain("📱 微信扫码登录\n请用微信扫描下方二维码，扫码后点击确认登录即可"),
            *qr_chain,
        ]
        if fallback_tip:
            msg_chain.append(Plain(fallback_tip))
        msg_chain.append(Plain("等待自动完成..."))
        yield event.chain_result(msg_chain)

        # Step 4: 后台轮询 + 完成登录
        def _build_notify_target() -> str:
            """构建登录进度通知的私聊目标（FriendMessage，杜绝推送到群，P0-2）。"""
            return build_friend_origin(
                event.get_platform_id(),
                getattr(event, "session_id", "") or event.get_sender_id(),
            )

        async def _send_notice(msg: str):
            try:
                await plugin.context.send_message(
                    _build_notify_target(), MessageChain([Plain(msg)])
                )
            except Exception as e:
                logger.error(f"[微信登录] 发送通知失败: {e}")

        async def _poll_and_finish():
            try:
                wx_code = await flow.step3_poll_scan(uuid)
                if not wx_code:
                    await _send_notice("⏰ 微信登录超时，请重新发送 /微信登录")
                    return

                session_data = await flow.step4_callback_and_get_session(
                    wx_code, wechat_state
                )
                if not session_data:
                    await _send_notice("❌ 微信登录失败，请重试 /微信登录")
                    return

                await plugin._storage.save_session(user_id, session_data)
                await plugin._storage.register_user(user_id)
                await plugin._storage.save_session_origin(
                    user_id,
                    event.get_platform_name(),
                    event.get_platform_id(),
                    getattr(event, "session_id", "") or event.get_sender_id(),
                )

                # 校验 session 有效性 + 顺手拉取一次作业（失败不影响登录）
                try:
                    client = TronClassClient.from_session_data(session_data)
                    valid = await client.verify_session()
                    if valid:
                        try:
                            fresh = await fetch_homeworks(client)
                            await plugin._storage.save_homeworks(user_id, fresh)
                        except Exception as e:
                            logger.warning(
                                f"微信登录后拉取作业失败（不影响登录）[{user_id}]：{e}"
                            )
                    await client.close()
                    if not valid:
                        await plugin._storage.delete_session(user_id)
                        await plugin._storage.unregister_user(user_id)
                        await _send_notice(
                            "❌ 微信登录未完成（会话校验未通过），请重试 /微信登录"
                        )
                        return
                except Exception as e:
                    logger.error(f"微信登录会话校验异常 [{user_id}]：{e}")

                await _send_notice("✅ 微信登录成功！你可以使用 /作业列表 查看作业了。")
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.error(f"微信登录轮询异常 [{user_id}]：{e}")
                await _send_notice("❌ 微信登录出现异常，请重试。")
            finally:
                await flow.close()
                self._wechat_tasks.pop(user_id, None)

        task = asyncio.create_task(_poll_and_finish())
        self._wechat_tasks[user_id] = task
