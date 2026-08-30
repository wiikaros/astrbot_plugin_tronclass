""".ics 课表文件解析与上课时间判断。"""

from datetime import datetime, date, timedelta
from typing import Optional, List

import icalendar
from astrbot.api import logger


def parse_ics(content: str) -> Optional[dict]:
    """解析 .ics 课表文件内容。

    Args:
        content: .ics 文件的文本内容。

    Returns:
        {
            "semester_start": "2026-02-24",
            "courses": [
                {
                    "name": "高等数学",
                    "day": 1,           # 1=周一, 7=周日
                    "start": "08:00",
                    "end": "09:40",
                    "weeks": [1,2,3,...,16],
                    "location": "教学楼A201",   # 可选
                },
                ...
            ]
        }
        解析失败返回 None。

    H1 修复说明：周次计算必须使用**全局**学期起点（最早上课事件所在周的周一），
    分两阶段处理——先收集全部事件日期，再统一计算周次，
    避免"结果依赖 VEVENT 在文件中的出现顺序"的缺陷。
    """
    try:
        cal = icalendar.Calendar.from_ical(content)
    except Exception as e:
        logger.error(f"ICS 解析失败：{e}")
        return None

    # ---- 第一遍：收集有效事件，计算全局学期起点 ----
    raw_events = []
    earliest_start = None
    for component in cal.walk():
        if component.name != "VEVENT":
            continue
        summary = str(component.get("summary", ""))
        if not summary:
            continue
        dtstart = component.get("dtstart")
        if dtstart is None:
            continue
        dtstart = dtstart.dt
        if isinstance(dtstart, datetime):
            start_date = dtstart.date()
        elif isinstance(dtstart, date):
            start_date = dtstart
        else:
            continue
        if earliest_start is None or start_date < earliest_start:
            earliest_start = start_date
        raw_events.append((component, summary, dtstart))

    if not raw_events:
        logger.warning("ICS 文件中未找到课程事件")
        return None

    # 全局学期起点 = 最早上课事件所在周的周一
    if earliest_start:
        weekday = earliest_start.isoweekday()
        global_semester_start = earliest_start - timedelta(days=weekday - 1)
        semester_start_str = global_semester_start.strftime("%Y-%m-%d")
    else:
        global_semester_start = datetime.now().date()
        semester_start_str = global_semester_start.strftime("%Y-%m-%d")

    # ---- 第二遍：逐事件解析（统一使用全局学期起点计算周次）----
    courses = []
    for component, summary, dtstart in raw_events:
        dtend = component.get("dtend")
        location = str(component.get("location", ""))
        dtend = dtend.dt if dtend else None

        # 如果是 datetime，提取日期和时间
        if isinstance(dtstart, datetime):
            start_date = dtstart.date()
            start_time = dtstart.strftime("%H:%M")
            end_time = dtend.strftime("%H:%M") if isinstance(dtend, datetime) else "00:00"
            day = dtstart.isoweekday()
        elif isinstance(dtstart, date):
            start_date = dtstart
            start_time = "00:00"
            end_time = "00:00"
            day = dtstart.isoweekday()
        else:
            continue

        weeks = _extract_weeks(component, start_date, global_semester_start)

        course = {
            "name": summary.strip(),
            "day": day,
            "start": start_time,
            "end": end_time,
            "weeks": weeks,
            "location": location.strip() if location else "",
        }
        courses.append(course)

    # 去重（同一课程可能有多个 VEVENT）
    seen = set()
    unique_courses = []
    for c in courses:
        key = (c["name"], c["day"], c["start"], c["end"])
        if key not in seen:
            seen.add(key)
            unique_courses.append(c)

    logger.info(f"ICS 解析完成：{len(unique_courses)} 门课程")

    return {
        "semester_start": semester_start_str,
        "courses": unique_courses,
    }


def _extract_weeks(component, start_date: date, semester_start: date) -> List[int]:
    """从 VEVENT 中提取上课周次列表。

    通过 RRULE 的 BYWEEKNO、INTERVAL、COUNT/UNTIL 等信息计算。
    如果无法从规则推算，则根据开始日期计算所属周次。
    """
    rrule = component.get("rrule")
    if rrule is None:
        # 单次事件，计算所在周次
        week_num = (start_date - semester_start).days // 7 + 1
        if 0 < week_num <= 52:
            return [week_num]
        return [1]

    interval = int(rrule.get("INTERVAL", [1])[0]) if rrule.get("INTERVAL") else 1
    count = int(rrule.get("COUNT", [0])[0]) if rrule.get("COUNT") else 0

    # 获取直到日期
    until = rrule.get("UNTIL")
    until_date = None
    if until:
        raw = until[0] if isinstance(until, list) else until
        if isinstance(raw, datetime):
            until_date = raw.date()
        elif isinstance(raw, date):
            until_date = raw

    # 计算起始周
    start_week = (start_date - semester_start).days // 7 + 1
    if start_week < 1:
        start_week = 1

    # 计算结束周
    if until_date:
        end_week = (until_date - semester_start).days // 7 + 1
    elif count > 0:
        end_week = start_week + (count - 1) * interval
    else:
        end_week = start_week + 15 * interval  # 默认 16 周

    weeks = list(range(start_week, min(end_week + 1, 53), interval))
    return weeks


def is_in_class_now(
    schedule: dict,
    precheck_minutes: int = 5,
    now: Optional[datetime] = None,
) -> bool:
    """判断当前是否在上课时间内（含提前检测窗口）。

    Args:
        schedule: parse_ics() 返回的课表数据。
        precheck_minutes: 课前提前多少分钟开始检测。
        now: 要检查的时间点（默认为现在，用于测试）。

    Returns:
        是否在当前时间应检测点名。
    """
    if now is None:
        now = datetime.now()

    if not schedule or not schedule.get("courses"):
        return False

    # 计算当前教学周
    try:
        semester_start = datetime.strptime(
            schedule["semester_start"], "%Y-%m-%d"
        ).date()
    except (ValueError, KeyError):
        return False

    current_week = (now.date() - semester_start).days // 7 + 1
    if current_week < 1 or current_week > 52:
        return False

    precheck_delta = timedelta(minutes=precheck_minutes)
    # 本教学周的周一（用于把"周几"映射到绝对日期，支撑跨天课程判定）
    monday = now.date() - timedelta(days=now.isoweekday() - 1)

    for course in schedule["courses"]:
        # 检查周次
        weeks = course.get("weeks", [])
        if weeks and current_week not in weeks:
            continue

        # 检查时间区间
        try:
            start_t = datetime.strptime(course["start"], "%H:%M").time()
            end_t = datetime.strptime(course["end"], "%H:%M").time()
        except (ValueError, KeyError):
            continue

        day = course.get("day")
        if not isinstance(day, int) or not (1 <= day <= 7):
            continue

        # M5 修复：先锚定"本教学周内课程开始日"的绝对时间点，
        # 再做跨天调整与检测窗口计算——否则跨天课程（如 23:30-00:30）
        # 次日凌晨时段会因 day 不匹配或 check_start 锚定错误而漏判。
        class_date = monday + timedelta(days=day - 1)
        start_dt = datetime.combine(class_date, start_t)
        end_dt = datetime.combine(class_date, end_t)
        if end_dt < start_dt:
            end_dt += timedelta(days=1)  # 跨天课程：结束时间顺延至次日

        check_start = start_dt - precheck_delta

        if check_start <= now <= end_dt:
            logger.debug(
                f"上课中：{course['name']} "
                f"(周{current_week} 周{day} "
                f"{course['start']}-{course['end']})"
            )
            return True

    return False
