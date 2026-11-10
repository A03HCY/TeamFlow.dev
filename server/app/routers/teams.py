"""团队、成员、概览、活动流、路线图、重置。"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import delete, select

from ..db.base import utcnow_iso
from ..db.models import (
    Activity,
    Comment,
    EditSession,
    Issue,
    Label,
    Milestone,
    ScheduleEntry,
    SessionRow,
    Team,
    TeamMember,
    User,
    issue_labels,
    schedule_participants,
)
from ..deps import Ctx, TeamAccess, get_ctx, read_body, team_access
from ..errors import ApiError
from ..services import activity
from ..services import overview as overview_service
from ..services import settings as settings_service
from ..services import teams as team_service
from ..services.serializers import team_summary
from ..services.milestones import listing as milestone_listing

router = APIRouter(prefix="/api", tags=["teams"])


# ---------------------------------------------------------------- 团队列表

@router.get("/teams")
def list_teams(ctx: Ctx = Depends(get_ctx)) -> dict:
    session = ctx.session
    memberships = team_service.memberships(session, ctx.user_id)
    teams = team_service.all_teams(session)
    if ctx.require_login:
        teams = [team for team in teams if team.id in memberships]
    return {"teams": [
        team_summary(team, team_service.display_role(memberships.get(team.id), ctx.require_login))
        for team in teams
    ]}


@router.post("/teams")
def create_team(body: dict = Depends(read_body), ctx: Ctx = Depends(get_ctx)) -> dict:
    session = ctx.session
    if not ctx.require_login:
        raise ApiError("关闭登录模式时全平台只有一个团队，无法新建团队", 403)
    user = ctx.require_user()
    name = str(body.get("name") or "").strip()
    if not name:
        raise ApiError("请填写团队名称")
    wanted_slug = str(body.get("slug") or "").strip()
    if wanted_slug:
        try:
            slug = team_service.clean_slug(wanted_slug)
        except ValueError as exc:
            raise ApiError(str(exc))
        if team_service.find_by_slug(session, slug) is not None:
            raise ApiError("该团队地址已被占用", 409)
    else:
        slug = team_service.unique_slug(session, name)
    team = team_service.create_team(
        session, name[:60], slug,
        str(body.get("description") or "")[:500], user.id,
    )
    activity.log(session, team.id, user.id, user.display_name, "created", "team", team.id,
                 team.name)
    return {"team": team_summary(team, "owner")}


# ---------------------------------------------------------------- 单个团队

@router.get("/teams/{team}")
def team_detail(access: TeamAccess = Depends(team_access)) -> dict:
    if not access.can_edit() and not any(
        access.is_public(section)
        for section in ("issues", "milestones", "roadmap", "schedules")
    ):
        raise ApiError("该团队未对外公开", 403)
    session = access.session
    # 前端把 detail 整个当作 S.detail 用（S.detail.members / S.detail.counts），
    # 所以成员与计数必须嵌在 team 里，保持与旧版一致的响应形状。
    payload = team_summary(access.team, access.effective_role())
    payload["members"] = (
        team_service.member_rows(session, access.id) if access.can_edit() else []
    )
    payload["counts"] = team_service.counts(session, access.id)
    payload["is_member"] = access.effective_role() is not None
    payload["can_edit"] = access.can_edit()
    return {"team": payload}


@router.patch("/teams/{team}")
def update_team(body: dict = Depends(read_body),
                access: TeamAccess = Depends(team_access)) -> dict:
    access.require_admin()
    session, team = access.session, access.team
    changed_public = False

    if "name" in body:
        name = str(body.get("name") or "").strip()
        if not name:
            raise ApiError("请填写团队名称")
        team.name = name[:60]
    if "slug" in body:
        wanted = str(body.get("slug") or "")
        try:
            new_slug = team_service.clean_slug(wanted)
        except ValueError as exc:
            raise ApiError(str(exc))
        if new_slug != team.slug:
            existing = team_service.find_by_slug(session, new_slug)
            if existing is not None and existing.id != team.id:
                raise ApiError("该团队地址已被占用", 409)
            team.slug = new_slug
    if "description" in body:
        team.description = str(body.get("description") or "")[:500]
    for flag_name in ("issues_public", "milestones_public", "roadmap_public",
                      "schedules_public"):
        if flag_name in body:
            value = 1 if body.get(flag_name) in (True, 1, "1", "true", "on") else 0
            if value != getattr(team, flag_name):
                changed_public = True
            setattr(team, flag_name, value)

    actor_id, actor_name = access.ctx.actor()
    if changed_public:
        activity.log(session, team.id, actor_id, actor_name, "updated", "team", team.id,
                     team.name, "调整了对外公开范围")
    session.flush()
    return {"team": team_summary(team, access.effective_role())}


@router.delete("/teams/{team}")
def delete_team(access: TeamAccess = Depends(team_access)) -> dict:
    session = access.session
    if not access.ctx.require_login:
        raise ApiError("关闭登录模式时不能删除唯一的团队", 403)
    if access.effective_role() != "owner":
        raise ApiError("只有团队所有者可以删除团队", 403)
    team = access.team
    session.delete(team)
    session.flush()
    if settings_service.default_team_id(session) == team.id:
        replacement = session.scalar(select(Team).order_by(Team.id))
        if replacement is not None:
            settings_service.set_setting(session, "default_team_id", replacement.id)
    return {"ok": True}


@router.post("/teams/{team}/activate")
def activate_team(access: TeamAccess = Depends(team_access)) -> dict:
    user_id = access.ctx.user_id
    if not user_id:
        return {"ok": True}
    if not access.can_edit():
        raise ApiError("你不是该团队成员", 403)
    settings_service.set_setting(access.session, f"active_team_{user_id}", access.id)
    return {"ok": True}


# ---------------------------------------------------------------- 成员

@router.post("/teams/{team}/members")
def add_member(body: dict = Depends(read_body),
               access: TeamAccess = Depends(team_access)) -> dict:
    access.require_admin()
    session, team = access.session, access.team
    display_name = str(body.get("display_name") or "").strip()[:40]
    username = str(body.get("username") or "").strip().lower()
    title = str(body.get("title") or "").strip()[:40]
    role = "admin" if body.get("role") == "admin" else "member"

    def _payload(user, role_value: str) -> dict:
        # 与 GET /teams/{team} 的成员行同一形状（含 joined_at / can_login），
        # 前端拿到后可以直接塞进 S.detail.members。
        for row in team_service.member_rows(session, team.id):
            if row["id"] == user.id:
                return row
        return {"id": user.id, "username": user.username,
                "display_name": user.display_name, "title": user.title or "",
                "role": role_value, "joined_at": utcnow_iso(),
                "can_login": bool((user.password_hash or "").strip())}


    if username:
        existing = team_service.find_user(session, username)
        if existing is not None:
            if team_service.user_is_member(session, team.id, existing.id):
                raise ApiError("该用户已在团队中", 409)
            if title:
                existing.title = title
            if not existing.display_name and display_name:
                existing.display_name = display_name
            team_service.add_member(session, team, existing, role)
            session.flush()
            actor_id, actor_name = access.ctx.actor()
            activity.log(session, team.id, actor_id, actor_name, "added", "member",
                         existing.id, existing.display_name)
            return {"ok": True, "member": _payload(existing, role)}
        if not display_name:
            raise ApiError("该用户名还没注册，请填写姓名新建成员", 404)

    if not display_name:
        raise ApiError("请填写成员姓名")
    if team_service.has_same_display_name(session, team.id, display_name):
        raise ApiError("团队里已经有同名成员，换个名字区分一下", 409)
    if username and team_service.find_user(session, username) is not None:
        raise ApiError("该用户名已被占用", 409)
    user = team_service.create_member_user(session, display_name, username or None,
                                           title=title or "团队成员")
    team_service.add_member(session, team, user, role)
    actor_id, actor_name = access.ctx.actor()
    activity.log(session, team.id, actor_id, actor_name, "added", "member", user.id,
                 user.display_name)
    return {"ok": True, "member": _payload(user, role)}


@router.patch("/teams/{team}/members/{username}")
def update_member(username: str, body: dict = Depends(read_body),
                  access: TeamAccess = Depends(team_access)) -> dict:
    access.require_admin()
    session, team = access.session, access.team
    user = access.member_or_404(username)
    current = team_service.role_of(session, team.id, user.id)
    changed: list[str] = []

    new_name = None
    if "display_name" in body:
        new_name = str(body.get("display_name") or "").strip()[:40]
        if not new_name:
            raise ApiError("请填写成员姓名")
        if new_name != user.display_name:
            if team_service.has_same_display_name(session, team.id, new_name):
                raise ApiError("团队里已经有同名成员，换个名字区分一下", 409)
            changed.append("姓名")

    new_title = None
    if "title" in body:
        new_title = str(body.get("title") or "").strip()[:40]
        if new_title != (user.title or ""):
            changed.append("职位")

    role = None
    if "role" in body:
        role = str(body.get("role") or "")
        if role not in ("admin", "member"):
            raise ApiError("角色只能是 admin 或 member")
        if current == "owner":
            raise ApiError("不能修改团队所有者的角色", 403)
        if role != current:
            changed.append("角色")

    if not changed:
        raise ApiError("没有需要更新的字段")

    if new_name is not None:
        user.display_name = new_name
    if new_title is not None:
        user.title = new_title
    if role is not None:
        team_service.set_role(session, team, user, role)
    session.flush()

    actor_id, actor_name = access.ctx.actor()
    activity.log(session, team.id, actor_id, actor_name, "updated", "member", user.id,
                 user.display_name, "更新了" + "、".join(changed))
    return {"ok": True, "member": {"id": user.id, "username": user.username,
                                   "display_name": user.display_name,
                                   "title": user.title or "",
                                   "role": current if role is None else role,
                                   "can_login": bool((user.password_hash or "").strip())}}


@router.delete("/teams/{team}/members/{username}")
def remove_member(username: str, access: TeamAccess = Depends(team_access)) -> dict:
    session, team = access.session, access.team
    user = access.member_or_404(username)
    is_self = access.ctx.user_id == user.id
    if not is_self:
        access.require_admin()
    if team_service.role_of(session, team.id, user.id) == "owner":
        raise ApiError("不能移除团队所有者", 403)
    team_service.remove_member(session, team, user)
    if not user.password_hash:
        remaining = session.scalar(select(TeamMember.team_id).where(TeamMember.user_id == user.id))
        if remaining is None:
            session.delete(user)
            session.flush()
    actor_id, actor_name = access.ctx.actor()
    activity.log(session, team.id, actor_id, actor_name, "removed", "member", user.id,
                 user.display_name)
    return {"ok": True}


# ---------------------------------------------------------------- 概览 / 活动 / 路线图

@router.get("/teams/{team}/overview")
def overview(access: TeamAccess = Depends(team_access)) -> dict:
    if not (access.can_edit() or access.is_public("issues") or access.is_public("milestones")):
        raise ApiError("该内容未对外公开", 403)
    return overview_service.overview(access.session, access.team, access.effective_role())


@router.get("/teams/{team}/activity")
def activity_feed(limit: int = 30, access: TeamAccess = Depends(team_access)) -> dict:
    if not access.can_edit() and not any(
        access.is_public(section)
        for section in ("issues", "milestones", "roadmap", "schedules")
    ):
        raise ApiError("该团队未对外公开", 403)
    entries = activity.recent(access.session, access.id, max(1, min(limit, 200)))
    return {"activity": [activity.serialize(entry) for entry in entries]}


@router.get("/teams/{team}/roadmap")
def roadmap(access: TeamAccess = Depends(team_access)) -> dict:
    access.require_read("roadmap")
    return {
        "milestones": milestone_listing(access.session, access.id, roadmap=True),
        "can_edit": access.can_edit(),
        "team": team_summary(access.team, access.effective_role()),
    }


# ---------------------------------------------------------------- 重置

@router.post("/reset")
def reset(body: dict = Depends(read_body), ctx: Ctx = Depends(get_ctx)) -> dict:
    if body.get("confirm") != "DELETE":
        raise ApiError("需要确认")
    session = ctx.session
    if ctx.require_login:
        user = ctx.require_user()
        if not session.scalar(
            select(TeamMember.team_id).where(
                TeamMember.user_id == user.id, TeamMember.role == "owner"
            )
        ):
            raise ApiError("需要团队所有者权限", 403)
    for table in (issue_labels, schedule_participants):
        session.execute(delete(table))
    for model in (Comment, Activity, Issue, Milestone, Label, ScheduleEntry,
                  TeamMember, SessionRow, EditSession, Team, User):
        session.execute(delete(model))
    settings_service.set_setting(session, "demo_seeded", "0")
    settings_service.set_setting(session, "require_login", "0")
    team = team_service.create_team(session, "我的团队", "main", "在这里描述团队目标。")
    settings_service.set_setting(session, "default_team_id", team.id)
    return {"ok": True, "team_id": team.id}
