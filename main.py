"""AstrBot 畅课（TronClass）插件 — 入口模块。"""

import time
import asyncio

from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register
from astrbot.core import AstrBotConfig
from astrbot.api import logger
from astrbot.api.message_components import Image, Plain
from astrbot.core.utils.session_waiter import session_waiter, SessionController
from astrbot.core.utils.io import download_file
from astrbot.core.star import StarTools

from .config import (
    LOGIN_STATE_TTL_SECONDS,
    MAX_LOGIN_ATTEMPTS_PER_HOUR,
    DEFAULT_BASE_URL,
    DEFAULT_HOMEWORK_CHECK_INTERVAL,
    DEFAULT_ROLLCALL_DEFAULT_INTERVAL,
    DEFAULT_ROLLCALL_PRECHECK_MINUTES,
    DEFAULT_HOMEWORK_DUE_WARN_HOURS,
)
from .api.auth import TronClassClient, SessionInvalidError
from .api._utils import parse_datetime
from .api.wechat_login import WeChatLoginFlow
from .api.homework import fetch_homeworks, diff_homeworks, get_imminent_due
from .api.rollcall import fetch_rollcalls, detect_new_rollcalls
from .services.storage import StorageService
from .services.ics_parser import parse_ics
from .services.notifier import format_homework_summary
from .services.scheduler import SchedulerService


