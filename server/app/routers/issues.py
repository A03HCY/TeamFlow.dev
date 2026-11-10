"""Issue 端点（GitHub 风格：``/api/teams/{slug}/issues/{number}``）。"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import func, select

from ..db.base import utcnow_iso
from ..db.models import Comment, Issue, Label, Milestone
from ..deps import TeamAccess, read_body, team_access
from ..errors import ApiError
from ..services import activity, hierarchy, weekplan
from ..services import issues as issue_service
from ..services.teams import next_issue_number
from ..services.serializers import comment_counts, comment_dict, issue_dict, team_summary

router = APIRouter(prefix="/api/teams/{team}", tags=["issues"])


def _filters(state, status, priority, q, label, assignee, milestone, sort, week=None,
             parent=None) -> dict:
    return {
        "state": state, "status": status, "priority": priority, "q": q,
        "label": label, "label_id": label, "assignee": assignee,
        "milestone": milestone, "sort": sort, "week": week, "parent": parent,
    }


@router.get("/issues")
def list_issues(
    state: str | None = None,
    status: str | None = None,
    priority: str | None = None,
    q: str | None = None,
    label: str | None = None,
    assignee: str | None = None,
    milestone: str | None = None,
    week: str | None = None,
    sort: str | None = None,
    parent: str | None = None,
    access: TeamAccess = Depends(team_access),
) -> dict:
    access.require_read("issues")
    session = access.session
    # week 支持 current / next / prev / 2026-W45 / none
    try:
        week_value = weekplan.resolve(week) if week else None
    except ValueError as exc:
        raise ApiError(str(exc)) from exc
    filters = _filters(state, status, priority, q, label, assignee, milestone, sort,
                       "none" if week and week_value is None else week_value, parent)
    # 过滤器按标签名时先换算成 id（URL 里允许写名字更好用）
    if label and not str(label).isdigit():
        found = session.scalar(
            select(Label.id).where(Label.team_id == access.id, Label.name == label)
        )
        filters["label"] = filters["label_id"] = found if found is not None else "-1"
    items = issue_service.list_issues(session, access.id, filters)
    counts = comment_counts(session, [issue.id for issue in items])
    return {
        "issues": [issue_dict(issue, counts.get(issue.id, 0)) for issue in items],
        "counts": issue_service.counts(session, access.id),
        "can_edit": access.can_edit(),
    }


@router.post("/issues")
def create_issue(body: dict = Depends(read_body),
                 access: TeamAccess = Depends(team_access)) -> dict:
    access.require_edit()
    session, team = access.session, access.team
    title = str(body.get("title") or "").strip()
    if not title:
        raise ApiError("请填写标题")
    body = dict(body)
    body["title"] = title
    body["author_id"] = access.ctx.user_id
    try:
        milestone_id = issue_service.validate_milestone(session, team.id, body.get("milestone_id"))
    except LookupError as exc:
        raise ApiError(str(exc), 400) from exc
    body["milestone_id"] = milestone_id
    number = next_issue_number(session, team)
    try:
        issue = issue_service.create_issue(session, team, body, number)
    except LookupError as exc:
        raise ApiError(str(exc), 400) from exc
    actor_id, actor_name = access.ctx.actor()
    activity.log(session, team.id, actor_id, actor_name, "created", "issue", issue.id,
                 issue.title, f"#{number}")
    return {"issue": issue_dict(issue)}



@router.get("/issues/{number}")
def issue_detail(number: int, access: TeamAccess = Depends(team_access)) -> dict:
    access.require_read("issues")
    session = access.session
    issue = issue_service.find_by_number(session, access.id, number)
    if issue is None:
        raise ApiError("Issue 不存在", 404)
    comments = list(
        session.scalars(
            select(Comment).where(Comment.issue_id == issue.id).order_by(Comment.id)
        )
    )
    parent = session.get(Issue, issue.parent_id) if issue.parent_id else None
    children = hierarchy.children_of(session, issue)
    ancestors = hierarchy.ancestors_of(session, issue)
    return {
        "issue": issue_dict(issue, len(comments)),
        "comments": [comment_dict(comment) for comment in comments],
        "team": team_summary(access.team, access.effective_role()),
        "can_edit": access.can_edit(),
        "parent": issue_dict(parent) if parent is not None else None,
        "ancestors": [issue_dict(item) for item in ancestors],
        "children": [issue_dict(item) for item in children],
        "progress": hierarchy.rollup(session, issue),
        "can_close": hierarchy.can_close_parent(session, issue),
    }


@router.patch("/issues/{number}")
def update_issue(number: int, body: dict = Depends(read_body),
                 access: TeamAccess = Depends(team_access)) -> dict:
    access.require_edit()
    session = access.session
    issue = issue_service.find_by_number(session, access.id, number)
    if issue is None:
        raise ApiError("Issue 不存在", 404)
    try:
        issue, note = issue_service.update_issue(session, issue, body)
    except LookupError as exc:
        message = str(exc)
        if message == "empty":
            raise ApiError("没有需要更新的字段") from exc
        raise ApiError(message, 400) from exc
    except ValueError as exc:
        raise ApiError(str(exc), 400) from exc
    actor_id, actor_name = access.ctx.actor()
    activity.log(session, access.id, actor_id, actor_name, "updated", "issue", issue.id,
                 issue.title, note or "")
    counts = comment_counts(session, [issue.id])
    return {"issue": issue_dict(issue, counts.get(issue.id, 0))}


@router.delete("/issues/{number}")
def delete_issue(number: int, access: TeamAccess = Depends(team_access)) -> dict:
    access.require_edit()
    session = access.session
    issue = issue_service.find_by_number(session, access.id, number)
    if issue is None:
        raise ApiError("Issue 不存在", 404)
    if hierarchy.has_children(session, issue):
        raise ApiError(
            "该议题下还有子议题，请先迁移或删除子议题",
            status=409,
            code="has_children",
        )
    title = issue.title
    issue_id = issue.id
    session.delete(issue)
    session.flush()
    actor_id, actor_name = access.ctx.actor()
    activity.log(session, access.id, actor_id, actor_name, "deleted", "issue", issue_id, title)
    return {"ok": True}


@router.post("/issues/{number}/comments")
def create_comment(number: int, body: dict = Depends(read_body),
                   access: TeamAccess = Depends(team_access)) -> dict:
    access.require_edit()
    session = access.session
    issue = issue_service.find_by_number(session, access.id, number)
    if issue is None:
        raise ApiError("Issue 不存在", 404)
    text = str(body.get("body") or "").strip()
    if not text:
        raise ApiError("评论不能为空")
    actor_id, actor_name = access.ctx.actor()
    now = utcnow_iso()
    comment = Comment(
        issue_id=issue.id,
        user_id=actor_id,
        author_name=actor_name,
        body=text[:20000],
        created_at=now,
    )
    session.add(comment)
    issue.updated_at = now
    session.flush()
    activity.log(session, access.id, actor_id, actor_name, "commented", "issue", issue.id,
                 issue.title)
    return {"comment": comment_dict(comment)}
