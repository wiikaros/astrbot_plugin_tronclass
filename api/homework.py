"""畅课作业 API 封装。"""

from typing import List
from datetime import datetime, timedelta

from .auth import TronClassClient


async def fetch_homeworks(client: TronClassClient) -> List[dict]:
    """获取最新作业列表，从 homework-activities 补充截止时间和状态。

    /api/todos 只返回 id/title/course，不含 due_at 和 status。
    需要调 /api/courses/{course_id}/homework-activities 获取详情。
    """
    todos = await client.get_todos()

    # 收集所有涉及的 course_id，批量获取作业详情
    course_ids = set()
    for item in todos:
        cid = item.get("course_id")
        if cid:
            course_ids.add(cid)

    # 并行获取各课程的作业活动
    from astrbot.api import logger as _logger
    activities = {}
    for cid in course_ids:
        try:
            acts = await client.get_homework_activities(cid)
            _logger.info(f"course {cid}: {len(acts)} activities, keys={list(acts[0].keys()) if acts else 'EMPTY'}")
            for act in acts:
                activities[act.get("id")] = act
        except Exception as e:
            _logger.warning(f"course {cid} homework-activities 获取失败: {e}")

    homeworks = []
    for item in todos:
        hw_id = item.get("id")
        detail = activities.get(hw_id, {})

        # homework-activities 的字段名: deadline, submitted_status
        due_at = detail.get("deadline") or detail.get("end_time") or item.get("due_at", "")
        status_raw = detail.get("submitted_status") or item.get("status", "")
        if not status_raw and detail.get("submitted"):
            status_raw = "已提交"

        hw = {
            "id": hw_id,
            "title": item.get("title", "未命名"),
            "course_name": item.get("course_name", ""),
            "course_id": item.get("course_id"),
            "due_at": due_at,
            "status": status_raw if status_raw else "未知",
            "type": item.get("type", ""),
        }
        homeworks.append(hw)

    return homeworks


def diff_homeworks(
    cached: List[dict],
    fresh: List[dict],
) -> dict:
    """对比新旧作业列表。

    Args:
        cached: 本地缓存的作业列表。
        fresh: API 返回的最新作业列表。

    Returns:
        {
            "added": [...],     # 新作业
            "updated": [...],   # 字段变化的作业
            "removed": [...],   # 已完成的作业（从列表中移除）
            "unchanged": [...], # 无变化的作业
        }
    """
    cached_map = {h["id"]: h for h in cached if h.get("id") is not None}
    fresh_map = {h["id"]: h for h in fresh if h.get("id") is not None}

    added = []
    updated = []
    removed = []
    unchanged = []

    # 检查新增和更新
    for hw_id, fresh_hw in fresh_map.items():
        if hw_id not in cached_map:
            added.append(fresh_hw)
        else:
            cached_hw = cached_map[hw_id]
            # 比较关键字段
            if (
                fresh_hw.get("title") != cached_hw.get("title")
                or fresh_hw.get("due_at") != cached_hw.get("due_at")
                or fresh_hw.get("status") != cached_hw.get("status")
            ):
                updated.append(fresh_hw)
            else:
                unchanged.append(fresh_hw)

    # 检查移除（已完成）
    for hw_id, cached_hw in cached_map.items():
        if hw_id not in fresh_map:
            removed.append(cached_hw)

    return {
        "added": added,
        "updated": updated,
        "removed": removed,
        "unchanged": unchanged,
    }


def get_imminent_due(
    homeworks: List[dict],
    warn_hours: int = 24,
) -> List[dict]:
    """筛选快到期但未提交的作业。"""
    from ._utils import parse_datetime

    now = datetime.now()
    threshold = now + timedelta(hours=warn_hours)
    imminent = []

    for hw in homeworks:
        status = hw.get("status", "")
        if status in ("已提交", "已完成", "submitted", "graded"):
            continue

        due_str = hw.get("due_at", "")
        if not due_str:
            continue

        due_dt = parse_datetime(due_str)
        if due_dt and now < due_dt <= threshold:
            imminent.append(hw)

    imminent.sort(key=lambda h: h.get("due_at", ""))
    return imminent
