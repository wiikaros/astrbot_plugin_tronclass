"""AstrBot 畅课（TronClass）插件 — 入口模块。"""

import re

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, StarTools
from astrbot.api import logger, AstrBotConfig
from astrbot.core.utils.session_waiter import session_waiter, SessionController

from .config import (
    PLUGIN_NAME,
    KV_LOGIN_ATTEMPTS_PREFIX,
    DEFAULT_BASE_URL,
    DEFAULT_HOMEWORK_CHECK_INTERVAL,
    DEFAULT_ROLLCALL_DEFAULT_INTERVAL,
    DEFAULT_ROLLCALL_PRECHECK_MINUTES,
    DEFAULT_HOMEWORK_DUE_WARN_HOURS,
)
from .api.auth import TronClassClient, SessionInvalidError
from .api._utils import download_file_http
from .api.homework import fetch_homeworks, diff_homeworks, get_imminent_due
from .services.storage import StorageService
from .services.ics_parser import parse_ics
from .services.identity import get_user_key
from .services.notifier import format_homework_summary, _fmt_due
from .services.scheduler import SchedulerService
from .services.login_flow import LoginFlowManager


class TronClassPlugin(Star):
    """畅课助手插件：作业查询/提醒 + 点名实时通知。"""

    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config
        self._storage = StorageService(self)
        self._scheduler: SchedulerService | None = None
        self._login_flow = LoginFlowManager(self)
        logger.info("畅课助手插件已加载")

    async def terminate(self):
        """插件卸载/停用时调用。"""
        # 清理登录流程资源（后台轮询任务 + 登录中 ClientSession）
        await self._login_flow.close_all()
        # 注销定时任务，防止热重载后旧实例任务残留重复执行（框架合规 L1）
        if self._scheduler is not None:
            try:
                await self._scheduler.shutdown()
            except Exception as e:
                logger.warning(f"注销定时任务异常: {e}")
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
        """获取当前用户唯一标识（P0-3：platform_id:sender_id，全链路唯一入口）。"""
        return get_user_key(event)

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

        try:
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
        except Exception as e:
            # 定时任务注册失败不阻塞插件加载，仅降级（无自动通知）
            logger.error(f"定时任务初始化失败，自动通知功能将不可用：{e}")
            self._scheduler = None

        # 启动清扫：进程重启后内存登录会话已失（session_waiter 为内存态），
        # 残留的 login_state KV 无法续传，统一清理（不涉及 _login_attempts 频率限制）
        try:
            for uid in await self._storage.get_login_state_user_ids():
                await self._storage.delete_login_state(uid)
            await self._storage.clear_login_state_index()
        except Exception as e:
            logger.warning(f"登录状态启动清扫失败: {e}")

    # ========== 命令：/重置登录限制 ==========

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("重置登录限制")
    async def cmd_reset_login_limit(self, event: AstrMessageEvent, user_id: str = None):
        """管理员命令：重置用户的登录频率限制。"""

        # 如果未指定 user_id，则默认重置当前用户的限制
        if user_id is None:
            user_id = self._get_user_id(event)

        # 如果指定的 user_id 不存在
        if not await self.get_kv_data(f"{KV_LOGIN_ATTEMPTS_PREFIX}:{user_id}"):
            yield event.plain_result(f"⚠️ 用户 {user_id} 没有登录尝试记录，无需重置。")
            return
        await self.delete_kv_data(f"{KV_LOGIN_ATTEMPTS_PREFIX}:{user_id}")
        yield event.plain_result("✅ 登录限制已重置，可以重新登录了。")

    # ========== 命令：/微信登录 ==========

    @filter.command("微信登录")
    async def cmd_wechat_login(self, event: AstrMessageEvent):
        """微信扫码登录 —— 无需密码/验证码（逻辑见 services/login_flow.py）。"""
        async for result in self._login_flow.start_wechat_login(event):
            yield result

    # ========== 命令：/登录畅课 ==========

    @filter.command("登录畅课")
    async def cmd_login(self, event: AstrMessageEvent):
        """交互式登录畅课账号（状态机见 services/login_flow.py）。"""
        async for result in self._login_flow.start_password_login(event):
            yield result

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
            due = _fmt_due(hw.get("due_at", ""))
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
                due = _fmt_due(hw.get("due_at", ""))
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
            ics_dir = StarTools.get_data_dir(PLUGIN_NAME) / "ics"
            ics_dir.mkdir(parents=True, exist_ok=True)
            # L3：user_id 来自平台 sender_id，先 sanitize 再拼文件名，防路径穿越
            safe_id = re.sub(r"[^\w.-]", "_", user_id or "")
            ics_path = ics_dir / f"{safe_id}.ics"

            try:
                await download_file_http(file_url, ics_path)
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
