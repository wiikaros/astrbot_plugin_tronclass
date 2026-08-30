"""KV 存储统一封装。

为插件中的各类数据提供带命名空间的读写接口，
隔离不同用户的数据，隐藏原始 KV key 拼接细节。
"""

from pathlib import Path
from typing import Optional, List

from astrbot.api import logger
from astrbot.api.star import Star

from .session_cipher import SessionCipher
from ..config import (
    KV_SESSION_PREFIX,
    KV_SESSION_ORIGIN_PREFIX,
    KV_HOMEWORKS_PREFIX,
    KV_SCHEDULE_PREFIX,
    KV_ROLLCALL_SEEN_PREFIX,
    KV_LOGIN_STATE_PREFIX,
    KV_LAST_ROLLCALL_CHECK_PREFIX,
    KV_ALL_LOGGED_IN_USERS,
)

# Session 存储格式版本（v2 = Fernet 密文）
SESSION_ENC_VERSION = 2


class StorageService:
    """基于 AstrBot KV 存储的数据访问层。

    使用方式：
        storage = StorageService(plugin_instance)
        await storage.save_homeworks(user_id, data)
        homeworks = await storage.get_homeworks(user_id)

    Session 凭据（H4 修复）：save_session 自动加密，get_session 自动解密，
    并对旧版明文数据做一次性迁移（读明文 → 重写为密文）。
    """

    def __init__(self, plugin: Star, key_path: Path | None = None):
        self._plugin = plugin
        self._key_path = key_path
        self._cipher: SessionCipher | None = None

    def _get_cipher(self) -> SessionCipher:
        """懒加载 SessionCipher（默认密钥文件位于插件数据目录）。"""
        if self._cipher is None:
            if self._key_path is None:
                from astrbot.api.star import StarTools

                self._key_path = StarTools.get_data_dir() / ".session_key"
            self._cipher = SessionCipher(self._key_path)
        return self._cipher

    # ========== Session ==========

    async def save_session(self, user_id: str, data: dict) -> None:
        """保存用户 session（自动 Fernet 加密后落 KV）。"""
        encrypted = self._get_cipher().encrypt_dict(data)
        await self._plugin.put_kv_data(
            f"{KV_SESSION_PREFIX}:{user_id}",
            {"v": SESSION_ENC_VERSION, "data": encrypted},
        )

    async def get_session(self, user_id: str) -> Optional[dict]:
        """获取用户 session（自动解密；旧明文自动迁移为密文）。"""
        raw = await self._plugin.get_kv_data(f"{KV_SESSION_PREFIX}:{user_id}", default=None)
        if raw is None:
            return None

        # 新版密文格式
        if (
            isinstance(raw, dict)
            and raw.get("v") == SESSION_ENC_VERSION
            and isinstance(raw.get("data"), str)
        ):
            return self._get_cipher().decrypt_dict_or_none(raw["data"])

        # 旧版明文（dict 无 v 标记）→ 迁移为密文后返回明文给调用方
        if isinstance(raw, dict) and "data" not in raw:
            try:
                await self.save_session(user_id, raw)
            except Exception as e:
                logger.warning(f"旧明文 session 迁移加密失败 [{user_id}]：{e}")
            return raw

        # 无法识别的数据形态
        logger.warning(f"session 数据形态异常，已忽略 [{user_id}]")
        return None

    async def delete_session(self, user_id: str) -> None:
        """删除用户 session。"""
        await self._plugin.delete_kv_data(f"{KV_SESSION_PREFIX}:{user_id}")

    async def save_session_origin(self, user_id: str, origin: str) -> None:
        """记录用户的会话源（unified_msg_origin），供定时任务主动推送使用。"""
        if not origin:
            return
        await self._plugin.put_kv_data(f"{KV_SESSION_ORIGIN_PREFIX}:{user_id}", origin)

    async def get_session_origin(self, user_id: str) -> Optional[str]:
        """获取用户的会话源（unified_msg_origin）。"""
        origin = await self._plugin.get_kv_data(
            f"{KV_SESSION_ORIGIN_PREFIX}:{user_id}", default=None
        )
        return origin if isinstance(origin, str) else None

    async def get_all_session_user_ids(self) -> List[str]:
        """获取所有已登录用户的 user_id 列表。"""
        users = await self._plugin.get_kv_data(KV_ALL_LOGGED_IN_USERS, default=[])
        return users if isinstance(users, list) else []

    async def register_user(self, user_id: str) -> None:
        """登记已登录用户（幂等，重复调用不重复）。"""
        users = await self._plugin.get_kv_data(KV_ALL_LOGGED_IN_USERS, default=[])
        if not isinstance(users, list):
            users = []
        if user_id not in users:
            users.append(user_id)
            await self._plugin.put_kv_data(KV_ALL_LOGGED_IN_USERS, users)

    async def unregister_user(self, user_id: str) -> None:
        """从已登录列表移除用户（幂等）。"""
        users = await self._plugin.get_kv_data(KV_ALL_LOGGED_IN_USERS, default=[])
        if isinstance(users, list) and user_id in users:
            users.remove(user_id)
            await self._plugin.put_kv_data(KV_ALL_LOGGED_IN_USERS, users)

    # ========== 作业 ==========

    async def save_homeworks(self, user_id: str, data: List[dict]) -> None:
        """保存用户作业缓存。"""
        await self._plugin.put_kv_data(f"{KV_HOMEWORKS_PREFIX}:{user_id}", data)

    async def get_homeworks(self, user_id: str) -> List[dict]:
        """获取用户作业缓存。"""
        return await self._plugin.get_kv_data(f"{KV_HOMEWORKS_PREFIX}:{user_id}", default=[])

    # ========== 课表 ==========

    async def save_schedule(self, user_id: str, data: dict) -> None:
        """保存用户 ICS 课表。

        data 格式：
        {
            "semester_start": "2026-02-24",
            "courses": [
                {"name": "高数", "day": 1, "start": "08:00", "end": "09:40", "weeks": [1..16]},
                ...
            ]
        }
        """
        await self._plugin.put_kv_data(f"{KV_SCHEDULE_PREFIX}:{user_id}", data)

    async def get_schedule(self, user_id: str) -> Optional[dict]:
        """获取用户课表。"""
        return await self._plugin.get_kv_data(f"{KV_SCHEDULE_PREFIX}:{user_id}", default=None)

    # ========== 点名状态（去重） ==========

    async def get_rollcall_seen_ids(self, user_id: str) -> set:
        """获取用户上次见到的点名 ID 集合（M4：按用户独立 key，避免全量读改写）。"""
        ids = await self._plugin.get_kv_data(
            f"{KV_ROLLCALL_SEEN_PREFIX}:{user_id}", default=[]
        )
        if not isinstance(ids, list):
            return set()
        return set(ids)

    async def update_rollcall_seen_ids(self, user_id: str, ids: set) -> None:
        """更新用户已见到的点名 ID 集合（M4：按用户独立 key）。"""
        await self._plugin.put_kv_data(f"{KV_ROLLCALL_SEEN_PREFIX}:{user_id}", list(ids))

    # ========== 登录状态机 ==========

    async def save_login_state(self, user_id: str, state: dict) -> None:
        """保存登录状态机上下文。"""
        await self._plugin.put_kv_data(f"{KV_LOGIN_STATE_PREFIX}:{user_id}", state)

    async def get_login_state(self, user_id: str) -> Optional[dict]:
        """获取登录状态机上下文。"""
        return await self._plugin.get_kv_data(f"{KV_LOGIN_STATE_PREFIX}:{user_id}", default=None)

    async def delete_login_state(self, user_id: str) -> None:
        """清除登录状态机上下文。"""
        await self._plugin.delete_kv_data(f"{KV_LOGIN_STATE_PREFIX}:{user_id}")

    # ========== 点名时间追踪 ==========

    async def get_last_rollcall_time(self, user_id: str) -> int:
        """获取用户上次点名检测的时间戳。"""
        return await self._plugin.get_kv_data(
            f"{KV_LAST_ROLLCALL_CHECK_PREFIX}:{user_id}", default=0
        )

    async def set_last_rollcall_time(self, user_id: str, timestamp: int) -> None:
        """记录用户上次点名检测的时间戳。"""
        await self._plugin.put_kv_data(f"{KV_LAST_ROLLCALL_CHECK_PREFIX}:{user_id}", timestamp)
