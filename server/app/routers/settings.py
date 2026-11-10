"""设置类端点：登录模式开关、编辑口令（单团队模式）、活动流、路线图。"""
from __future__ import annotations

import datetime as _dt

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select

from ..config import EDIT_COOKIE_HOURS
from ..db.base import utcnow_iso
from ..db.models import EditSession
from ..deps import Ctx, edit_enabled, edit_locked, get_ctx, read_body
from ..errors import ApiError
from ..security import (
    EDIT_COOKIE, cookie_secure, hash_password, new_token, verify_password,
)
from ..services import activity
from ..services import settings as settings_service
from ..services import teams as team_service

router = APIRouter(prefix="/api", tags=["settings"])


def _iso_in(hours: int) -> str:
    moment = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(hours=hours)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "on", "yes")


# ---------------------------------------------------------------- 登录模式

@router.post("/settings/require-login")
def set_require_login(body: dict = Depends(read_body),
                      ctx: Ctx = Depends(get_ctx)) -> dict:
    session = ctx.session
    wanted = body.get("enabled", body.get("require_login"))
    enabled = _truthy(wanted) if wanted is not None else not ctx.require_login

    if enabled:
        if not ctx.require_login:
            settings_service.set_setting(session, "claim_default_team", "1")
            team = team_service.default_team(session)
            if team is not None and settings_service.default_team_id(session) is None:
                settings_service.set_setting(session, "default_team_id", team.id)
    else:
        user = ctx.require_user()
        if not team_service.is_admin_everywhere(session, user.id):
            raise ApiError("需要团队所有者权限", 403)

    settings_service.set_flag(session, "require_login", enabled)
    team = team_service.default_team(session)
    actor_id, actor_name = ctx.actor()
    activity.log(
        session, team.id if team is not None else 0, actor_id, actor_name,
        "updated", "setting", None, "登录设置",
        "开启登录" if enabled else "关闭登录（单团队模式）",
    )
    return {"require_login": enabled}


# ---------------------------------------------------------------- 编辑口令

@router.get("/edit/status")
def edit_status(ctx: Ctx = Depends(get_ctx)) -> dict:
    return {"enabled": edit_enabled(ctx.session), "locked": edit_locked(ctx)}


def _unlock_cookie(response: Response, token: str, request: Request) -> None:
    response.set_cookie(
        EDIT_COOKIE, token, max_age=EDIT_COOKIE_HOURS * 3600,
        httponly=True, samesite="lax", path="/",
        secure=cookie_secure(request),
    )


@router.post("/edit/unlock")
def edit_unlock(response: Response, body: dict = Depends(read_body),
                ctx: Ctx = Depends(get_ctx)) -> dict:
    session = ctx.session
    stored = settings_service.get_setting(session, "edit_password")
    if not stored:
        return {"ok": True}
    password = str(body.get("password") or "")
    if not password or not verify_password(password, stored):
        raise ApiError("口令不正确", 403)
    token = new_token(24)
    session.add(EditSession(token=token, created_at=utcnow_iso(),
                            expires_at=_iso_in(EDIT_COOKIE_HOURS)))
    session.flush()
    _unlock_cookie(response, token, ctx.request)
    return {"ok": True}


@router.post("/edit/password")
def edit_password(response: Response, body: dict = Depends(read_body),
                  ctx: Ctx = Depends(get_ctx)) -> dict:
    session = ctx.session
    if ctx.require_login:
        raise ApiError("仅单团队模式支持编辑口令")
    new_password = str(body.get("next") or "")

    if new_password:
        if len(new_password) < 4:
            raise ApiError("口令至少 4 位")
        settings_service.set_setting(session, "edit_password", hash_password(new_password))
        for row in session.scalars(select(EditSession)):
            session.delete(row)
        session.flush()
        token = new_token(24)
        session.add(EditSession(token=token, created_at=utcnow_iso(),
                                expires_at=_iso_in(EDIT_COOKIE_HOURS)))
        session.flush()
        _unlock_cookie(response, token, ctx.request)
    else:
        settings_service.del_setting(session, "edit_password")
        for row in session.scalars(select(EditSession)):
            session.delete(row)
        session.flush()
        response.delete_cookie(EDIT_COOKIE, path="/")

    return {"enabled": bool(new_password)}
