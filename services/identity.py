"""用户标识与推送目标构建（P0-3/P0-4 唯一入口，禁止绕过）。

- `get_user_key`：统一用户标识 `platform_id:sender_id`，全链路 KV key 唯一来源；
- `build_friend_origin`：构造私聊推送目标字符串（等价框架 MessageSession.__str__ 输出）；
- `resolve_platform_id`：按平台类型名解析当前适配器实例 id（适配器重建后自愈）。
"""

from astrbot.api import logger


def get_user_key(event) -> str:
    """统一用户标识：`platform_id:sender_id`。

    - `platform_id` 唯一（框架注释保证），作 key 前缀；
    - `sender_id` 为空（框架在 `sender.user_id` 非 str 时返回 ""，见
      astr_message_event.py:202-207）→ 回退 `event.session_id`，再空 → "unknown" 并告警；
    - 任何作 KV key 的地方必须经此函数，禁止直接 `get_sender_id()`。
    """
    sender = event.get_sender_id() or getattr(event, "session_id", "") or ""
    pid = event.get_platform_id() or event.get_platform_name() or "unknown"
    if not sender:
        logger.warning(f"[identity] sender_id 为空，回退 session_id（platform={pid}）")
    return f"{pid}:{sender}"


def build_friend_origin(platform_id: str, session_id: str) -> str:
    """构造私聊推送目标字符串。

    等价于框架 `MessageSession(platform_id, FriendMessage, session_id).__str__` 的输出
    （`message_session.py:18-19` 格式：`platform_id:message_type:session_id`）。
    不 import 框架内部类，仅按格式拼串，保证跨版本稳定。
    """
    return f"{platform_id}:FriendMessage:{session_id}"


def resolve_platform_id(context, platform_name: str, hint_id: str = "") -> str | None:
    """按平台类型名解析当前适配器实例 id（适配器重建后自愈）。

    Args:
        context: AstrBot Context（公开属性 `platform_manager.platform_insts`，context.py:92）。
        platform_name: 平台类型名（如 aiocqhttp），登录时由 `event.get_platform_name()` 取得。
        hint_id: 登录时记录的实例 id（快速路径 / 多实例歧义消解）。

    Returns:
        解析出的实例 id；找不到返回 None。
    """
    instances = []
    try:
        pm = getattr(context, "platform_manager", None)
        instances = list(getattr(pm, "platform_insts", None) or [])
    except Exception as e:
        logger.warning(f"[identity] 读取平台实例列表失败: {e}")
        instances = []

    matched = []
    for p in instances:
        try:
            meta = p.meta()
            if meta and getattr(meta, "name", None) == platform_name:
                matched.append(meta)
        except Exception:
            continue

    if not matched:
        # 按 name 查不到：回退 hint（快速路径，适配器元数据异常时兜底）
        return hint_id or None
    if len(matched) == 1:
        return matched[0].id
    # 多实例歧义：优先 hint id，否则取首个并告警
    for meta in matched:
        if meta.id == hint_id:
            return meta.id
    logger.warning(
        f"[identity] 平台 {platform_name!r} 存在 {len(matched)} 个实例，"
        f"hint={hint_id!r} 不匹配，取首个"
    )
    return matched[0].id
