"""畅课作业 API 封装。"""

from typing import List, Optional

from api.auth import TronClassClient


async def fetch_homeworks(client: TronClassClient) -> List[dict]:
    """获取最新作业列表。

    Args:
        client: 已登录的 TronClassClient 实例。

    Returns:
        标准化后的作业列表，每项包含 id, title, course_name, due_at, status。
    """
    raw = await client.get_todos()

    homeworks = []
    for item in raw:
        hw = {
            "id": item.get("id"),
            "title": item.get("title", "未命名"),
            "course_name": item.get("course_name", ""),
            "course_id": item.get("course_id"),
            "due_at": item.get("due_at", ""),
            "status": item.get("status", "未知"),
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
    """筛选快到期但未提交的作业。

    Args:
        homeworks: 作业列表。
        warn_hours: 提前警告的小时数。

    Returns:
        快到期的作业列表。
    """
    from datetime import datetime, timedelta

    now = datetime.now()
    threshold = now + timedelta(hours=warn_hours)
    imminent = []

    for hw in homeworks:
        # 跳过已完成的
        status = hw.get("status", "")
        if status in ("已提交", "已完成", "submitted", "graded"):
            continue

        due_str = hw.get("due_at", "")
        if not due_str:
            continue

        try:
            # 尝试多种日期格式
            for fmt in (
                "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%dT%H:%M:%S.%f",
                "%Y-%m-%dT%H:%M:%SZ",
                "%Y-%m-%dT%H:%M:%S.%fZ",
                "%Y-%m-%d",
            ):
                try:
                    due_dt = datetime.strptime(due_str, fmt)
                    break
                except ValueError:
                    continue
            else:
                continue

            if now < due_dt <= threshold:
                imminent.append(hw)
        except Exception:
            continue

    imminent.sort(key=lambda h: h.get("due_at", ""))
    return imminent
