"""内部工具函数，供 api/ 层各模块共享。"""

import base64
import json
import time
from datetime import datetime, timedelta


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


def parse_datetime(s: str) -> datetime | None:
    """尝试多种格式解析 datetime 字符串。

    Returns:
        datetime 对象，全部格式都不匹配时返回 None。
    """
    for fmt in DATETIME_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None
