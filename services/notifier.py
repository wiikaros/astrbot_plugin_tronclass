"""通知消息生成器。"""

from typing import List
from datetime import datetime

from ..api._utils import parse_datetime


def _fmt_due(iso_str: str) -> str:
    if not iso_str:
        return "未知"
    dt = parse_datetime(iso_str)
    if dt:
        return dt.strftime("%m月%d日 %H:%M")
    return iso_str


def format_new_homework(homework: dict) -> str:
    course = homework.get("course_name", "未知课程")
    title = homework.get("title", "未命名作业")
    due = _fmt_due(homework.get("due_at", ""))
    return f"📌 **新作业发布**\n课程：《{course}》\n作业：{title}\n⏰ 截止时间：{due}"


def format_due_warning(homework: dict, remaining_hours: int) -> str:
    course = homework.get("course_name", "未知课程")
    title = homework.get("title", "未命名作业")
    due = _fmt_due(homework.get("due_at", ""))
    if remaining_hours < 1:
        urgency = "⚡ 不到 1 小时"
    elif remaining_hours < 6:
        urgency = f"⏰ 仅剩 {remaining_hours} 小时"
    else:
        urgency = f"📅 剩余 {remaining_hours} 小时"
    return f"⏳ **作业即将截止**\n课程：《{course}》\n作业：{title}\n⏰ 截止时间：{due}\n{urgency}"


def format_new_rollcall(rollcall: dict) -> str:
    course = rollcall.get("course_title", "未知课程")
    teacher = rollcall.get("created_by_name", "老师")
    rc_type = "数字点名" if rollcall.get("is_number") else "签到"
    code = rollcall.get("number_code", "")
    msg = (
        f"🔔 **新签到/点名通知**\n"
        f"课程：《{course}》\n"
        f"类型：{rc_type}（{teacher}发起的）"
    )
    if code:
        msg += f"\n🔢 签到码：{code}"
    msg += "\n\n⚠️ 请尽快在畅课 App 中完成签到！"
    return msg


def format_homework_summary(
    added: List[dict], updated: List[dict], removed: List[dict]
) -> str:
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
    added: List[dict], imminent: List[dict]
) -> List[str]:
    messages = []
    for hw in added:
        messages.append(format_new_homework(hw))
    for hw in imminent:
        due_dt = parse_datetime(hw.get("due_at", ""))
        remaining = due_dt - datetime.now() if due_dt else None
        hours = max(0, int(remaining.total_seconds() / 3600)) if remaining else 0
        messages.append(format_due_warning(hw, hours))
    return messages
