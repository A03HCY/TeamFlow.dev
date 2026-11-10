"""iCalendar（.ics）订阅文件生成。"""
from __future__ import annotations

import datetime as _dt

from ..db.models import Team
from .schedules import ICS_BYDAY


def _escape(text) -> str:
    return (str(text).replace("\\", "\\\\").replace(";", "\\;")
            .replace(",", "\\,").replace("\n", "\\n"))


def _next_weekday(start_iso: str, weekday: int) -> str:
    """start_iso 当天或之后第一个 weekday（0=周一）对应的日期。"""
    day = _dt.date.fromisoformat(start_iso)
    delta = (weekday - day.weekday()) % 7
    return (day + _dt.timedelta(days=delta)).isoformat()


def build_ics(team: Team, entries) -> str:
    stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0",
        "PRODID:-//TeamFlow//Schedule//CN", "CALSCALE:GREGORIAN",
        f"X-WR-CALNAME:{_escape(team.name)} 日程",
    ]
    for entry in entries:
        # 循环日程的锚点：生效开始（或今天）后第一个匹配星期；单日日程用自身日期
        if entry.kind == "recurring" and entry.weekday is not None:
            anchor = _next_weekday(
                entry.start_date or _dt.date.today().isoformat(), entry.weekday
            )
        else:
            anchor = entry.date
        if not anchor:
            continue
        owner = entry.owner.display_name if entry.owner is not None else ""
        summary = entry.title + (f" · {owner}" if owner else "")
        lines.append("BEGIN:VEVENT")
        lines.append(f"UID:teamflow-{entry.id}@teamflow")
        lines.append(f"DTSTAMP:{stamp}")
        lines.append(f"SUMMARY:{_escape(summary)}")
        if entry.start_time:
            sh, sm = int(entry.start_time[:2]), int(entry.start_time[3:5])
            duration = 60
            if entry.end_time:
                eh, em = int(entry.end_time[:2]), int(entry.end_time[3:5])
                duration = max((eh * 60 + em) - (sh * 60 + sm), 15)
            year, month, day = (int(x) for x in anchor.split("-"))
            start_dt = _dt.datetime(year, month, day, sh, sm)
            lines.append(f"DTSTART:{start_dt.strftime('%Y%m%dT%H%M%S')}")
            lines.append(
                f"DTEND:{(start_dt + _dt.timedelta(minutes=duration)).strftime('%Y%m%dT%H%M%S')}"
            )
        else:
            lines.append(f"DTSTART;VALUE=DATE:{anchor.replace('-', '')}")
        if entry.kind == "recurring" and entry.weekday is not None:
            rule = f"FREQ=WEEKLY;BYDAY={ICS_BYDAY[entry.weekday]}"
            if entry.end_date:
                rule += ";UNTIL=" + entry.end_date.replace("-", "") + "T235959"
            lines.append("RRULE:" + rule)
        if entry.note:
            lines.append(f"DESCRIPTION:{_escape(entry.note)}")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines)
