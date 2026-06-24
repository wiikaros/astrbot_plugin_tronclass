"""KV 存储统一封装。

为插件中的各类数据提供带命名空间的读写接口，
隔离不同用户的数据，隐藏原始 KV key 拼接细节。
"""

from typing import Optional, List, Dict, Any

from astrbot.api.star import Star


class StorageService:
    """基于 AstrBot KV 存储的数据访问层。

    使用方式：
        storage = StorageService(plugin_instance)
        await storage.save_homeworks(user_id, data)
        homeworks = await storage.get_homeworks(user_id)
    """

    def __init__(self, plugin: Star):
        self._plugin = plugin

    # ========== Session ==========

    async def save_session(self, user_id: str, data: dict) -> None:
        """保存用户 session。"""
        await self._plugin.put_kv_data(f"session:{user_id}", data)

    async def get_session(self, user_id: str) -> Optional[dict]:
        """获取用户 session。"""
        return await self._plugin.get_kv_data(f"session:{user_id}", default=None)

    async def delete_session(self, user_id: str) -> None:
        """删除用户 session。"""
        await self._plugin.delete_kv_data(f"session:{user_id}")

    async def get_all_session_user_ids(self) -> List[str]:
        """获取所有已登录用户的 user_id 列表。

        注意：这是一个简化实现，通过扫描已知 key 前缀来收集。
        实际运行时，也可以通过维护一个额外的全局 key 来追踪。
        """
        # 通过 _all_logged_in_users 全局 key 获取
        users = await self._plugin.get_kv_data("_all_logged_in_users", default=[])
        return users if users else []

    async def _mark_user_logged_in(self, user_id: str) -> None:
        """标记用户为已登录状态。"""
        users = await self.get_all_session_user_ids()
        if user_id not in users:
            users.append(user_id)
            await self._plugin.put_kv_data("_all_logged_in_users", users)

    async def _mark_user_logged_out(self, user_id: str) -> None:
        """从已登录列表移除用户。"""
        users = await self.get_all_session_user_ids()
        if user_id in users:
            users.remove(user_id)
            await self._plugin.put_kv_data("_all_logged_in_users", users)

    # ========== 作业 ==========

    async def save_homeworks(self, user_id: str, data: List[dict]) -> None:
        """保存用户作业缓存。"""
        await self._plugin.put_kv_data(f"homeworks:{user_id}", data)

    async def get_homeworks(self, user_id: str) -> List[dict]:
        """获取用户作业缓存。"""
        return await self._plugin.get_kv_data(f"homeworks:{user_id}", default=[])

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
        await self._plugin.put_kv_data(f"schedule:{user_id}", data)

    async def get_schedule(self, user_id: str) -> Optional[dict]:
        """获取用户课表。"""
        return await self._plugin.get_kv_data(f"schedule:{user_id}", default=None)

    # ========== 点名状态（去重） ==========

    async def get_rollcall_seen_ids(self, user_id: str) -> set:
        """获取用户上次见到的点名 ID 集合。"""
        states = await self._plugin.get_kv_data("rollcall_states", default={})
        ids = states.get(user_id, [])
        return set(ids)

    async def update_rollcall_seen_ids(self, user_id: str, ids: set) -> None:
        """更新用户已见到的点名 ID 集合。"""
        states = await self._plugin.get_kv_data("rollcall_states", default={})
        states[user_id] = list(ids)
        await self._plugin.put_kv_data("rollcall_states", states)

    # ========== 登录状态机 ==========

    async def save_login_state(self, user_id: str, state: dict) -> None:
        """保存登录状态机上下文。"""
        await self._plugin.put_kv_data(f"login_state:{user_id}", state)

    async def get_login_state(self, user_id: str) -> Optional[dict]:
        """获取登录状态机上下文。"""
        return await self._plugin.get_kv_data(f"login_state:{user_id}", default=None)

    async def delete_login_state(self, user_id: str) -> None:
        """清除登录状态机上下文。"""
        await self._plugin.delete_kv_data(f"login_state:{user_id}")
