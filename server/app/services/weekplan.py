"""周计划：ISO 周的解析、推进与顺延。

设计上刻意不引入 Sprint 实体——Issue 只有一个 ``plan_week``（``2026-W45``）字段，
将来真的需要迭代（容量、燃尽图）时，把 ``plan_week`` 映射到 Sprint 的周区间即可，
不会推翻现有数据。
"""
from __future__ import annotations

import datetime as _dt
import re

from sqlalchemy import case, select
from sqlalchemy.orm import Session

from ..db.base import utcnow_iso
from ..db.models import Issue

_WEEK_RE = re.compile(r"^(\d{4})-?[Ww](\d{1,2})$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

#: 未排期在列表筛选里的写法
EMPTY_TOKENS = frozenset({"none", "null", "unscheduled", "backlog", "0"})

#: 周内排序：高优先级在前，然后按编号
_WEEK_ORDER = (
    case((Issue.priority == "high", 0), (Issue.priority == "medium", 1), else_=2),
    Issue.number.asc(),
)


def week_of(date: _dt.date) -> str:
    iso = date.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def parse_week(value) -> str | None:
    """``2026-W45`` / ``2026W45`` / ``2026-11-03`` 统一成 ``2026-W45``；空值返回 ``None``。"""
    if value in (None, "", "null"):
        return None
    text = str(value).strip()
    if not text:
        return None
    match = _WEEK_RE.match(text)
    if match:
        year, week = int(match.group(1)), int(match.group(2))
        if not 1 <= week <= 53:
            return None
        try:
            _dt.date.fromisocalendar(year, week, 1)
        except ValueError:  # 该年没有第 53 周
            return None
        return f"{year}-W{week:02d}"
    if _DATE_RE.match(text):
        try:
            return week_of(_dt.date.fromisoformat(text))
        except ValueError:
            return None
    return None


def require_week(value) -> str:
    parsed = parse_week(value)
    if parsed is None:
        raise ValueError("计划周格式应为 YYYY-Www，例如 2026-W45")
    return parsed


def week_start(week: str) -> _dt.date:
    match = _WEEK_RE.match(str(week or "").strip())
    if not match:
        raise ValueError(f"无法识别的周：{week}")
    return _dt.date.fromisocalendar(int(match.group(1)), int(match.group(2)), 1)


def week_end(week: str) -> _dt.date:
    return week_start(week) + _dt.timedelta(days=6)


def shift_week(week: str, delta: int) -> str:
    return week_of(week_start(week) + _dt.timedelta(days=7 * delta))


def current_week(today: _dt.date | None = None) -> str:
    return week_of(today or _dt.date.today())


def resolve(token: str | None, *, today: _dt.date | None = None) -> str | None:
    """把 ``current`` / ``next`` / ``prev`` / ``none`` 解析成具体周（``none`` → ``None``）。"""
    base = current_week(today)
    text = str(token or "").strip().lower()
    if not text or text == "current":
        return base
    if text in ("next", "+1"):
        return shift_week(base, 1)
    if text in ("prev", "last", "-1"):
        return shift_week(base, -1)
    if text in EMPTY_TOKENS:
        return None
    parsed = parse_week(text)
    if parsed is None:
        raise ValueError(f"无法识别的周：{token}")
    return parsed


def label(week: str) -> str:
    start = week_start(week)
    iso = start.isocalendar()
    return f"{iso.year} 第 {iso.week} 周"


def range_text(week: str) -> str:
    start, end = week_start(week), week_end(week)
    left = f"{start.month}/{start.day:02d}"
    right = f"{end.month}/{end.day:02d}" if start.month != end.month else f"{end.day:02d}"
    return f"{left} – {right}"


def issues_in_week(session: Session, team_id: int, week: str, *, state: str | None = None) -> list[Issue]:
    stmt = select(Issue).where(Issue.team_id == team_id, Issue.plan_week == week)
    if state:
        stmt = stmt.where(Issue.state == state)
    return list(session.scalars(stmt.order_by(*_WEEK_ORDER)))


def unscheduled(session: Session, team_id: int, limit: int = 100) -> list[Issue]:
    """未排期的开启中 Issue（周视图底部的池子）。"""
    stmt = (
        select(Issue)
        .where(
            Issue.team_id == team_id,
            Issue.state == "open",
            Issue.plan_week.is_(None),
        )
        .order_by(*_WEEK_ORDER)
        .limit(limit)
    )
    return list(session.scalars(stmt))


def rollover(
    session: Session,
    team_id: int,
    from_week: str,
    to_week: str,
    numbers: list[int] | None = None,
) -> list[Issue]:
    """把上一周仍未完成的 Issue 顺延到 ``to_week``（只动 open 状态的）。"""
    stmt = select(Issue).where(
        Issue.team_id == team_id,
        Issue.plan_week == from_week,
        Issue.state == "open",
    )
    if numbers:
        stmt = stmt.where(Issue.number.in_(numbers))
    issues = list(session.scalars(stmt))
    if issues and to_week != from_week:
        now = utcnow_iso()
        for issue in issues:
            issue.plan_week = to_week
            issue.updated_at = now
        session.flush()
    return issues
