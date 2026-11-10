"""FastAPI 依赖：请求上下文、登录用户、团队访问控制、请求体解析。

所有权限判断都集中在这里，路由函数只表达业务意图：

    access = Depends(team_access)      # 自动解析 {team} 并算出角色
    access.require_edit()              # 没有编辑权限时抛 403
"""
from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import ADMIN_ROLES, EDITABLE_ROLES
from .db.base import utcnow_iso
from .db.models import EditSession, SessionRow, Team, User
from .db.session import get_db
from .errors import ApiError
from .security import EDIT_COOKIE, SID_COOKIE
from .services import settings as settings_service
from .services import teams as team_service
from .services.serializers import public_user


# ------------------------------------------------------------------ 请求上下文

@dataclass
class Ctx:
    request: Request
    session: Session
    user: User | None
    require_login: bool

    # ---- 当前用户
    @property
    def user_id(self) -> int | None:
        return self.user.id if self.user is not None else None

    def actor(self) -> tuple[int | None, str]:
        """动态流的署名者：登录用户 > X-Actor-Id 头里的成员 > 访客。"""
        if self.user is not None:
            return self.user.id, self.user.display_name
        header = self.request.headers.get("x-actor-id", "").strip()
        if header.isdigit():
            user = self.session.get(User, int(header))
            if user is not None:
                return user.id, user.display_name
        return None, "访客"

    def require_user(self) -> User:
        if self.user is None:
            raise ApiError("请先登录", 401)
        return self.user


def session_user(session: Session, token: str | None) -> User | None:
    if not token:
        return None
    row = session.get(SessionRow, token)
    if row is None or row.expires_at <= utcnow_iso():
        return None
    return session.get(User, row.user_id)


def get_ctx(request: Request, session: Session = Depends(get_db)) -> Ctx:
    return Ctx(
        request=request,
        session=session,
        user=session_user(session, request.cookies.get(SID_COOKIE)),
        require_login=settings_service.require_login_on(session),
    )


# ------------------------------------------------------------------ 编辑口令

def edit_enabled(session: Session) -> bool:
    return bool(settings_service.get_setting(session, "edit_password"))


def edit_unlocked(session: Session, token: str | None) -> bool:
    if not token:
        return False
    row = session.get(EditSession, token)
    return row is not None and row.expires_at > utcnow_iso()


def edit_locked(ctx: Ctx) -> bool:
    """单团队模式 + 设置了编辑口令 + 没解锁 → 所有写操作都要口令。"""
    if ctx.require_login or not edit_enabled(ctx.session):
        return False
    token = ctx.request.cookies.get(EDIT_COOKIE)
    return not edit_unlocked(ctx.session, token)


# ------------------------------------------------------------------ 团队访问

@dataclass
class TeamAccess:
    ctx: Ctx
    team: Team
    role: str | None

    @property
    def session(self) -> Session:
        return self.ctx.session

    @property
    def id(self) -> int:
        return self.team.id

    @property
    def slug(self) -> str:
        return self.team.slug

    # ---- 角色
    def effective_role(self) -> str | None:
        """单团队模式下（无需登录）把匿名访问者当作所有者。"""
        if self.role is not None:
            return self.role
        if not self.ctx.require_login:
            return "owner"
        return None

    def is_admin(self) -> bool:
        return self.effective_role() in ADMIN_ROLES

    def can_edit(self) -> bool:
        return self.effective_role() in EDITABLE_ROLES

    def is_public(self, section: str) -> bool:
        return bool(getattr(self.team, f"{section}_public", 0))

    def can_see(self, section: str) -> bool:
        return self.can_edit() or self.is_public(section)

    # ---- 断言
    def require_read(self, section: str) -> None:
        if not self.can_see(section):
            raise ApiError("该内容未对外公开", 403)

    def require_edit(self) -> None:
        if not self.can_edit():
            raise ApiError("没有编辑权限", 403)

    def require_admin(self) -> None:
        if not self.is_admin():
            raise ApiError("需要管理员权限", 403)

    def member_or_404(self, username: str) -> User:
        user = team_service.find_user(self.session, username)
        if user is None or not team_service.user_is_member(self.session, self.id, user.id):
            raise ApiError("成员不存在", 404)
        return user


def team_access(team: str, ctx: Ctx = Depends(get_ctx)) -> TeamAccess:
    """路径参数 ``{team}`` → 团队 + 当前用户在团队里的角色。"""
    found = team_service.resolve(ctx.session, team)
    if found is None:
        raise ApiError("团队不存在", 404)
    role = team_service.role_of(ctx.session, found.id, ctx.user_id)
    return TeamAccess(ctx=ctx, team=found, role=role)


# ------------------------------------------------------------------ 请求体

async def read_body(request: Request) -> dict:
    """同时兼容 JSON 与表单/multipart（截图导入用 multipart）。"""
    content_type = request.headers.get("content-type", "")
    if "multipart/form-data" in content_type or "application/x-www-form-urlencoded" in content_type:
        form = await request.form()
        payload: dict = {}
        for key, value in form.multi_items():
            if hasattr(value, "filename"):        # UploadFile
                payload[key] = value
            elif key in payload:
                existing = payload[key]
                payload[key] = existing + [value] if isinstance(existing, list) else [existing, value]
            else:
                payload[key] = value
        return payload
    raw = await request.body()
    if not raw:
        return {}
    import json

    try:
        data = json.loads(raw)
    except ValueError:
        raise ApiError("请求体格式不正确", 400)
    if not isinstance(data, dict):
        raise ApiError("请求体格式不正确", 400)
    return data


def body_field(body: dict, name: str, default=None):
    value = body.get(name, default)
    return default if value is None else value


def current_user_payload(ctx: Ctx) -> dict | None:
    return public_user(ctx.user)


__all__ = [
    "Ctx",
    "TeamAccess",
    "body_field",
    "current_user_payload",
    "edit_enabled",
    "edit_locked",
    "edit_unlocked",
    "get_ctx",
    "read_body",
    "session_user",
    "team_access",
]
