"""里程碑（milestone）读写：GitHub 风格用团队内 number 定位。"""
from __future__ import annotations

import datetime as _dt

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db.base import utcnow_iso
from ..db.models import Milestone, Team
from ..errors import ApiError
from .serializers import milestone_counts, milestone_dict
from .teams import next_milestone_number

_ORDER_BY_START = (
    Milestone.start_date.is_(None),
    Milestone.start_date.asc(),
    Milestone.position.asc(),
    Milestone.id.asc(),
)

#: 里程碑两种形态：按日期排期 / 长期持续（没有终点，也就没有 DDL）
VALID_KINDS = ("scheduled", "continuous")


def _kind(value, fallback: str = "scheduled") -> str:
    text = str(value or "").strip().lower()
    return text if text in VALID_KINDS else fallback

_ORDER_BY_ROADMAP = (
    func.coalesce(Milestone.start_date, Milestone.due_date).is_(None),
    func.coalesce(Milestone.start_date, Milestone.due_date).asc(),
    Milestone.position.asc(),
    Milestone.id.asc(),
)


def parse_date(value) -> str | None:
    if value in (None, "", "null"):
        return None
    text = str(value).strip()[:10]
    try:
        _dt.date.fromisoformat(text)
    except ValueError:
        return None
    return text


def listing(session: Session, team_id: int, roadmap: bool = False) -> list[dict]:
    order = _ORDER_BY_ROADMAP if roadmap else _ORDER_BY_START
    milestones = list(
        session.scalars(select(Milestone).where(Milestone.team_id == team_id).order_by(*order))
    )
    counts = milestone_counts(session, team_id)
    return [milestone_dict(m, counts) for m in milestones]


def get_by_number(session: Session, team_id: int, number: int) -> Milestone | None:
    return session.scalar(
        select(Milestone).where(Milestone.team_id == team_id, Milestone.number == number)
    )


def get_or_404(session: Session, milestone_id: int) -> Milestone:
    milestone = session.get(Milestone, milestone_id)
    if milestone is None:
        raise ApiError("里程碑不存在", 404)
    return milestone


def _validate_window(start: str | None, due: str | None) -> None:
    if start and due and start > due:
        raise ApiError("开始日期不能晚于截止日期")


def create(session: Session, team: Team, data: dict) -> Milestone:
    title = str(data.get("title") or "").strip()
    if not title:
        raise ApiError("请填写里程碑名称")
    start = parse_date(data.get("start_date"))
    due = parse_date(data.get("due_date"))
    kind = _kind(data.get("kind"))
    if kind == "continuous":
        due = None  # 持续型里程碑没有终点
    _validate_window(start, due)
    milestone = Milestone(
        team_id=team.id,
        number=next_milestone_number(session, team.id),
        title=title[:120],
        description=str(data.get("description") or "")[:4000],
        acceptance=str(data.get("acceptance") or "")[:8000],
        state="open",
        kind=kind,
        start_date=start,
        due_date=due,
        position=next_position(session, team.id),
        created_at=utcnow_iso(),
        closed_at=None,
    )
    session.add(milestone)
    session.flush()
    return milestone


def next_position(session: Session, team_id: int) -> int:
    current = session.scalar(
        select(func.max(Milestone.position)).where(Milestone.team_id == team_id)
    )
    return int(current or 0) + 1


def update(session: Session, milestone: Milestone, data: dict) -> tuple[Milestone, str | None]:
    """返回 (milestone, 动态文案)；没有可更新字段时抛 ValueError。"""
    touched = False
    note: str | None = None
    now = utcnow_iso()

    if "title" in data:
        title = str(data.get("title") or "").strip()
        if not title:
            raise ApiError("请填写里程碑名称")
        milestone.title = title[:120]
        touched = True
    if "description" in data:
        milestone.description = str(data.get("description") or "")[:4000]
        touched = True
    if "acceptance" in data:
        milestone.acceptance = str(data.get("acceptance") or "")[:8000]
        touched = True
    for key in ("start_date", "due_date"):
        if key in data:
            setattr(milestone, key, parse_date(data.get(key)))
            touched = True
    if "kind" in data:
        milestone.kind = _kind(data.get("kind"), milestone.kind or "scheduled")
        touched = True
    if (milestone.kind or "scheduled") == "continuous":
        milestone.due_date = None  # 持续型没有截止日期
    if "start_date" in data or "due_date" in data:
        _validate_window(milestone.start_date, milestone.due_date)
    if "state" in data and data.get("state") in ("open", "closed"):
        state = data["state"]
        if state != milestone.state:
            note = "closed" if state == "closed" else "reopened"
            touched = True
        milestone.state = state
        milestone.closed_at = now if state == "closed" else None
    if not touched:
        raise LookupError("empty")
    session.flush()
    return milestone, note


def remove(session: Session, milestone: Milestone) -> None:
    session.delete(milestone)
    session.flush()
