"""系统级端点：健康检查与前端启动所需的 bootstrap。"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select

from ..config import APP_NAME, APP_VERSION, SESSION_DAYS
from ..db.models import User
from ..db.seed import DEMO_PASSWORD, DEMO_USERS
from ..deps import Ctx, edit_enabled, edit_locked, get_ctx
from ..services import settings as settings_service
from ..services import teams as team_service
from ..services.serializers import public_user, team_summary

router = APIRouter(tags=["system"])


@router.get("/api/health")
def health() -> dict:
    return {"ok": True, "app": APP_NAME, "version": APP_VERSION}


@router.get("/api/bootstrap")
def bootstrap(ctx: Ctx = Depends(get_ctx)) -> dict:
    session = ctx.session
    memberships = team_service.memberships(session, ctx.user_id)
    all_teams = team_service.all_teams(session)
    if ctx.require_login:
        all_teams = [team for team in all_teams if team.id in memberships]
    teams = [
        team_summary(team, team_service.display_role(memberships.get(team.id), ctx.require_login))
        for team in all_teams
    ]

    last_team_id = None
    active = team_service.active_team(session, ctx.user_id)
    if active is not None:
        last_team_id = active.id
    elif teams:
        last_team_id = teams[0]["id"]

    demo_seeded = settings_service.flag(session, "demo_seeded")
    demo_accounts = []
    if demo_seeded:
        usernames = [username for username, _display, _title in DEMO_USERS]
        demo_accounts = [
            {"id": user.id, "username": user.username,
             "display_name": user.display_name, "title": user.title}
            for user in session.scalars(
                select(User).where(User.username.in_(usernames)).order_by(User.id)
            )
        ]

    return {
        "require_login": ctx.require_login,
        "user": public_user(ctx.user),
        "teams": teams,
        "last_team_id": last_team_id,
        "edit_protected": (not ctx.require_login) and edit_enabled(session),
        "edit_locked": edit_locked(ctx),
        "demo_accounts": demo_accounts,
        "demo_password": DEMO_PASSWORD if demo_seeded else None,
        "session_days": SESSION_DAYS,
    }
