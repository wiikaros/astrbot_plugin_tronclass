"""畅课点名 API 封装。"""

from typing import List

from .auth import TronClassClient
from ..config import ROLLCALL_STATUS_ATTENDED


async def fetch_rollcalls(client: TronClassClient) -> List[dict]:
    """获取当前活跃的点名列表。

    Args:
        client: 已登录的 TronClassClient 实例。

    Returns:
        点名列表，每项包含 id, course_title, status, rollcall_time 等；
        已签到的点名（on_call_fine）已被剔除（P1-6，黑名单策略）。
    """
    raw = await client.get_rollcalls()

    rollcalls = []
    for item in raw:
        rc = {
            "id": item.get("id"),
            "course_id": item.get("course_id"),
            "course_title": item.get("course_title", item.get("course_name", "未知课程")),
            "created_by_name": item.get("created_by_name", ""),
            "is_number": item.get("is_number", False),
            "number_code": item.get("number_code", ""),
            "status": item.get("status", "未知"),
            "source": item.get("source", ""),
            "rollcall_time": item.get("rollcall_time", ""),
        }
        rollcalls.append(rc)

    # P1-6：剔除已签到的点名。过滤基于同一列表，被剔除的 ID 不进 seen 集合、
    # 也不会被 detect_new_rollcalls 判为 new → 无重复推送风险。
    return [rc for rc in rollcalls if rc.get("status") != ROLLCALL_STATUS_ATTENDED]


def detect_new_rollcalls(
    current: List[dict],
    last_seen_ids: set,
) -> List[dict]:
    """检测新增的点名（与上次 ID 集合比对）。

    Args:
        current: 当前 API 返回的点名列表。
        last_seen_ids: 上次已见到的点名 ID 集合。

    Returns:
        新出现的点名列表。
    """
    new = []
    current_ids = set()

    for rc in current:
        rc_id = rc.get("id")
        if rc_id is not None:
            current_ids.add(rc_id)
            if rc_id not in last_seen_ids:
                new.append(rc)

    return new
