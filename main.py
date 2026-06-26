"""AstrBot 畅课（TronClass）插件 — 入口模块。"""

import time
import asyncio

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.core import AstrBotConfig
from astrbot.api import logger
from astrbot.api.message_components import Plain
from astrbot.core.message.message_event_result import MessageChain

from .config import (
    LOGIN_STATE_TTL_SECONDS,
    MAX_LOGIN_ATTEMPTS_PER_HOUR,
    DEFAULT_BASE_URL,
    DEFAULT_HOMEWORK_CHECK_INTERVAL,
    DEFAULT_ROLLCALL_DEFAULT_INTERVAL,
    DEFAULT_ROLLCALL_PRECHECK_MINUTES,
    DEFAULT_HOMEWORK_DUE_WARN_HOURS,
)
from .api.auth import TronClassClient
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
        logger.info("畅课助手插件已加载")

    async def terminate(self):
        """插件卸载/停用时调用。"""
        for task in self._wechat_tasks.values():
            if not task.done():
                task.cancel()
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
            await self._storage.delete_login_state(user_id)
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
        elif step == "wait_captcha":
            async for result in self._handle_login_captcha(event, login_state, text):
                yield result

    # ========== 命令：/重置登录限制 ==========

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("重置登录限制")
    async def cmd_reset_login_limit(self, event: AstrMessageEvent):
        """管理员命令：重置当前用户的登录频率限制。"""
        user_id = self._get_user_id(event)
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

        # Step 3: 发送二维码给用户
        yield event.plain_result(
            "📱 **微信扫码登录**\n\n"
            f"请打开链接用微信扫描二维码：\n{qr_url}\n\n"
            "扫码后请**点击确认登录**，等待自动完成..."
        )

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
        await self._storage.delete_login_state(user_id)

        login_state = {
            "step": "wait_username",
            "username": "",
            "password": "",
            "captcha_type": "",
            "execution": "",
            "lt_token": "",
            "login_url": "",
            "captcha_url": "",
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
        """处理用户输入密码，尝试第一步登录。"""
        user_id = self._get_user_id(event)
        login_state["password"] = text.strip()
        login_state["step"] = "logging_in"
        await self._storage.save_login_state(user_id, login_state)

        base_url = self._get_base_url()
        client = TronClassClient(base_url)

        try:
            # 第一步：获取登录页面，提取 token
            state = await client.login_step_get_login_page(
                login_state["username"], login_state["password"]
            )

            if state.step == "done":
                # 直接登录成功（无额外验证）
                session_data = client.get_session_data()
                if session_data:
                    await self._storage.save_session(user_id, session_data)
                await self._storage.delete_login_state(user_id)
                await client.close()
                yield event.plain_result("✅ 登录成功！你可以使用 /作业列表 查看作业了。")
                return

            elif state.step == "wait_password":
                # 需要提交密码（正常流程）
                result_state = await client.login_step_submit(state)

                if result_state.step == "done":
                    session_data = client.get_session_data()
                    if session_data:
                        await self._storage.save_session(user_id, session_data)
                    await self._storage.delete_login_state(user_id)
                    await client.close()
                    yield event.plain_result(
                        "✅ 登录成功！你可以使用 /作业列表 查看作业了。"
                    )
                    return

                elif result_state.step == "wait_captcha":
                    # 需要验证码
                    captcha_type = result_state.captcha_type
                    self._save_login_state_with_auth(
                        login_state, result_state, captcha_type
                    )
                    await self._storage.save_login_state(user_id, login_state)

                    if captcha_type == "image":
                        msg = "📷 需要输入图片验证码\n请输入图片中的验证码："
                        if result_state.captcha_url:
                            msg = (
                                f"📷 需要输入图片验证码\n"
                                f"验证码图片：{result_state.captcha_url}\n"
                                f"请打开链接查看并输入验证码："
                            )
                        yield event.plain_result(msg)
                    else:
                        triggered = await client.login_step_trigger_sms(result_state)
                        if triggered:
                            yield event.plain_result(
                                "📱 需要手机短信验证码\n"
                                "已触发短信发送，请输入收到的验证码："
                            )
                        else:
                            yield event.plain_result(
                                "📱 需要手机短信验证码\n请输入收到的验证码："
                            )
                    return

                else:
                    await self._storage.delete_login_state(user_id)
                    await client.close()
                    yield event.plain_result(
                        "❌ 登录失败：用户名或密码错误，请重新发送 /登录畅课"
                    )
                    return

            elif state.step == "error":
                await self._storage.delete_login_state(user_id)
                await client.close()
                yield event.plain_result(
                    "❌ 登录失败：无法连接到畅课服务器，请稍后重试。\n"
                    "请检查网络或服务器地址配置。"
                )
                return

        except Exception as e:
            logger.error(f"登录异常 [{user_id}]：{e}")
            await self._storage.delete_login_state(user_id)
            await client.close()
            yield event.plain_result(
                f"❌ 登录过程出现异常：{e}\n请稍后重试或联系管理员。"
            )

    async def _handle_login_captcha(
        self, event: AstrMessageEvent, login_state: dict, text: str
    ):
        """处理用户输入验证码。"""
        user_id = self._get_user_id(event)
        captcha = text.strip()

        base_url = self._get_base_url()
        client = TronClassClient(base_url)

        try:
            # 重建 LoginState
            from .api.auth import LoginState
            state = LoginState(
                username=login_state["username"],
                password=login_state["password"],
                captcha_type=login_state["captcha_type"],
                lt_token=login_state["lt_token"],
                execution=login_state["execution"],
                login_url=login_state["login_url"],
                captcha_url=login_state.get("captcha_url", ""),
                sms_action_url=login_state.get("sms_action_url", ""),
                sms_form_inputs=login_state.get("sms_form_inputs", {}),
                sms_captcha_field=login_state.get("sms_captcha_field", ""),
            )

            if login_state["captcha_type"] == "sms":
                result_state = await client.login_step_submit_sms(state, captcha)
            else:
                result_state = await client.login_step_submit(state, captcha=captcha)

            if result_state.step == "done":
                session_data = client.get_session_data()
                if session_data:
                    await self._storage.save_session(user_id, session_data)
                await self._storage.delete_login_state(user_id)
                await client.close()
                yield event.plain_result(
                    "✅ 登录成功！你可以使用 /作业列表 查看作业了。"
                )
                return

            elif result_state.step == "wait_captcha":
                # 验证码错误，重试
                login_state["retries"] = login_state.get("retries", 0) + 1
                if login_state["retries"] >= 3:
                    await self._storage.delete_login_state(user_id)
                    await client.close()
                    yield event.plain_result(
                        "❌ 验证码错误次数过多，登录已取消。\n"
                        "请重新发送 /登录畅课"
                    )
                    return

                self._save_login_state_with_auth(
                    login_state, result_state,
                    result_state.captcha_type or login_state["captcha_type"],
                )
                await self._storage.save_login_state(user_id, login_state)

                yield event.plain_result(
                    f"❌ 验证码错误，请重新输入（剩余 {3 - login_state['retries']} 次尝试）："
                )
                return

            else:
                await self._storage.delete_login_state(user_id)
                await client.close()
                yield event.plain_result(
                    "❌ 登录失败，请重新发送 /登录畅课"
                )
                return

        except Exception as e:
            logger.error(f"验证码登录异常 [{user_id}]：{e}")
            await self._storage.delete_login_state(user_id)
            await client.close()
            yield event.plain_result(
                f"❌ 登录异常：{e}\n请稍后重试。"
            )

    def _save_login_state_with_auth(
        self, login_state: dict, auth_state, captcha_type: str
    ):
        """将 auth 层的 LoginState 数据同步到 KV 中的 login_state。"""
        login_state["step"] = "wait_captcha"
        login_state["captcha_type"] = captcha_type
        login_state["lt_token"] = auth_state.lt_token
        login_state["execution"] = auth_state.execution
        login_state["login_url"] = auth_state.login_url
        login_state["captcha_url"] = auth_state.captcha_url
        login_state["sms_action_url"] = getattr(auth_state, "sms_action_url", "")
        login_state["sms_form_inputs"] = getattr(auth_state, "sms_form_inputs", {})
        login_state["sms_captcha_field"] = getattr(auth_state, "sms_captcha_field", "")
        login_state["sms_trigger_url"] = getattr(auth_state, "sms_trigger_url", "")
        login_state["expires_at"] = time.time() + LOGIN_STATE_TTL_SECONDS

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

    @filter.command("调试作业")
    async def cmd_debug_homework(self, event: AstrMessageEvent):
        """调试用：打印缓存的原始作业数据。"""
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
        """上传 .ics 课表文件。"""
        user_id = self._get_user_id(event)

        # 尝试从消息中提取文件
        ics_content = await self._extract_ics_from_event(event)

        if ics_content is None:
            yield event.plain_result(
                "📅 请将 .ics 课表文件发送给我，同时附带命令 /上传课表。\n\n"
                "获取方式：\n"
                "1. 从学校教务系统导出课表（通常支持 .ics 格式）\n"
                "2. 在手机/电脑日历 App 中导出课表为 .ics 文件\n"
                "3. 将文件发送给我并附带此命令"
            )
            return

        schedule = parse_ics(ics_content)
        if schedule is None:
            yield event.plain_result(
                "❌ 课表文件解析失败。请确认：\n"
                "1. 文件是标准的 .ics 格式\n"
                "2. 文件内容完整无损坏\n"
                "3. 文件包含课程事件（VEVENT）"
            )
            return

        await self._storage.save_schedule(user_id, schedule)

        # 统计信息
        course_count = len(schedule.get("courses", []))
        semester_start = schedule.get("semester_start", "未知")

        yield event.plain_result(
            f"✅ 课表导入成功！\n"
            f"📅 学期起始：{semester_start}\n"
            f"📚 课程数量：{course_count} 门\n\n"
            f"将在上课时间自动检测点名，无需手动操作。"
        )

    async def _extract_ics_from_event(self, event: AstrMessageEvent) -> str | None:
        """从消息事件中提取 .ics 文件内容。"""
        # 方式 1：消息附件
        try:
            attachments = event.get_attachments()
            if attachments:
                for att in attachments:
                    if isinstance(att, dict):
                        name = att.get("name", att.get("filename", ""))
                        url = att.get("url", "")
                        content = att.get("content", "")
                    else:
                        name = getattr(att, "name", "") or getattr(att, "filename", "")
                        url = getattr(att, "url", "")
                        content = getattr(att, "content", "")

                    if name.lower().endswith(".ics") or "ics" in name.lower():
                        if content:
                            return content
                        if url:
                            # 下载文件
                            try:
                                import aiohttp
                                async with aiohttp.ClientSession() as session:
                                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                                        resp.raise_for_status()
                                        return await resp.text()
                            except Exception as e:
                                logger.error(f"下载 ICS 文件失败：{e}")
                                return None
        except Exception as e:
            logger.debug(f"提取附件失败：{e}")

        # 方式 2：消息本身就是 ICS 内容
        text = event.message_str.strip()
        if text.startswith("BEGIN:VCALENDAR"):
            return text

        return None
