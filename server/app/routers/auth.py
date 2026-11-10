"""登录 / 注册 / 退出。"""
from __future__ import annotations

import datetime as _dt
import re

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import func, select

from ..config import LOGIN_MAX_ATTEMPTS, LOGIN_WINDOW_SECONDS, SESSION_DAYS
from ..db.base import utcnow_iso
from ..db.models import SessionRow, Team, TeamMember, User
from ..db.session import get_db
from ..deps import Ctx, get_ctx, read_body
from ..errors import ApiError
from ..security import (
    SID_COOKIE, client_ip, cookie_secure, hash_password, new_token, verify_password,
)
from ..services import ratelimit
from ..services import settings as settings_service
from ..services import teams as team_service
from ..services.serializers import public_user

router = APIRouter(prefix="/api/auth", tags=["auth"])

USERNAME_RE = re.compile(r"[a-z0-9_.-]{3,24}")


def _guard(request: Request, subject: str, limit: int | None = None) -> str:
    """限流：返回用于成功时清账的 key。"""
    limit = LOGIN_MAX_ATTEMPTS if limit is None else limit
    key = ratelimit.key_of(client_ip(request), subject)
    allowed, wait = ratelimit.allow(key, limit, LOGIN_WINDOW_SECONDS)
    if not allowed:
        raise ApiError(f"尝试次数过多，请 {max(1, int(wait))} 秒后再试", 429)
    return key


def _issue_session(session, user: User, response: Response, request: Request) -> str:
    token = new_token(32)
    expires = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(days=SESSION_DAYS)
    session.add(SessionRow(
        token=token,
        user_id=user.id,
        created_at=utcnow_iso(),
        expires_at=expires.strftime("%Y-%m-%dT%H:%M:%SZ"),
    ))
    session.flush()
    response.set_cookie(
        SID_COOKIE, token, max_age=SESSION_DAYS * 86400,
        httponly=True, samesite="lax", path="/",
        secure=cookie_secure(request),
    )
    return token


@router.post("/login")
def login(request: Request, response: Response, body: dict = Depends(read_body),
          session=Depends(get_db)) -> dict:
    username = str(body.get("username") or "").strip().lower()
    password = str(body.get("password") or "")
    if not username or not password:
        raise ApiError("请输入用户名和密码")
    key = _guard(request, username)
    user = session.scalar(select(User).where(User.username == username))
    if user is None or not user.password_hash or not verify_password(password, user.password_hash):
        raise ApiError("用户名或密码不正确", 401)
    ratelimit.forget(key)
    _issue_session(session, user, response, request)
    return {"user": public_user(user)}


@router.post("/register")
def register(request: Request, response: Response, body: dict = Depends(read_body),
             ctx: Ctx = Depends(get_ctx)) -> dict:
    session = ctx.session
    if not ctx.require_login:
        raise ApiError("当前无需登录即可使用，开启登录模式后才能注册")

    username = str(body.get("username") or "").strip().lower()
    password = str(body.get("password") or "")
    display_name = str(body.get("display_name") or "").strip()[:40]

    key = _guard(request, f"register:{username}", limit=LOGIN_MAX_ATTEMPTS * 2)

    if not USERNAME_RE.fullmatch(username):
        raise ApiError("用户名需为 3-24 位字母、数字或 _ . -")
    if len(password) < 6:
        raise ApiError("密码至少 6 位")
    if session.scalar(select(User.id).where(User.username == username)) is not None:
        raise ApiError("该用户名已被占用", 409)

    is_first = (session.scalar(select(func.count()).select_from(User)) or 0) == 0
    user = User(
        username=username,
        display_name=display_name or username,
        title="团队成员",
        email=f"{username}@example.com",
        password_hash=hash_password(password),
        created_at=utcnow_iso(),
    )
    session.add(user)
    session.flush()

    if is_first:
        for team in session.scalars(select(Team)):
            has_member = session.scalar(
                select(TeamMember.user_id).where(TeamMember.team_id == team.id)
            )
            if has_member is None:
                session.add(TeamMember(team_id=team.id, user_id=user.id, role="owner",
                                       joined_at=utcnow_iso()))
    if settings_service.flag(session, "claim_default_team"):
        team = team_service.default_team(session)
        if team is not None and not team_service.user_is_member(session, team.id, user.id):
            session.add(TeamMember(team_id=team.id, user_id=user.id, role="owner",
                                   joined_at=utcnow_iso()))
        settings_service.set_flag(session, "claim_default_team", False)

    ratelimit.forget(key)
    _issue_session(session, user, response, request)
    return {"user": public_user(user)}


@router.post("/logout")
def logout(request: Request, response: Response, session=Depends(get_db)) -> dict:
    token = request.cookies.get(SID_COOKIE)
    if token:
        row = session.get(SessionRow, token)
        if row is not None:
            session.delete(row)
    response.delete_cookie(SID_COOKIE, path="/")
    return {"ok": True}
