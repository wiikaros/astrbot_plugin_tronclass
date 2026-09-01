"""KV 存量数据迁移（P0-3/P0-4，一次性、幂等）。

迁移内容：
1. **user_id 语义改版**：裸 `sender_id` → `platform_id:sender_id`
   （仅单平台场景执行，多平台无法推断归属，宁可不迁不误迁）；
2. **session_origin 格式改版**：旧 str（unified_msg_origin）→ 结构化 dict；
   `GroupMessage` 条目为 P0-2 污染数据，**一律丢弃**。

安全约定：
- 先写新 key → 读回校验 → 再删旧 key；任一环节异常则该用户整体跳过（旧数据保留），
  不中断其他用户，下次启动自愈重试；
- 迁移失败的用户保留旧 uid 在注册表中（登录态失效 → 提示重新登录，不丢数据）。
"""

from astrbot.api import logger

from ..config import (
    KV_SESSION_PREFIX,
    KV_SESSION_ORIGIN_PREFIX,
    KV_HOMEWORKS_PREFIX,
    KV_SCHEDULE_PREFIX,
    KV_ROLLCALL_SEEN_PREFIX,
    KV_LAST_ROLLCALL_CHECK_PREFIX,
    KV_LOGIN_ATTEMPTS_PREFIX,
    KV_ALL_LOGGED_IN_USERS,
)

# 旧裸 sender_id 需要迁移的数据 key 前缀（origin 单独处理）
_LEGACY_KEY_PREFIXES = (
    KV_SESSION_PREFIX,
    KV_HOMEWORKS_PREFIX,
    KV_SCHEDULE_PREFIX,
    KV_ROLLCALL_SEEN_PREFIX,
    KV_LAST_ROLLCALL_CHECK_PREFIX,
    KV_LOGIN_ATTEMPTS_PREFIX,
)


def _parse_legacy_origin(origin: str):
    """解析旧 unified_msg_origin（`platform_id:message_type:session_id`）。

    Returns:
        (platform_id, session_id)；格式非法或非 FriendMessage（群聊污染）返回 None。
    """
    try:
        pid, mtype, sid = origin.split(":", 2)
    except (ValueError, AttributeError):
        return None
    if mtype != "FriendMessage":
        return None
    return pid, sid


async def migrate_legacy_kv(plugin) -> dict:
    """执行存量迁移（幂等，可重复调用）。

    Args:
        plugin: TronClassPlugin 实例。

    Returns:
        {"migrated": int, "dropped_origin": int, "failed": int, "skipped": bool}
    """
    result = {"migrated": 0, "dropped_origin": 0, "failed": 0, "skipped": False}

    # 1) 平台判定：仅单平台场景可迁移
    instances = []
    try:
        pm = getattr(plugin.context, "platform_manager", None)
        instances = list(getattr(pm, "platform_insts", None) or [])
    except Exception as e:
        logger.warning(f"[migration] 读取平台实例列表失败，跳过迁移: {e}")
        result["skipped"] = True
        return result
    if len(instances) != 1:
        logger.warning(f"[migration] 平台实例数 {len(instances)} ≠ 1，跳过迁移（防误迁）")
        result["skipped"] = True
        return result
    try:
        platform_id = instances[0].meta().id
        platform_name = instances[0].meta().name
    except Exception as e:
        logger.warning(f"[migration] 读取平台元数据失败，跳过迁移: {e}")
        result["skipped"] = True
        return result

    legacy_users = await plugin.get_kv_data(KV_ALL_LOGGED_IN_USERS, default=[])
    if not isinstance(legacy_users, list) or not legacy_users:
        return result  # 无旧数据，正常路径

    new_registry = []
    for sender_id in legacy_users:
        if not isinstance(sender_id, str):
            continue
        if ":" in sender_id:
            # 已是新格式（理论不会出现），保留
            new_registry.append(sender_id)
            continue

        new_uid = f"{platform_id}:{sender_id}"
        # 幂等：新 key 已存在 → 视为已迁移（可能上次写了一半），补齐收尾
        try:
            existing = await plugin.get_kv_data(
                f"{KV_SESSION_PREFIX}:{new_uid}", default=None
            )
            if existing is None:
                await _copy_keys(plugin, sender_id, new_uid)
            await _migrate_origin(plugin, sender_id, new_uid, platform_name, platform_id, result)
            # 读回校验最关键数据（session）
            verify = await plugin.get_kv_data(f"{KV_SESSION_PREFIX}:{new_uid}", default=None)
            if verify is None:
                raise RuntimeError("新 session key 校验失败")
            new_registry.append(new_uid)
            result["migrated"] += 1
            # 删除旧 key
            for prefix in _LEGACY_KEY_PREFIXES:
                await plugin.delete_kv_data(f"{prefix}:{sender_id}")
            await plugin.delete_kv_data(f"{KV_SESSION_ORIGIN_PREFIX}:{sender_id}")
        except Exception as e:
            result["failed"] += 1
            logger.warning(f"[migration] 迁移用户 {sender_id} 失败（保留旧数据待重试）: {e}")
            new_registry.append(sender_id)  # 保底保留旧 uid，防止注册表丢用户

    await plugin.put_kv_data(KV_ALL_LOGGED_IN_USERS, new_registry)
    logger.info(
        f"[migration] 完成：迁移 {result['migrated']} 人，"
        f"丢弃 {result['dropped_origin']} 条群聊 origin，失败 {result['failed']} 人"
    )
    return result


async def _copy_keys(plugin, sender_id: str, new_uid: str) -> None:
    """逐 key 拷贝：读旧 → 写新（不删旧，删除由调用方在全部成功后执行）。"""
    for prefix in _LEGACY_KEY_PREFIXES:
        old_key = f"{prefix}:{sender_id}"
        new_key = f"{prefix}:{new_uid}"
        value = await plugin.get_kv_data(old_key, default=None)
        if value is None:
            continue
        # 新 key 已存在则以旧为准（迁移期间不会并发写，此处防御）
        await plugin.put_kv_data(new_key, value)


async def _migrate_origin(plugin, sender_id: str, new_uid: str, platform_name: str, platform_id: str, result: dict) -> None:
    """迁移旧 origin（str）→ 新结构（dict）。

    - FriendMessage → 转换；GroupMessage / 非法 → 丢弃并计数（P0-2 污染数据）。
    """
    old_key = f"{KV_SESSION_ORIGIN_PREFIX}:{sender_id}"
    raw = await plugin.get_kv_data(old_key, default=None)
    if raw is None:
        return
    if isinstance(raw, dict):
        # 已是新结构（理论不会出现在旧 key 下），直接搬运
        await plugin.put_kv_data(f"{KV_SESSION_ORIGIN_PREFIX}:{new_uid}", raw)
        return
    if not isinstance(raw, str):
        return
    parsed = _parse_legacy_origin(raw)
    if parsed is None:
        result["dropped_origin"] += 1
        logger.warning(
            f"[migration] 用户 {sender_id} 旧 origin 为群聊/非法条目，已丢弃（请重新登录）"
        )
        return
    old_pid, session_id = parsed
    await plugin.put_kv_data(
        f"{KV_SESSION_ORIGIN_PREFIX}:{new_uid}",
        {
            "platform_name": platform_name if old_pid == platform_id else "",
            "platform_id": old_pid,
            "session_id": session_id,
            "saved_at": 0.0,
        },
    )