class TronClassPlugin(Star):
    """畅课助手插件：作业查询/提醒 + 点名实时通知。"""

    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config
        self._storage = StorageService(self)
        self._scheduler: SchedulerService | None = None
        self._wechat_tasks: dict[str, asyncio.Task] = {}  # user_id → polling task
        self._login_clients: dict[str, TronClassClient] = {}  # 登录流程持有的 ClientSession
        logger.info("畅课助手插件已加载")

    async def terminate(self):
        """插件卸载/停用时调用。"""
        for task in self._wechat_tasks.values():
            if not task.done():
                task.cancel()
        # 注销定时任务，防止热重载后旧实例任务残留重复执行（框架合规 L1）
        if self._scheduler is not None:
            try:
                await self._scheduler.shutdown()
            except Exception as e:
                logger.warning(f"注销定时任务异常: {e}")
        for client in self._login_clients.values():
            try:
                await client.close()
            except Exception:
                pass
        self._login_clients.clear()
        logger.info("畅课助手插件已卸载")

    # ========== 辅助方法 ==========

    def _get_config(self, key: str, default=None):
        """安全读取配置项，支持嵌套 key（如 'school.base_url'）。"""
        if self.config is None:
            return default
        if "." in key:
            parts = key.split(".")
            value = self.config
            for part in parts:
                if isinstance(value, dict):
                    value = value.get(part)
                else:
                    return default
            return value if value is not None else default
        return self.config.get(key, default)

    def _get_user_id(self, event: AstrMessageEvent) -> str:
        """获取当前用户唯一标识。"""
        return event.get_sender_id()

    def _is_private_chat(self, event: AstrMessageEvent) -> bool:
        """判断是否为私聊会话。"""
        try:
            gid = event.get_group_id()
            return gid is None or gid == ""
        except Exception:
            return True

    def _get_base_url(self) -> str:
        """获取配置的畅课服务器地址。"""
        return self._get_config("school.base_url", DEFAULT_BASE_URL)

    @staticmethod
    def _fmt_due(iso_str: str) -> str:
        """将 ISO 格式时间转为可读形式，如 '6月30日 15:59'。"""
        if not iso_str:
            return "未知截止时间"
        dt = parse_datetime(iso_str)
        if dt:
            return dt.strftime("%m月%d日 %H:%M")
        return iso_str

    async def _create_client(self, user_id: str) -> TronClassClient | None:
        """为用户创建已认证的 API 客户端。"""
        session_data = await self._storage.get_session(user_id)
        if session_data is None:
            return None
        client = TronClassClient.from_session_data(session_data)
        if client.is_expired:
            await client.close()
            return None
        return client

    # ========== 事件：插件加载完成 ==========

    @filter.on_astrbot_loaded()
    async def on_bot_loaded(self, event: AstrMessageEvent):
        """插件加载完成后初始化定时任务。"""
        logger.info("畅课助手：初始化定时任务...")

        self._scheduler = SchedulerService(
            context=self.context,
            storage=self._storage,
            base_url=self._get_base_url(),
            homework_interval=self._get_config(
                "homework_check_interval", DEFAULT_HOMEWORK_CHECK_INTERVAL
            ),
            rollcall_default_interval=self._get_config(
                "rollcall_default_interval", DEFAULT_ROLLCALL_DEFAULT_INTERVAL
            ),
            precheck_minutes=self._get_config(
                "rollcall_class_precheck_minutes", DEFAULT_ROLLCALL_PRECHECK_MINUTES
            ),
            due_warn_hours=self._get_config(
                "homework_due_warn_hours", DEFAULT_HOMEWORK_DUE_WARN_HOURS
            ),
            enable_homework_notify=self._get_config(
                "enable_new_homework_notify", True
            ),
            enable_due_warning=self._get_config("enable_due_warning", True),
            enable_rollcall_notify=self._get_config(
                "enable_rollcall_notify", True
            ),
        )
        await self._scheduler.setup()
        logger.info("畅课助手：定时任务初始化完成")

    # ========== 事件：任意消息 — 登录状态机驱动器 ==========

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_any_message(self, event: AstrMessageEvent):
        """监听所有消息，处理登录状态机的后续步骤。"""
        # 只在私聊中处理登录状态机
        if not self._is_private_chat(event):
            return

        user_id = self._get_user_id(event)
        login_state = await self._storage.get_login_state(user_id)

        if login_state is None:
            return  # 没有活跃的登录流程

        # 检查超时
        expires_at = login_state.get("expires_at", 0)
        if expires_at and time.time() > expires_at:
            await self._finalize_login(user_id)
            yield event.plain_result(
                "⏰ 登录流程已超时。请重新发送 /登录畅课"
            )
            return

        text = event.message_str.strip()

        # 忽略命令（如果用户又发了 /登录畅课，让命令 handler 重启流程）
        if text.startswith("/"):
            return

        step = login_state.get("step", "")

        if step == "wait_username":
            async for result in self._handle_login_username(event, login_state, text):
                yield result
        elif step == "wait_password":
            async for result in self._handle_login_password(event, login_state, text):
                yield result
        elif step == "wait_mfa_sms":
            async for result in self._handle_login_mfa_sms(event, login_state, text):
                yield result
        else:
            # 未知/异常步骤 → 清理状态，重新开始
            await self._finalize_login(user_id)
            yield event.plain_result(
                "⚠️ 登录流程状态异常，请重新发送 /登录畅课"
            )

    # ========== 命令：/重置登录限制 ==========

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("重置登录限制")
    async def cmd_reset_login_limit(self, event: AstrMessageEvent, user_id: str = None):
        """管理员命令：重置用户的登录频率限制。"""

        # 如果未指定 user_id，则默认重置当前用户的限制
        if user_id is None:
            user_id = self._get_user_id(event)

        # 如果指定的 user_id 不存在
        if not await self.get_kv_data(f"_login_attempts:{user_id}"):
            yield event.plain_result(f"⚠️ 用户 {user_id} 没有登录尝试记录，无需重置。")
            return
        await self.delete_kv_data(f"_login_attempts:{user_id}")
        yield event.plain_result("✅ 登录限制已重置，可以重新登录了。")

    # ========== 命令：/微信登录 ==========

    @filter.command("微信登录")
    async def cmd_wechat_login(self, event: AstrMessageEvent):
        """微信扫码登录 —— 无需密码/验证码。"""
        user_id = self._get_user_id(event)

        # 取消旧的轮询任务
        if user_id in self._wechat_tasks:
            old_task = self._wechat_tasks[user_id]
            if not old_task.done():
                old_task.cancel()
            del self._wechat_tasks[user_id]

        base_url = self._get_base_url()
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
        yield event.chain_result([
            Plain("📱 微信扫码登录\n请用微信扫描下方二维码，扫码后点击确认登录即可"),
            Image.fromURL(qr_url),
            Plain("等待自动完成..."),
        ])

        # Step 4: 后台轮询 + 完成登录
        session_key = getattr(event, "unified_msg_origin", "") or getattr(event, "session", "")

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

                await self._storage.save_session(user_id, session_data)
                await self._storage.register_user(user_id)

                # 校验 session 有效性 + 顺手拉取一次作业（失败不影响登录）
                try:
                    client = TronClassClient.from_session_data(session_data)
                    valid = await client.verify_session()
                    if valid:
                        try:
                            fresh = await fetch_homeworks(client)
                            await self._storage.save_homeworks(user_id, fresh)
                        except Exception as e:
                            logger.warning(
                                f"微信登录后拉取作业失败（不影响登录）[{user_id}]：{e}"
                            )
                    await client.close()
                    if not valid:
                        await self._storage.delete_session(user_id)
                        await self._storage.unregister_user(user_id)
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

        async def _send_notice(msg: str):
            try:
                await self.context.send_message(
                    session_key, MessageChain([Plain(msg)])
                )
            except Exception as e:
                logger.error(f"[微信登录] 发送通知失败: {e}")

        task = asyncio.create_task(_poll_and_finish())
        self._wechat_tasks[user_id] = task

    # ========== 命令：/登录畅课 ==========

    @filter.command("登录畅课")
    async def cmd_login(self, event: AstrMessageEvent):
        """交互式登录畅课账号。"""
        user_id = self._get_user_id(event)

        # 频率限制检查
        if not await self._check_login_rate_limit(user_id):
            yield event.plain_result(
                "⚠️ 登录尝试过于频繁，请 1 小时后再试。"
            )
            return

        if not self._is_private_chat(event):
            yield event.plain_result(
                "🔐 登录涉及账号密码，请在**私聊**中发送 /登录畅课"
            )
            return

        # 清除旧状态
        await self._finalize_login(user_id)

        login_state = {
            "step": "wait_username",
            "username": "",
            "password": "",
            "expires_at": time.time() + LOGIN_STATE_TTL_SECONDS,
            "retries": 0,
        }
        await self._storage.save_login_state(user_id, login_state)

        yield event.plain_result("🔐 请输入你的畅课用户名")

    async def _handle_login_username(
        self, event: AstrMessageEvent, login_state: dict, text: str
    ):
        """处理用户输入用户名。"""
        user_id = self._get_user_id(event)
        login_state["username"] = text.strip()
        login_state["step"] = "wait_password"
        login_state["expires_at"] = time.time() + LOGIN_STATE_TTL_SECONDS
        await self._storage.save_login_state(user_id, login_state)
        yield event.plain_result("🔑 请输入密码（密码不会被记录）")

    async def _handle_login_password(
        self, event: AstrMessageEvent, login_state: dict, text: str
    ):
        """处理用户输入密码，执行 CAS 登录主流程（含 MFA 触发）。"""
        user_id = self._get_user_id(event)
        username = login_state.get("username", "")
        password = text.strip()
        if not username or not password:
            yield event.plain_result("⚠️ 用户名或密码不能为空，请重新发送 /登录畅课")
            return

        client = self._login_clients.get(user_id)
        if client is None:
            client = TronClassClient(self._get_base_url())
            self._login_clients[user_id] = client

        try:
            state = await client.login_with_password(username, password)
        except RuntimeError as e:
            # pycryptodome 缺失等
            await self._finalize_login(user_id)
            yield event.plain_result(f"❌ {e}")
            return
        except Exception as e:
            logger.error(f"登录异常 [{user_id}]：{e}")
            await self._finalize_login(user_id)
            yield event.plain_result("❌ 登录过程出现异常，请稍后重试。")
            return

        if state.step == "done":
            async for result in self._login_success_finalize(event, user_id, client):
                yield result
            return

        if state.step == "wait_mfa_sms":
            # 进入短信二次认证：清除密码，只存轻量状态
            login_state.pop("password", None)
            login_state["step"] = "wait_mfa_sms"
            login_state["mfa_url"] = state.mfa_url
            login_state["mfa_service"] = state.mfa_service
            login_state["sso_host"] = state.sso_host
            login_state["retries"] = 0
            login_state["expires_at"] = time.time() + LOGIN_STATE_TTL_SECONDS
            await self._storage.save_login_state(user_id, login_state)
            if state.sms_sent:
                yield event.plain_result(
                    "📱 需要短信二次认证，验证码已发送，请输入收到的验证码："
                )
            else:
                yield event.plain_result(
                    "📱 需要短信二次认证，但短信发送可能失败，"
                    "请稍后重试或重新发送 /登录畅课"
                )
            return

        if state.step == "need_slider_captcha":
            await self._finalize_login(user_id)
            yield event.plain_result(
                f"⚠️ {state.error_msg}\n建议改用 /微信登录"
            )
            return

        # error
        await self._finalize_login(user_id)
        yield event.plain_result(
            f"❌ {state.error_msg}\n请重新发送 /登录畅课"
        )

    async def _handle_login_mfa_sms(
        self, event: AstrMessageEvent, login_state: dict, text: str
    ):
        """处理用户输入短信验证码，提交 MFA 并完成登录。"""
        user_id = self._get_user_id(event)
        client = self._login_clients.get(user_id)
        if client is None:
            await self._finalize_login(user_id)
            yield event.plain_result(
                "⚠️ 登录状态已失效（进程可能已重启），请重新发送 /登录畅课"
            )
            return

        from .api.auth import LoginState
        state = LoginState(
            username=login_state.get("username", ""),
            mfa_service=login_state.get("mfa_service", ""),
            sso_host=login_state.get("sso_host", ""),
            mfa_url=login_state.get("mfa_url", ""),
        )

        try:
            result = await client.login_submit_mfa_sms(state, text.strip())
        except Exception as e:
            logger.error(f"MFA 短信提交异常 [{user_id}]：{e}")
            await self._finalize_login(user_id)
            yield event.plain_result("❌ 短信验证提交异常，请重新发送 /登录畅课")
            return

        if result.step == "done":
            async for r in self._login_success_finalize(event, user_id, client):
                yield r
            return

        if result.step == "wait_mfa_sms":
            # 验证码错误，重试
            login_state["retries"] = login_state.get("retries", 0) + 1
            if login_state["retries"] >= 3:
                await self._finalize_login(user_id)
                yield event.plain_result(
                    "❌ 验证码错误次数过多，登录已取消。\n请重新发送 /登录畅课"
                )
                return
            login_state["expires_at"] = time.time() + LOGIN_STATE_TTL_SECONDS
            await self._storage.save_login_state(user_id, login_state)
            yield event.plain_result(
                f"❌ 验证码错误，请重新输入（剩余 {3 - login_state['retries']} 次尝试）："
            )
            return

        # error
        await self._finalize_login(user_id)
        yield event.plain_result(
            f"❌ {result.error_msg or '短信验证失败'}\n请重新发送 /登录畅课"
        )

    async def _login_success_finalize(
        self, event: AstrMessageEvent, user_id: str, client: TronClassClient
    ):
        """登录成功统一收尾：自检 → 保存 session → 登记 → 拉取作业填充缓存。"""
        session_data = client.get_session_data()
        if not session_data:
            await self._finalize_login(user_id)
            yield event.plain_result(
                "❌ 登录未完成（未获取到 session），请重新发送 /登录畅课"
            )
            return

        try:
            valid = await client.verify_session()
        except Exception:
            valid = False
        if not valid:
            logger.error(f"登录后 session 自检失败 [{user_id}]，不保存")
            await self._finalize_login(user_id)
            yield event.plain_result(
                "❌ 登录未完成（会话校验未通过），请重新发送 /登录畅课"
            )
            return

        await self._storage.save_session(user_id, session_data)
        await self._storage.register_user(user_id)

        # 登录成功即拉取一次作业，保证 /作业列表 立即可查
        try:
            fresh = await fetch_homeworks(client)
            await self._storage.save_homeworks(user_id, fresh)
        except Exception as e:
            logger.warning(f"登录后拉取作业失败（不影响登录）[{user_id}]：{e}")

        await self._finalize_login(user_id)
        yield event.plain_result(
            "✅ 登录成功！你可以使用 /作业列表 查看作业了。"
        )

    async def _finalize_login(self, user_id: str):
        """清 KV login_state + close 并删除内存中的登录 client。"""
        await self._storage.delete_login_state(user_id)
        client = self._login_clients.pop(user_id, None)
        if client:
            try:
                await client.close()
            except Exception:
                pass

    async def _check_login_rate_limit(self, user_id: str) -> bool:
        """检查登录频率限制。"""
        key = f"_login_attempts:{user_id}"
        attempts = await self.get_kv_data(key, default=[])
        now = time.time()

        # 清理 1 小时前的记录
        recent = [t for t in attempts if now - t < 3600]
        if len(recent) >= MAX_LOGIN_ATTEMPTS_PER_HOUR:
            return False

        recent.append(now)
        await self.put_kv_data(key, recent)
        return True

    # ========== 命令：/调试作业 ==========

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("调试作业")
    async def cmd_debug_homework(self, event: AstrMessageEvent):
        """调试用（仅管理员）：打印缓存的原始作业数据。"""
        user_id = self._get_user_id(event)
        homeworks = await self._storage.get_homeworks(user_id)
        if not homeworks:
            yield event.plain_result("暂无缓存数据，请先 /更新作业")
            return
        import json as _json
        yield event.plain_result(
            f"缓存的原始作业数据（共 {len(homeworks)} 项）：\n"
            + _json.dumps(homeworks, ensure_ascii=False, indent=2)[:2000]
        )

    # ========== 命令：/作业列表 ==========

    @filter.command("作业列表")
    async def cmd_homework_list(self, event: AstrMessageEvent):
        """查询未完成的作业列表。"""
        user_id = self._get_user_id(event)

        homeworks = await self._storage.get_homeworks(user_id)

        if not homeworks:
            yield event.plain_result(
                "📋 暂无作业数据。\n"
                "请先发送 /更新作业 获取最新作业列表"
            )
            return

        # 过滤已完成项
        active = [
            h for h in homeworks
            if h.get("status") not in ("已提交", "已完成", "submitted", "graded")
        ]

        if not active:
            yield event.plain_result("✅ 所有作业已完成！")
            return

        # 按截止时间排序
        active.sort(key=lambda h: h.get("due_at", ""))

        lines = [f"📋 待完成作业（共 {len(active)} 项）：", ""]
        for i, hw in enumerate(active, 1):
            course = hw.get("course_name", "未知课程")
            title = hw.get("title", "未命名作业")
            due = self._fmt_due(hw.get("due_at", ""))
            status = hw.get("status", "未知")
            lines.append(f"{i}.《{course}》{title}")
            lines.append(f"   ⏰ 截止: {due}")
            lines.append(f"   📌 状态: {status}")
            lines.append("")

        yield event.plain_result("\n".join(lines))

    # ========== 命令：/更新作业 ==========

    @filter.command("更新作业")
    async def cmd_update_homework(self, event: AstrMessageEvent):
        """手动触发作业列表更新。"""
        user_id = self._get_user_id(event)

        client = await self._create_client(user_id)
        if client is None:
            yield event.plain_result(
                "⚠️ 你尚未登录或登录已过期。\n"
                "请先在私聊中发送 /登录畅课"
            )
            return

        yield event.plain_result("🔄 正在更新作业列表...")

        try:
            fresh = await fetch_homeworks(client)
        except SessionInvalidError as e:
            # session 已失效：清理本地状态并引导重新登录
            await client.close()
            await self._storage.delete_session(user_id)
            await self._storage.unregister_user(user_id)
            logger.warning(f"更新作业时 session 失效 [{user_id}]")
            yield event.plain_result(
                f"⚠️ {e}\n请在私聊中重新发送 /登录畅课 或 /微信登录"
            )
            return
        except Exception as e:
            await client.close()
            logger.error(f"获取作业列表失败 [{user_id}]：{e}")
            yield event.plain_result(
                f"❌ 获取作业列表失败：{e}\n请检查网络后重试。"
            )
            return

        cached = await self._storage.get_homeworks(user_id)
        diff = diff_homeworks(cached, fresh)

        # 保存最新数据
        await self._storage.save_homeworks(user_id, fresh)

        # 检查快到期
        warn_hours = self._get_config(
            "homework_due_warn_hours", DEFAULT_HOMEWORK_DUE_WARN_HOURS
        )
        imminent = get_imminent_due(fresh, warn_hours)

        # 返回摘要
        summary = format_homework_summary(
            diff["added"], diff["updated"], diff["removed"]
        )

        await client.close()

        # 如果有快到期作业，附带提醒
        if imminent and self._get_config("enable_due_warning", True):
            summary += "\n\n⚠️ **快到期提醒：**\n"
            for hw in imminent:
                course = hw.get("course_name", "?")
                title = hw.get("title", "?")
                due = self._fmt_due(hw.get("due_at", ""))
                summary += f"  - 《{course}》{title}（截止：{due}）\n"

        yield event.plain_result(summary)

    # ========== 命令：/上传课表 ==========

    @filter.command("上传课表")
    async def cmd_upload_schedule(self, event: AstrMessageEvent):
        """上传 .ics 课表文件。使用 session_waiter 等待用户发送文件。"""
        user_id = self._get_user_id(event)

        yield event.plain_result(
            "📅 请在 120 秒内发送 .ics 课表文件，发送「退出」可取消。\n\n"
            "获取方式：从学校教务系统导出课表为 .ics 文件，直接发送即可。"
        )

        @session_waiter(timeout=120, record_history_chains=False)
        async def waiter(controller: SessionController, evt: AstrMessageEvent):
            text = (evt.message_str or "").strip()
            if text == "退出":
                await evt.send(evt.plain_result("已取消上传。"))
                controller.stop()
                return

            # 尝试从消息附件获取文件
            file_url = await self._try_get_file_url(evt)
            if not file_url:
                await evt.send(evt.plain_result("未检测到文件，请直接发送 .ics 课表文件。发送「退出」可取消。"))
                controller.keep(timeout=120, reset_timeout=True)
                return

            # 下载文件
            await evt.send(evt.plain_result("正在下载课表文件..."))
            ics_dir = StarTools.get_data_dir("astrbot_plugin_tronclass") / "ics"
            ics_dir.mkdir(parents=True, exist_ok=True)
            ics_path = ics_dir / f"{user_id}.ics"

            try:
                await download_file(file_url, str(ics_path))
                content = ics_path.read_text(encoding="utf-8")
            except Exception as e:
                logger.error(f"下载 ICS 文件失败 [{user_id}]：{e}")
                await evt.send(evt.plain_result("❌ 文件下载失败，请重试。"))
                controller.stop()
                return

            # 解析
            schedule = parse_ics(content)
            if schedule is None:
                await evt.send(evt.plain_result(
                    "❌ 课表解析失败。请确认文件是标准的 .ics 格式，且包含课程事件。"
                ))
                controller.stop()
                return

            # 保存
            await self._storage.save_schedule(user_id, schedule)
            course_count = len(schedule.get("courses", []))
            semester_start = schedule.get("semester_start", "未知")

            await evt.send(evt.plain_result(
                f"✅ 课表导入成功！\n"
                f"📅 学期起始：{semester_start}\n"
                f"📚 课程数量：{course_count} 门\n\n"
                f"将在上课时间自动检测点名，无需手动操作。"
            ))
            controller.stop()

        try:
            await waiter(event)
        except TimeoutError:
            yield event.plain_result("⏰ 上传超时，请重新发送 /上传课表。")
        finally:
            event.stop_event()

    @staticmethod
    async def _try_get_file_url(evt: AstrMessageEvent) -> str | None:
        """从消息事件中提取 .ics 文件的下载 URL。"""
        try:
            # 尝试多种方式获取附件
            attachments = None
            for meth in ("get_attachments", "attachments", "get_uploaded_files"):
                fn = getattr(evt, meth, None)
                if callable(fn):
                    attachments = fn()
                elif fn is not None:
                    attachments = fn
                if attachments:
                    break

            if not attachments:
                # 检查消息链中的 File 组件
                try:
                    messages = evt.get_messages()
                    import astrbot.api.message_components as Comp
                    for seg in (messages or []):
                        if isinstance(seg, Comp.File):
                            name = getattr(seg, "name", "") or getattr(seg, "file", "")
                            url = getattr(seg, "url", "")
                            if url and ("ics" in name.lower()):
                                return url
                except Exception:
                    pass
                return None

            for att in (attachments or []):
                if isinstance(att, dict):
                    name = att.get("name", att.get("filename", ""))
                    url = att.get("url", att.get("data", ""))
                else:
                    name = getattr(att, "name", "") or getattr(att, "filename", "")
                    url = getattr(att, "url", "") or getattr(att, "data", "")
                if name.lower().endswith(".ics") or "ics" in name.lower():
                    if url:
                        return url
        except Exception as e:
            logger.warning(f"[上传课表] 提取文件 URL 异常: {e}")

        return None
