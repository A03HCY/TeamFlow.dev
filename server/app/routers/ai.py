"""AI 配置端点（全局设置，仅团队管理员可改）。"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select

from ..db.models import TeamMember
from ..deps import Ctx, get_ctx, read_body
from ..errors import ApiError
from ..services import activity
from ..services import ai
from ..services import settings as settings_service
from ..services import teams as team_service

router = APIRouter(prefix="/api/ai", tags=["ai"])


def require_platform_admin(ctx: Ctx) -> None:
    """AI 配置是全局设置：登录模式下需要任一团队的管理员；免登录模式由编辑口令把关。"""
    if ctx.user is None:
        return
    ok = ctx.session.scalar(
        select(TeamMember.team_id).where(
            TeamMember.user_id == ctx.user.id, TeamMember.role.in_(("owner", "admin"))
        )
    )
    if ok is None:
        raise ApiError("需要团队管理员权限", 403)


@router.get("/config")
def get_config(ctx: Ctx = Depends(get_ctx)) -> dict:
    if ctx.require_login and ctx.user is None:
        raise ApiError("请先登录", 401)
    config = ai.get_config(ctx.session)
    key = config["api_key"]
    masked = (key[:4] + "****" + key[-4:]) if len(key) > 8 else ("****" if key else "")
    return {
        "configured": ai.is_configured(config),
        "base_url": config["base_url"],
        "model": config["model"],
        "api_key_masked": masked,
    }


@router.post("/config")
def set_config(body: dict = Depends(read_body), ctx: Ctx = Depends(get_ctx)) -> dict:
    require_platform_admin(ctx)
    session = ctx.session
    base_url = str(body.get("base_url") or "").strip()
    model = str(body.get("model") or "").strip()
    api_key = str(body.get("api_key") or "").strip()
    if base_url:
        settings_service.set_setting(session, "ai_base_url", base_url)
    if model:
        settings_service.set_setting(session, "ai_model", model)
    if api_key:
        settings_service.set_setting(session, "ai_api_key", api_key)
    config = ai.get_config(session)
    team = team_service.default_team(session)
    actor_id, actor_name = ctx.actor()
    activity.log(session, team.id if team is not None else 0, actor_id, actor_name,
                 "updated", "setting", None, "AI 导入配置",
                 "已配置" if ai.is_configured(config) else "配置不完整")
    return {"configured": ai.is_configured(config)}
