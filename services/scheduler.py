"""定时任务调度管理。

管理作业检测和点名检测的 Cron 任务，
支持 ICS 课表驱动的智能点名检测。
"""

import asyncio
import time
from typing import Optional, Set

from astrbot.api import logger
from astrbot.api.star import Context

from api.auth import TronClassClient
from api.homework import fetch_homeworks, diff_homeworks, get_imminent_due
from api.rollcall import fetch_rollcalls, detect_new_rollcalls
from services.storage import StorageService
from services.ics_parser import is_in_class_now
from services.notifier import format_multiple_homework_notifications, format_new_rollcall


class SchedulerService:
    """插件定时任务管理器。

    负责：
    - 作业定时检测（固定间隔）
    - 点名定时检测（ICS 驱动 / 固定间隔回退）
    - 通知推送
    """

    def __init__(
        self,
        context: Context,
        storage: StorageService,
        base_url: str,
        homework_interval: int = 30,
        rollcall_default_interval: int = 5,
        precheck_minutes: int = 5,
        due_warn_hours: int = 24,
        enable_homework_notify: bool = True,
        enable_due_warning: bool = True,
        enable_rollcall_notify: bool = True,
    ):
        self._context = context
        self._storage = storage
        self._base_url = base_url
        self._homework_interval = homework_interval
        self._rollcall_default_interval = rollcall_default_interval
        self._precheck_minutes = precheck_minutes
        self._due_warn_hours = due_warn_hours
        self._enable_homework_notify = enable_homework_notify
        self._enable_due_warning = enable_due_warning
        self._enable_rollcall_notify = enable_rollcall_notify

        # 作业检测 job ID
        self._homework_job_ids: list[str] = []
        # 点名检测 job ID
        self._rollcall_job_ids: list[str] = []

    async def setup(self):
        """首次启动时注册所有定时任务。"""
        await self._schedule_homework_check()
        await self._schedule_rollcall_check()
        logger.info(
            f"定时任务已注册：作业检测每 {self._homework_interval} 分钟，"
            f"点名检测每 {self._rollcall_default_interval} 分钟"
        )

    async def _schedule_homework_check(self):
        """注册作业定时检测任务。"""
        job = await self._context.cron_manager.add_basic_job(
            name="tronclass_homework_check",
            cron_expression=f"*/{self._homework_interval} * * * *",
            handler=self.check_homeworks,
            persistent=False,
            enabled=True,
        )
        if job:
            self._homework_job_ids.append(job.job_id)

    async def _schedule_rollcall_check(self):
        """注册点名定时检测任务（每分钟触发一次，内部判断是否真正检测）。"""
        job = await self._context.cron_manager.add_basic_job(
            name="tronclass_rollcall_check",
            cron_expression="* * * * *",  # 每分钟
            handler=self.check_rollcalls,
            persistent=False,
            enabled=True,
        )
        if job:
            self._rollcall_job_ids.append(job.job_id)

    # ========== 帮助方法 ==========

    async def _get_client_for_user(self, user_id: str) -> Optional[TronClassClient]:
        """为用户创建已认证的 API 客户端。"""
        session_data = await self._storage.get_session(user_id)
        if session_data is None:
            return None
        return TronClassClient.from_session_data(session_data)

    # ========== 作业检测 ==========

    async def check_homeworks(self, event=None, payload=None):
        """遍历所有已登录用户，检测作业更新并推送通知。

        Args:
            event: AstrBot Cron 任务触发时传入的 CronMessageEvent（可选）。
            payload: Cron 任务的自定义 payload（可选）。
        """
        if not self._enable_homework_notify and not self._enable_due_warning:
            return

        user_ids = await self._storage.get_all_session_user_ids()
        if not user_ids:
            return

        logger.debug(f"作业定时检测：{len(user_ids)} 个用户")

        for user_id in user_ids:
            try:
                await self._check_homeworks_for_user(user_id)
            except Exception as e:
                logger.error(f"作业检测失败 [{user_id}]：{e}")

    async def _check_homeworks_for_user(self, user_id: str):
        """为单个用户检测作业更新。"""
        client = await self._get_client_for_user(user_id)
        if client is None:
            return

        try:
            fresh = await fetch_homeworks(client)
        except Exception as e:
            await client.close()
            logger.warning(f"获取作业列表失败 [{user_id}]：{e}")
            return

        cached = await self._storage.get_homeworks(user_id)

        diff = diff_homeworks(cached, fresh)

        # 保存最新数据
        await self._storage.save_homeworks(user_id, fresh)

        # 检查快到期
        imminent = []
        if self._enable_due_warning:
            imminent = get_imminent_due(fresh, self._due_warn_hours)

        added = diff["added"] if self._enable_homework_notify else []

        # 没有变化则静默
        if not added and not imminent:
            await client.close()
            return

        # 生成通知
        messages = format_multiple_homework_notifications(added, imminent)

        for msg in messages:
            try:
                await self._send_private_notification(user_id, msg)
            except Exception as e:
                logger.error(f"推送作业通知失败 [{user_id}]：{e}")

        await client.close()

    # ========== 点名检测 ==========

    async def check_rollcalls(self, event=None, payload=None):
        """点名定时检测（每分钟触发）。

        对每个用户判断是否应该检测：
        - 有课表 → is_in_class_now() 判断
        - 无课表 → 检查距离上次检测的时间

        Args:
            event: AstrBot Cron 任务触发时传入的 CronMessageEvent（可选）。
            payload: Cron 任务的自定义 payload（可选）。
        """
        if not self._enable_rollcall_notify:
            return

        user_ids = await self._storage.get_all_session_user_ids()
        if not user_ids:
            return

        for user_id in user_ids:
            try:
                await self._check_rollcalls_for_user(user_id)
            except Exception as e:
                logger.error(f"点名检测失败 [{user_id}]：{e}")

    async def _check_rollcalls_for_user(self, user_id: str):
        """为单个用户检测点名更新。"""
        schedule = await self._storage.get_schedule(user_id)

        if schedule:
            # ICS 驱动
            if not is_in_class_now(schedule, self._precheck_minutes):
                return  # 不在上课时间，跳过
        else:
            # 无课表 → 检查默认间隔
            if not await self._should_check_rollcall_by_default(user_id):
                return

        client = await self._get_client_for_user(user_id)
        if client is None:
            return

        try:
            current = await fetch_rollcalls(client)
        except Exception as e:
            await client.close()
            logger.warning(f"获取点名列表失败 [{user_id}]：{e}")
            return

        if not current:
            await client.close()
            return

        # 检测新点名
        last_seen = await self._storage.get_rollcall_seen_ids(user_id)
        new_rollcalls = detect_new_rollcalls(current, last_seen)

        # 更新已见 ID
        current_ids = {rc.get("id") for rc in current if rc.get("id") is not None}
        await self._storage.update_rollcall_seen_ids(user_id, current_ids)

        # 推送通知
        for rc in new_rollcalls:
            msg = format_new_rollcall(rc)
            try:
                await self._send_private_notification(user_id, msg)
            except Exception as e:
                logger.error(f"推送点名通知失败 [{user_id}]：{e}")

        await client.close()

    async def _should_check_rollcall_by_default(self, user_id: str) -> bool:
        """检查是否到达无课表时的默认点名检测间隔。"""
        # 简化为：每分钟 Cron 触发，根据是否到达间隔决定
        # 使用当前分钟数判断
        now = int(time.time())
        interval_seconds = self._rollcall_default_interval * 60

        # 使用带用户粒度的分钟数对齐（不同用户分片检测）
        key = f"_last_rollcall_check:{user_id}"
        last_check = await self._storage._plugin.get_kv_data(key, default=0)

        if now - last_check >= interval_seconds:
            await self._storage._plugin.put_kv_data(key, now)
            return True

        return False

    # ========== 通知发送 ==========

    async def _send_private_notification(self, user_id: str, message: str):
        """给指定用户发送私聊通知。

        通过 AstrBot 的 platform_manager 直接发送私聊消息。
        user_id 的格式取决于平台：
        - QQ: 用户 QQ 号
        - Telegram: 用户 Telegram ID
        """
        try:
            # 通过 platform_manager 获取已激活的适配器并发送
            adapters = self._context.platform_manager.get_adapters()
            for adapter in adapters:
                try:
                    await adapter.send_private_message(user_id, message)
                    return
                except Exception:
                    continue

            logger.warning(
                f"无法发送私聊消息 [{user_id}]：没有可用的平台适配器"
            )
        except Exception as e:
            logger.error(f"发送私聊消息失败 [{user_id}]：{e}")
            raise
