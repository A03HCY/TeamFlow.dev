"""响应体序列化：字段名与旧版前端逐字保持一致，前端无需改动取值逻辑。"""
from __future__ import annotations

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from ..db.models import (
    Activity,
    Comment,
    Issue,
    Label,
    Milestone,
    ScheduleEntry,
    Team,
    User,
)
from .avatars import url as avatar_url


def public_user(user: User | None) -> dict | None:
    if user is None:
        return None
    return {
        "id": user.id,
        "username": user.username,
        "display_name": user.display_name,
        "title": user.title or "",
        "email": user.email or "",
        "avatar_url": avatar_url(user),
    }


def team_summary(team: Team, role: str | None = None) -> dict:
    return {
        "id": team.id,
        "name": team.name,
        "slug": team.slug,
        "description": team.description or "",
        "issues_public": bool(team.issues_public),
        "milestones_public": bool(team.milestones_public),
        "roadmap_public": bool(team.roadmap_public),
        "schedules_public": bool(team.schedules_public),
        "issue_seq": team.issue_seq or 0,
        "created_at": team.created_at,
        "role": role,
    }


def label_dict(label: Label) -> dict:
    return {"id": label.id, "name": label.name, "color": label.color}


def issue_dict(issue: Issue, comment_count: int | None = None) -> dict:
    assignee = issue.assignee
    author = issue.author
    milestone = issue.milestone
    return {
        "id": issue.id,
        "team_id": issue.team_id,
        "number": issue.number,
        "title": issue.title,
        "body": issue.body or "",
        "state": issue.state or "open",
        "status": issue.status or "todo",
        "priority": issue.priority or "medium",
        "milestone_id": issue.milestone_id,
        "milestone_title": milestone.title if milestone is not None else "",
        "assignee_id": issue.assignee_id,
        "assignee_name": assignee.display_name if assignee is not None else "",
        "assignee_title": (assignee.title or "") if assignee is not None else "",
        "assignee_avatar_url": avatar_url(assignee),
        "author_id": issue.author_id,
        "author_name": author.display_name if author is not None else "",
        "author_avatar_url": avatar_url(author),
        "due_date": issue.due_date,
        "plan_week": issue.plan_week,
        "created_at": issue.created_at,
        "updated_at": issue.updated_at,
        "closed_at": issue.closed_at,
        "comment_count": int(comment_count or 0),
        "labels": [label_dict(item) for item in (issue.labels or [])],
    }


def comment_counts(session: Session, issue_ids: list[int]) -> dict[int, int]:
    """一条聚合查询把多个 Issue 的评论数取回来（列表页用，避免 N+1）。"""
    if not issue_ids:
        return {}
    rows = session.execute(
        select(Comment.issue_id, func.count())
        .where(Comment.issue_id.in_(issue_ids))
        .group_by(Comment.issue_id)
    )
    return {issue_id: int(count) for issue_id, count in rows}


def comment_dict(comment: Comment) -> dict:
    return {
        "id": comment.id,
        "issue_id": comment.issue_id,
        "user_id": comment.user_id,
        "username": comment.user.username if comment.user is not None else "",
        "author_name": comment.author_name or "",
        "avatar_url": avatar_url(comment.user),
        "body": comment.body or "",
        "created_at": comment.created_at,
    }


def milestone_counts(session: Session, team_id: int) -> dict[int, tuple[int, int]]:
    """{milestone_id: (total, closed)}，一条聚合查询搞定，避免 N+1。"""
    stmt = (
        select(
            Issue.milestone_id,
            func.count(Issue.id),
            func.coalesce(func.sum(case((Issue.state == "closed", 1), else_=0)), 0),
        )
        .where(Issue.team_id == team_id, Issue.milestone_id.is_not(None))
        .group_by(Issue.milestone_id)
    )
    return {
        mid: (int(total or 0), int(closed or 0))
        for mid, total, closed in session.execute(stmt)
    }


def milestone_dict(milestone: Milestone, counts: dict[int, tuple[int, int]]) -> dict:
    total, closed = counts.get(milestone.id, (0, 0))
    progress = round(closed / total * 100) if total else 0
    return {
        "id": milestone.id,
        "team_id": milestone.team_id,
        "number": milestone.number,
        "title": milestone.title,
        "description": milestone.description or "",
        "state": milestone.state or "open",
        "kind": milestone.kind or "scheduled",
        "start_date": milestone.start_date,
        "due_date": milestone.due_date,
        "position": milestone.position or 0,
        "created_at": milestone.created_at,
        "closed_at": milestone.closed_at,
        "total": total,
        # closed 是旧版概览用的字段名，closed_count 是旧版里程碑列表用的；
        # 两个都给，前端不必判断用的是哪个接口。
        "closed": closed,
        "closed_count": closed,
        "open_count": total - closed,
        "progress": progress,
    }


def schedule_dict(entry: ScheduleEntry) -> dict:
    return {
        "id": entry.id,
        "team_id": entry.team_id,
        "user_id": entry.user_id,
        "user_name": entry.owner.display_name if entry.owner is not None else "",
        "title": entry.title,
        "note": entry.note or "",
        "kind": entry.kind or "once",
        "weekday": entry.weekday,
        "date": entry.date,
        "start_time": entry.start_time,
        "end_time": entry.end_time,
        "start_date": entry.start_date,
        "end_date": entry.end_date,
        "color": entry.color or "",
        "participants": [
            {"id": participant.id, "name": participant.display_name,
             "avatar_url": avatar_url(participant)}
            for participant in (entry.participants or [])
        ],
        "created_at": entry.created_at,
        "updated_at": entry.updated_at,
    }


def activity_dict(entry: Activity) -> dict:
    return {
        "id": entry.id,
        "team_id": entry.team_id,
        "actor_id": entry.actor_id,
        "actor_name": entry.actor_name or "",
        "verb": entry.verb,
        "subject_type": entry.subject_type,
        "subject_id": entry.subject_id,
        "subject_text": entry.subject_text or "",
        "detail": entry.detail or "",
        "created_at": entry.created_at,
    }
