"""定时任务调度管理。

管理作业检测和点名检测的 Cron 任务，
支持 ICS 课表驱动的智能点名检测。
"""

import asyncio
import time
from datetime import datetime
from typing import Optional

from astrbot.api import logger
from astrbot.api.star import Context

from ..api.auth import (
    TronClassClient,
    check_session_valid,
    SessionInvalidError,
)
from ..api.homework import (
    fetch_homeworks,
    diff_homeworks,
    filter_notified_imminent,
)
from ..api.rollcall import fetch_rollcalls, detect_new_rollcalls
from .storage import StorageService
from .ics_parser import is_in_class_now, is_schedule_expired
from .identity import build_friend_origin, resolve_platform_id
from .notifier import format_multiple_homework_notifications, format_new_rollcall
from ..config import (
    PUSH_FAIL_THRESHOLD,
    PUSH_FAIL_NOTIFY_COOLDOWN,
    SCHEDULE_EXPIRED_NOTIFY_COOLDOWN,
    FETCH_FAIL_BACKOFF_BASE,
    FETCH_FAIL_BACKOFF_MAX,
    FETCH_FAIL_ALERT_THRESHOLD,
)


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
        homework_interval: int = 30,
        rollcall_default_interval: int = 5,
        precheck_minutes: int = 5,
        due_warn_hours: int = 24,
        enable_homework_notify: bool = True,
        enable_due_warning: bool = True,
        enable_rollcall_notify: bool = True,
        quiet_hours: dict | None = None,
    ):
        self._context = context
        self._storage = storage
        self._homework_interval = homework_interval
        self._rollcall_default_interval = rollcall_default_interval
        self._precheck_minutes = precheck_minutes
        self._due_warn_hours = due_warn_hours
        self._enable_homework_notify = enable_homework_notify
        self._enable_due_warning = enable_due_warning
        self._enable_rollcall_notify = enable_rollcall_notify
        self._quiet_hours = quiet_hours or {}

        # 拉取失败退避（P1-5）：{f"{user_id}:homework"/":rollcall": (连续失败数, 下次允许时间)}
        # 内存态：退避是短时保护，插件重启后归零重新探测更健康（与 _session_check_cache 同风格）
        self._fetch_failures: dict[str, tuple[int, float]] = {}

        # 作业检测 job ID
        self._homework_job_ids: list[str] = []
        # 点名检测 job ID
        self._rollcall_job_ids: list[str] = []
        # verify_session 结果缓存（M3）：{user_id: (ok, checked_at)}，避免每次 cron 触发都真实请求
        self._session_check_cache: dict[str, tuple[bool, float]] = {}

    # verify_session 结果缓存 TTL（秒）
    SESSION_CHECK_TTL = 600

    def _is_quiet_now(self, now: Optional[datetime] = None) -> bool:
        """当前是否处于免打扰时段（P1-4）。

        跨午夜判定（start > end 即跨午夜）；start == end 视为不启用。
        **fail-open**：未配置 / 时间解析失败一律返回 False（非静默）——
        宁可多推一条，不可漏推作业。本方法在 cron 入口调用、位于
        gather/_run 的 try 之外，**绝不能抛异常**，否则整轮报错、通知永久失效。
        """
        qh = self._quiet_hours
        if not qh or not qh.get("enabled"):
            return False
        try:
            start = str(qh.get("start", "")).strip()
            end = str(qh.get("end", "")).strip()
            s_h, s_m = map(int, start.split(":"))
            e_h, e_m = map(int, end.split(":"))
        except Exception:
            return False
        if not (0 <= s_h <= 23 and 0 <= s_m <= 59 and 0 <= e_h <= 23 and 0 <= e_m <= 59):
            return False  # 越界时间（如 25:00）视为坏输入 → 非静默
        now = now or datetime.now()
        now_min = now.hour * 60 + now.minute
        s = s_h * 60 + s_m
        e = e_h * 60 + e_m
        if s == e:
            return False
        if s < e:
            return s <= now_min < e
        return now_min >= s or now_min < e

    # ========== 拉取失败退避（P1-5） ==========

    @staticmethod
    def _fetch_key(user_id: str, kind: str) -> str:
        return f"{user_id}:{kind}"

    def _can_fetch(self, user_id: str, kind: str) -> bool:
        """退避判定：距上次失败是否已过退避期（在 _run 内、免打扰之后调用）。"""
        rec = self._fetch_failures.get(self._fetch_key(user_id, kind))
        if not rec:
            return True
        failures, next_ts = rec
        if time.time() >= next_ts:
            return True
        logger.debug(f"拉取退避中 [{user_id}/{kind}] 第 {failures} 次失败，跳过本轮")
        return False

    def _record_fetch_failure(self, user_id: str, kind: str) -> None:
        """记录一次拉取失败：连续失败数 +1，指数退避计算下次允许时间。

        必须在 _check_*_for_user 的**内层** except 中调用——内层已吞掉异常，
        外层 _run 捕获不到，放外层等于从未触发。
        """
        key = self._fetch_key(user_id, kind)
        failures = self._fetch_failures.get(key, (0, 0.0))[0] + 1
        delay = min(
            FETCH_FAIL_BACKOFF_BASE * (2 ** (failures - 1)),
            FETCH_FAIL_BACKOFF_MAX,
        )
        self._fetch_failures[key] = (failures, time.time() + delay)
        if failures >= FETCH_FAIL_ALERT_THRESHOLD:
            logger.error(
                f"拉取持续失败 [{user_id}/{kind}]：连续 {failures} 次，退避 {delay}s"
            )

    def _clear_fetch_failure(self, user_id: str, kind: str) -> None:
        """拉取成功后归零退避计数。"""
        self._fetch_failures.pop(self._fetch_key(user_id, kind), None)

    async def setup(self):
        """首次启动时注册所有定时任务。"""
        await self._schedule_homework_check()
        await self._schedule_rollcall_check()
        logger.info(
            f"定时任务已注册：作业检测每 {self._homework_interval} 分钟，"
            f"点名检测每 {self._rollcall_default_interval} 分钟"
        )

    async def shutdown(self):
        """插件卸载/重载时注销全部定时任务。

        修复框架合规 L1：cron_manager 是全局注册表，若不注销，
        插件热重载后旧实例的 persistent=False 任务会残留并重复执行
        （handler 仍引用旧实例）。本方法幂等，重复调用安全。
        """
        cron = getattr(self._context, "cron_manager", None)
        if cron is None:
            return
        for job_id in list(self._homework_job_ids) + list(self._rollcall_job_ids):
            try:
                await cron.delete_job(job_id)
            except Exception as e:
                logger.warning(f"注销定时任务失败 job_id={job_id}: {e}")
        self._homework_job_ids.clear()
        self._rollcall_job_ids.clear()

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
        """为用户创建已认证的 API 客户端，自动检查过期（带 TTL 缓存）。"""
        session_data = await self._storage.get_session(user_id)
        if session_data is None:
            return None
        client = TronClassClient.from_session_data(session_data)
        # 注入写回：服务器每次响应滚动续期 session/role_token（并回传 x-session-id），
        # 由 client 在请求后经节流把最新凭证持久化，保证跨 tick 不退回旧 cookie
        client.attach_session_persister(
            lambda data: self._storage.save_session(user_id, data)
        )
        if not await self._check_session_valid_cached(user_id, client):
            logger.info(f"Session 已过期 [{user_id}]，清理并通知用户")
            await self._storage.delete_session(user_id)
            await self._storage.unregister_user(user_id)  # 保持注册表与真实 session 一致
            # P1-5：用户 session 失效 → 清理其退避计数（防内存泄漏）
            self._fetch_failures.pop(f"{user_id}:homework", None)
            self._fetch_failures.pop(f"{user_id}:rollcall", None)
            await client.close()
            # 通知用户重新登录
            try:
                await self._send_private_notification(
                    user_id,
                    "⚠️ 你的畅课登录已过期，请重新发送 /微信登录 或 /登录畅课"
                )
            except Exception:
                pass
            return None
        return client

    async def _check_session_valid_cached(self, user_id: str, client: TronClassClient) -> bool:
        """verify_session 结果 TTL 缓存（M3）。

        - 命中缓存（10 分钟内）直接返回，避免重复真实 API 自检；
        - `is_expired`（JWT exp）由 check_session_valid 内部短路，仍优先；
        - 缓存失败结果同样 10 分钟，但失败路径会清理 session/注册表，后续不再触发。
        """
        cached = self._session_check_cache.get(user_id)
        now = time.time()
        if cached and now - cached[1] < self.SESSION_CHECK_TTL:
            return cached[0]
        ok = await check_session_valid(client)
        self._session_check_cache[user_id] = (ok, now)
        return ok

    # ========== 作业检测 ==========

    async def check_homeworks(self, event=None, payload=None):
        """遍历所有已登录用户，检测作业更新并推送通知。

        Args:
            event: AstrBot Cron 任务触发时传入的 CronMessageEvent（可选）。
            payload: Cron 任务的自定义 payload（可选）。
        """
        if self._is_quiet_now():
            return  # P1-4 免打扰：整轮跳过（不发请求不推送），diff/seen 在结束后自动补漏

        if not self._enable_homework_notify and not self._enable_due_warning:
            return

        user_ids = await self._storage.get_all_session_user_ids()
        if not user_ids:
            return

        logger.debug(f"作业定时检测：{len(user_ids)} 个用户")

        # M2 优化：用户间并发处理（Semaphore 限流），单用户异常不中断整轮
        sem = asyncio.Semaphore(5)

        async def _run(uid: str):
            async with sem:
                if not self._can_fetch(uid, "homework"):
                    return  # P1-5 退避中
                try:
                    await self._check_homeworks_for_user(uid)
                except Exception as e:
                    logger.error(f"作业检测失败 [{uid}]：{e}")

        await asyncio.gather(*(_run(uid) for uid in user_ids), return_exceptions=True)

    async def _check_homeworks_for_user(self, user_id: str):
        """为单个用户检测作业更新（client 生命周期由 finally 统一收口）。"""
        client = await self._get_client_for_user(user_id)
        if client is None:
            return
        try:
            fresh = await fetch_homeworks(client)
            self._clear_fetch_failure(user_id, "homework")

            cached = await self._storage.get_homeworks(user_id)
            diff = diff_homeworks(cached, fresh)
            await self._storage.save_homeworks(user_id, fresh)

            # 检查快到期（P0-1：分级去重，24h/6h/1h 各推一次）
            imminent = []
            if self._enable_due_warning:
                notified = await self._storage.get_due_notified(user_id)
                to_notify, new_notified = filter_notified_imminent(
                    fresh, self._due_warn_hours, notified
                )
                await self._storage.save_due_notified(user_id, new_notified)
                imminent = to_notify

            added = diff["added"] if self._enable_homework_notify else []

            # 没有变化则静默
            if not added and not imminent:
                return

            # 生成通知
            messages = format_multiple_homework_notifications(added, imminent)
            for msg in messages:
                try:
                    await self._send_private_notification(user_id, msg)
                except Exception as e:
                    logger.error(f"推送作业通知失败 [{user_id}]：{e}")
        except SessionInvalidError as e:
            # P1-5：session 失效不计退避——用户需要的是重新登录，退避只会延迟发现
            self._clear_fetch_failure(user_id, "homework")
            logger.info(f"作业检测 session 失效 [{user_id}]：{e}")
        except Exception as e:
            logger.warning(f"获取作业列表失败 [{user_id}]：{e}")
            self._record_fetch_failure(user_id, "homework")
        finally:
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
        if self._is_quiet_now():
            return  # P1-4 免打扰：整轮跳过（不发请求不推送），seen 在结束后自动补漏

        if not self._enable_rollcall_notify:
            return

        user_ids = await self._storage.get_all_session_user_ids()
        if not user_ids:
            return

        # M2 优化：用户间并发处理（Semaphore 限流），单用户异常不中断整轮
        sem = asyncio.Semaphore(5)

        async def _run(uid: str):
            async with sem:
                if not self._can_fetch(uid, "rollcall"):
                    return  # P1-5 退避中
                try:
                    await self._check_rollcalls_for_user(uid)
                except Exception as e:
                    logger.error(f"点名检测失败 [{uid}]：{e}")

        await asyncio.gather(*(_run(uid) for uid in user_ids), return_exceptions=True)

    async def _check_rollcalls_for_user(self, user_id: str):
        """为单个用户检测点名更新（client 生命周期由 finally 统一收口）。

        P1-1 分支语义：课表有效且在上课 → ICS 驱动；课表过期/损坏/无课表 →
        统一回退默认间隔轮询（否则学期结束后 is_in_class_now 恒 False，
        点名检测会永久停摆且不回退、无提示）。
        """
        schedule = await self._storage.get_schedule(user_id)
        if not isinstance(schedule, dict) or not schedule.get("courses"):
            schedule = None  # 脏数据守卫：按无课表处理（镜像 main.py /我的状态 写法）

        use_ics = False
        if schedule:
            if is_schedule_expired(schedule):
                # 课表已过期：提醒 + 回退默认轮询
                await self._notify_schedule_expired(user_id)
            elif not is_in_class_now(schedule, self._precheck_minutes):
                return  # 正常的不在上课时间
            else:
                use_ics = True

        if not use_ics:
            # 课表过期 / 损坏 / 无课表 → 检查默认间隔
            if not await self._should_check_rollcall_by_default(user_id):
                return

        client = await self._get_client_for_user(user_id)
        if client is None:
            return
        try:
            current = await fetch_rollcalls(client)
            self._clear_fetch_failure(user_id, "rollcall")
            if not current:
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
        except SessionInvalidError as e:
            # P1-5：session 失效不计退避——用户需要的是重新登录，退避只会延迟发现
            self._clear_fetch_failure(user_id, "rollcall")
            logger.info(f"点名检测 session 失效 [{user_id}]：{e}")
        except Exception as e:
            logger.warning(f"获取点名列表失败 [{user_id}]：{e}")
            self._record_fetch_failure(user_id, "rollcall")
        finally:
            await client.close()

    async def _should_check_rollcall_by_default(self, user_id: str) -> bool:
        """检查是否到达无课表时的默认点名检测间隔。"""
        now = int(time.time())
        interval_seconds = self._rollcall_default_interval * 60
        last_check = await self._storage.get_last_rollcall_time(user_id)

        if now - last_check >= interval_seconds:
            await self._storage.set_last_rollcall_time(user_id, now)
            return True

        return False

    async def _notify_schedule_expired(self, user_id: str):
        """课表过期提醒（P1-1，24h 冷却，避免每分钟轮询都推）。"""
        last = await self._storage.get_schedule_expired_notified(user_id)
        if time.time() - last < SCHEDULE_EXPIRED_NOTIFY_COOLDOWN:
            return
        await self._storage.mark_schedule_expired_notified(user_id)
        try:
            await self._send_private_notification(
                user_id,
                "📅 你的课表已过期（本学期课程已结束），点名检测已回退为定时轮询。\n"
                "请重新发送 /上传课表，或发送 /删除课表 停止提醒。",
            )
        except Exception as e:
            logger.error(f"课表过期提醒发送失败 [{user_id}]：{e}")

    # ========== 通知发送 ==========

    async def _send_private_notification(self, user_id: str, message: str):
        """给指定用户发送私聊通知（P0-2/P0-4 修复：不存群会话、按 name 自愈）。

        推送目标 = 登录时保存的 {platform_name, platform_id, session_id}，
        推送时按平台类型名重解析当前适配器实例 id（适配器重建后自愈），
        并以 FriendMessage 私聊会话发送，杜绝推送到群的可能。

        失败可观测（P0-4）：无目标/解析失败/发送失败/未送达均计入失败计数，
        连续失败达阈值且超过冷却期 → 主动提示用户重新登录，不再静默。
        """
        origin = await self._storage.get_session_origin(user_id)
        if not origin:
            logger.warning(
                f"用户 {user_id} 无推送目标记录（origin），无法推送定时通知，请重新登录"
            )
            await self._record_push_failure(user_id)
            return

        platform_id = resolve_platform_id(
            self._context,
            origin.get("platform_name", ""),
            origin.get("platform_id", ""),
        )
        if not platform_id:
            logger.warning(
                f"用户 {user_id} 平台解析失败（name={origin.get('platform_name')!r}），无法推送"
            )
            await self._record_push_failure(user_id)
            return

        from astrbot.api.event import MessageChain
        from astrbot.api.message_components import Plain

        target = build_friend_origin(platform_id, origin.get("session_id", ""))
        try:
            ok = await self._context.send_message(
                target, MessageChain([Plain(message)])
            )
        except Exception as e:
            logger.error(f"发送私聊消息失败 [{user_id}]：{e}")
            await self._record_push_failure(user_id)
            return
        if not ok:
            # 平台不匹配：send_message 返回 False 而非抛异常（context.py:471-478）
            logger.error(
                f"发送私聊消息未送达 [{user_id}]：平台未匹配目标 {target}"
            )
            await self._record_push_failure(user_id)
            return
        await self._storage.clear_push_failure(user_id)

    async def _record_push_failure(self, user_id: str):
        """推送失败计数 + 阈值触发一次性用户提示（P0-4）。"""
        await self._storage.record_push_failure(user_id)
        rec = await self._storage.get_push_failure(user_id)
        now = time.time()
        if rec.get("count", 0) < PUSH_FAIL_THRESHOLD:
            return
        if now - rec.get("last_notified_at", 0) < PUSH_FAIL_NOTIFY_COOLDOWN:
            return
        await self._storage.mark_push_fail_notified(user_id)
        try:
            origin = await self._storage.get_session_origin(user_id)
            if origin:
                pid = resolve_platform_id(
                    self._context,
                    origin.get("platform_name", ""),
                    origin.get("platform_id", ""),
                )
                if pid:
                    target = build_friend_origin(pid, origin.get("session_id", ""))
                    from astrbot.api.event import MessageChain
                    from astrbot.api.message_components import Plain

                    await self._context.send_message(
                        target,
                        MessageChain(
                            [Plain("⚠️ 定时推送多次失败，请重新登录，或发送 /我的状态 检查。")]
                        ),
                    )
        except Exception as e:
            logger.warning(f"推送失败提示发送失败 [{user_id}]：{e}")
