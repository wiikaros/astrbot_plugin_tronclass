"""内部工具函数，供 api/ 层各模块共享。"""

import base64
import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo

# 部署环境统一按东八区解释（UTC 输入 → 本地 naive）
LOCAL_TZ = ZoneInfo("Asia/Shanghai")


def decode_jwt_expiry(jwt_token: str) -> float:
    """从 JWT token 解码 exp 字段，返回过期时间戳。

    Args:
        jwt_token: JWT 字符串（如 role_token）。

    Returns:
        exp 时间戳（秒），解码失败时返回 time.time() + 3600（默认 1 小时）。
    """
    try:
        payload = jwt_token.split(".")[1]
        payload += "=" * (4 - len(payload) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(payload))
        if "exp" in decoded:
            return float(decoded["exp"])
    except Exception:
        pass
    return time.time() + 3600


DATETIME_FORMATS = (
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%dT%H:%M:%S.%fZ",
    "%Y-%m-%d",
)


def _to_local_naive(dt: datetime) -> datetime:
    """aware datetime → 东八区本地 naive；naive 输入原样返回。

    统一返回 naive 是为了与 `datetime.now()` 直接比较（调用方零改动）。
    """
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(LOCAL_TZ).replace(tzinfo=None)


def parse_datetime(s: str) -> datetime | None:
    """尝试多种格式解析 datetime 字符串。

    时区语义（H2 修复）：
    - 带时区（`Z` / `+08:00` / `+00:00` 等）的输入先按 ISO 解析，
      再统一转换为东八区本地 naive 时间，保证与 `datetime.now()` 可比；
    - 无时区输入保持原样（naive）。

    Returns:
        datetime 对象（naive），全部格式都不匹配时返回 None。
    """
    if not s:
        return None
    # 优先 fromisoformat（Python 3.11+ 支持 'Z' 后缀与 ±HH:MM 偏移）
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        dt = None
    if dt is not None:
        return _to_local_naive(dt)
    # 兜底：旧版 strptime 格式（兼容 fromisoformat 之外的写法）
    for fmt in DATETIME_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None
