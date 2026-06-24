"""通知消息生成器。

将业务事件（新作业、快到期、新点名）转化为用户可读的消息文本。
"""

from typing import List
from datetime import datetime


def format_new_homework(homework: dict) -> str:
    """格式化单条新作业通知。"""
    course = homework.get("course_name", "未知课程")
    title = homework.get("title", "未命名作业")
    due = homework.get("due_at", "未知截止时间")

    return (
        f"📌 **新作业发布**\n"
        f"课程：《{course}》\n"
        f"作业：{title}\n"
        f"⏰ 截止时间：{due}"
    )


def format_due_warning(homework: dict, remaining_hours: int) -> str:
    """格式化单条作业快到期提醒。"""
    course = homework.get("course_name", "未知课程")
    title = homework.get("title", "未命名作业")
    due = homework.get("due_at", "未知截止时间")

    if remaining_hours < 1:
        urgency = "⚡ 不到 1 小时"
    elif remaining_hours < 6:
        urgency = f"⏰ 仅剩 {remaining_hours} 小时"
    else:
        urgency = f"📅 剩余 {remaining_hours} 小时"

    return (
        f"⏳ **作业即将截止**\n"
        f"课程：《{course}》\n"
        f"作业：{title}\n"
        f"⏰ 截止时间：{due}\n"
        f"{urgency}"
    )


def format_new_rollcall(rollcall: dict) -> str:
    """格式化单条新点名通知。"""
    course = rollcall.get("course_title", "未知课程")
    teacher = rollcall.get("created_by_name", "老师")
    rc_type = "数字点名" if rollcall.get("is_number") else "签到"
    code = rollcall.get("number_code", "")
    rc_time = rollcall.get("rollcall_time", "")

    msg = (
        f"🔔 **新签到/点名通知**\n"
        f"课程：《{course}》\n"
        f"类型：{rc_type}（{teacher}发起的）"
    )
    if code:
        msg += f"\n🔢 签到码：{code}"
    if rc_time:
        msg += f"\n🕐 开始时间：{rc_time}"

    msg += "\n\n⚠️ 请尽快在畅课 App 中完成签到！"
    return msg


def format_homework_summary(
    added: List[dict],
    updated: List[dict],
    removed: List[dict],
) -> str:
    """格式化作业更新摘要（用于 /更新作业 命令的返回）。"""
    lines = ["📋 作业更新摘要：", ""]

    if added:
        lines.append(f"🆕 新作业（{len(added)} 项）：")
        for hw in added:
            lines.append(f"  - 《{hw.get('course_name', '?')}》{hw.get('title', '?')}")
        lines.append("")

    if updated:
        lines.append(f"🔄 有更新（{len(updated)} 项）：")
        for hw in updated:
            lines.append(f"  - 《{hw.get('course_name', '?')}》{hw.get('title', '?')}")
        lines.append("")

    if removed:
        lines.append(f"✅ 已完成（{len(removed)} 项）：")
        for hw in removed:
            lines.append(f"  - 《{hw.get('course_name', '?')}》{hw.get('title', '?')}")
        lines.append("")

    if not added and not updated and not removed:
        lines.append("✅ 作业列表无变化。")

    return "\n".join(lines)


def format_multiple_homework_notifications(
    added: List[dict],
    imminent: List[dict],
) -> List[str]:
    """批量生成通知消息列表（给定时任务用的）。

    Returns:
        每条通知为一个独立的消息字符串。
    """
    messages = []

    for hw in added:
        messages.append(format_new_homework(hw))

    for hw in imminent:
        # 计算剩余小时数
        due_str = hw.get("due_at", "")
        try:
            for fmt in (
                "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%dT%H:%M:%SZ",
                "%Y-%m-%d",
            ):
                try:
                    due_dt = datetime.strptime(due_str, fmt)
                    break
                except ValueError:
                    continue
            else:
                raise ValueError("no format matched")
            remaining = due_dt - datetime.now()
            hours = max(0, int(remaining.total_seconds() / 3600))
        except Exception:
            hours = 0

        messages.append(format_due_warning(hw, hours))

    return messages
