"""团队概览：统计、趋势、负载与最近动态。"""
from __future__ import annotations

import datetime as _dt

from sqlalchemy import case, func, literal, select, union_all
from sqlalchemy.orm import Session

from ..db.base import today_iso, utcnow_iso
from ..db.models import Issue, Team, TeamMember, User
from . import activity, hierarchy
from .serializers import milestone_counts, milestone_dict
from .teams import member_rows

TREND_DAYS = 14
ACTIVITY_LIMIT = 12


def stats(session: Session, team_id: int) -> dict:
    """一次聚合查出全部计数。

    旧实现是 7 条独立 COUNT；合并成单条带条件的 SUM 后，概览接口的
    查询数从 15 降到 9（见 README「性能」一节）。
    """
    today = today_iso()
    leaf = hierarchy.leaf_condition()
    total, open_count, todo, in_progress, done, high_open, overdue = session.execute(
        select(
            func.count(Issue.id),
            func.coalesce(func.sum(case((Issue.state == "open", 1), else_=0)), 0),
            func.coalesce(func.sum(case((Issue.status == "todo", 1), else_=0)), 0),
            func.coalesce(func.sum(case((Issue.status == "in_progress", 1), else_=0)), 0),
            func.coalesce(func.sum(case((Issue.status == "done", 1), else_=0)), 0),
            func.coalesce(func.sum(case(
                ((Issue.state == "open") & (Issue.priority == "high"), 1), else_=0)), 0),
            func.coalesce(func.sum(case((
                (Issue.state == "open")
                & Issue.due_date.is_not(None)
                & (Issue.due_date < today), 1), else_=0)), 0),
        ).where(Issue.team_id == team_id, leaf)
    ).one()
    parents = session.scalar(
        select(func.count()).select_from(Issue).where(
            Issue.team_id == team_id, ~leaf
        )
    ) or 0
    total = int(total or 0)
    open_count = int(open_count or 0)
    completion = round((total - open_count) / total * 100, 1) if total else 0.0
    return {
        "total": total,
        "open": open_count,
        "closed": total - open_count,
        "todo": int(todo or 0),
        "in_progress": int(in_progress or 0),
        "done": int(done or 0),
        "high_open": int(high_open or 0),
        "overdue": int(overdue or 0),
        "parents": int(parents),
        "completion": completion,
    }


def trend(session: Session, team_id: int, days: int = TREND_DAYS) -> list[dict]:
    """近 N 天的新建/关闭数。

    分桶交给数据库做（``substr(created_at, 1, 10)`` 在三套后端都成立），
    只回传有活动的日期，避免把整张 issue 表拉进内存。
    """
    today = _dt.date.fromisoformat(today_iso())
    start = (today - _dt.timedelta(days=days - 1)).isoformat()
    created = select(
        func.substr(Issue.created_at, 1, 10).label("day"),
        literal(1).label("kind"),
    ).where(Issue.team_id == team_id, Issue.created_at >= start)
    closed = select(
        func.substr(Issue.closed_at, 1, 10).label("day"),
        literal(0).label("kind"),
    ).where(
        Issue.team_id == team_id,
        Issue.closed_at.is_not(None),
        Issue.closed_at >= start,
    )
    merged = union_all(created, closed).subquery()
    rows = session.execute(
        select(
            merged.c.day,
            func.sum(case((merged.c.kind == 1, 1), else_=0)),
            func.sum(case((merged.c.kind == 0, 1), else_=0)),
        ).group_by(merged.c.day)
    )
    buckets = {day: (int(c or 0), int(k or 0)) for day, c, k in rows}

    out = []
    for offset in range(days - 1, -1, -1):
        day = (today - _dt.timedelta(days=offset)).isoformat()
        made, shut = buckets.get(day, (0, 0))
        out.append({"day": day, "created": made, "closed": shut})
    return out


def workload(session: Session, team_id: int) -> list[dict]:
    rows = session.execute(
        select(
            User.id,
            User.username,
            User.display_name,
            User.title,
            User.avatar,
            func.coalesce(func.sum(case((Issue.state == "open", 1), else_=0)), 0),
            func.coalesce(func.sum(case((Issue.state == "closed", 1), else_=0)), 0),
        )
        .join(TeamMember, TeamMember.user_id == User.id)
        .outerjoin(
            Issue,
            (Issue.assignee_id == User.id)
            & (Issue.team_id == team_id)
            & hierarchy.leaf_condition(),
        )
        .where(TeamMember.team_id == team_id)
        .group_by(User.id, User.username, User.display_name, User.title)
        .order_by(
            func.coalesce(func.sum(case((Issue.state == "open", 1), else_=0)), 0).desc(),
            User.display_name.asc(),
        )
    )
    out = []
    for user_id, username, display_name, title, avatar, open_count, closed_count in rows:
        out.append({
            "id": user_id,
            "username": username,
            "display_name": display_name,
            "title": title or "",
            "open_count": int(open_count or 0),
            "closed_count": int(closed_count or 0),
            "avatar_url": f"/api/users/{user_id}/avatar" if (avatar or "").strip() else "",
        })
    return out


def milestones(session: Session, team_id: int) -> list[dict]:
    from ..db.models import Milestone

    items = list(
        session.scalars(
            select(Milestone)
            .where(Milestone.team_id == team_id)
            .order_by(
                Milestone.due_date.is_(None),
                Milestone.due_date.asc(),
                Milestone.position.asc(),
            )
        )
    )
    counts = milestone_counts(session, team_id)
    return [milestone_dict(m, counts) for m in items]


def overview(session: Session, team: Team, role: str | None = None) -> dict:
    from .serializers import team_summary

    return {
        "stats": stats(session, team.id),
        "milestones": milestones(session, team.id),
        "trend": trend(session, team.id),
        "activity": [activity.serialize(entry)
                     for entry in activity.recent(session, team.id, ACTIVITY_LIMIT)],
        "workload": workload(session, team.id),
        "team": team_summary(team, role),
        "members": member_rows(session, team.id),
    }
