"""AI 配置与助手端点。

* ``/api/ai/config``：全局 AI 配置（管理员）。
* ``/api/teams/{team}/ai``：团队 AI 助手（常驻对话框）。
* ``/api/ai/project/*``：新创项目组的草稿生成与一次性落库。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select

from ..db.models import TeamMember
from ..deps import Ctx, TeamAccess, get_ctx, read_body, team_access
from ..errors import ApiError
from ..security import client_ip
from ..services import activity
from ..services import ai
from ..services import assistant
from ..services import ratelimit
from ..services import settings as settings_service
from ..services import teams as team_service
from ..services.serializers import team_summary

router = APIRouter(prefix="/api/ai", tags=["ai"])
team_router = APIRouter(prefix="/api/teams/{team}", tags=["ai"])

CHAT_LIMIT = 20
CHAT_WINDOW = 60.0


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


# ---------------------------------------------------------------- 团队助手


QUICK_PROMPTS = [
    {"key": "progress", "label": "概括当前进度", "prompt": "用三句话概括这个团队当前的进度和风险。"},
    {"key": "milestone", "label": "建议里程碑", "prompt": "根据现有议题，建议接下来的里程碑怎么切，每个里程碑的验收标准是什么？"},
    {"key": "week", "label": "安排本周", "prompt": "帮我安排本周计划：哪些议题先做、哪些可以往后放？给出理由。"},
    {"key": "project", "label": "起草新项目", "prompt": "我想新建一个项目组，请先问我这个项目要解决什么问题。"},
]


def _limit(ctx: Ctx, subject: str) -> None:
    allowed, retry_after = ratelimit.allow(
        ratelimit.key_of(client_ip(ctx.request), subject), CHAT_LIMIT, CHAT_WINDOW
    )
    if not allowed:
        raise ApiError(
            f"AI 请求太频繁，请 {int(retry_after) + 1} 秒后再试", 429, code="rate_limited"
        )


@team_router.get("/ai")
def assistant_status(access: TeamAccess = Depends(team_access)) -> dict:
    access.require_read("issues")
    cfg = ai.get_config(access.session)
    return {
        "configured": ai.is_configured(cfg),
        "model": cfg["model"],
        "quick_prompts": QUICK_PROMPTS,
        "modes": [{"value": key, "label": value["label"]}
                  for key, value in assistant.MODES.items()],
        "can_edit": access.can_edit(),
    }


@team_router.post("/ai/chat")
def assistant_chat(body: dict = Depends(read_body),
                   access: TeamAccess = Depends(team_access)) -> dict:
    access.require_read("issues")
    _limit(access.ctx, "ai-chat")
    session = access.session
    actor_id, actor_name = access.ctx.actor()
    messages = body.get("messages")
    message = str(body.get("message") or "").strip()
    session_id = body.get("session_id")
    try:
        session_id = int(session_id) if session_id not in (None, "") else None
    except (TypeError, ValueError):
        raise ApiError("会话编号不正确") from None
    if not message and not messages:
        raise ApiError("请先输入要问的内容", 400, code="ai_bad_request")
    result = assistant.chat_turn(
        session,
        team=access.team,
        message=message,
        messages=messages,
        session_id=session_id,
        context=body.get("context"),
        user_id=access.ctx.user.id if access.ctx.user is not None else None,
        mode=str(body.get("mode") or "ask"),
    )
    ask = message or ""
    if not ask and isinstance(messages, list) and messages and isinstance(messages[-1], dict):
        ask = str(messages[-1].get("content") or "")
    activity.log(session, access.team.id, actor_id, actor_name, "asked", "ai", None,
                 "AI 助手", ask[:180])
    return result


# ---------------------------------------------------------------- AI 工作台会话


@team_router.get("/ai/sessions")
def list_ai_sessions(limit: int = 20, access: TeamAccess = Depends(team_access)) -> dict:
    access.require_read("issues")
    rows = assistant.list_sessions(access.session, team_id=access.team.id, limit=limit)
    return {"sessions": rows, "modes": [
        {"value": key, "label": value["label"]} for key, value in assistant.MODES.items()
    ]}


@team_router.post("/ai/sessions")
def create_ai_session(body: dict = Depends(read_body),
                      access: TeamAccess = Depends(team_access)) -> dict:
    access.require_read("issues")
    user = access.ctx.user
    row = assistant.create_session(
        access.session,
        team=access.team,
        user_id=user.id if user is not None else None,
        title=str(body.get("title") or "").strip(),
        mode=str(body.get("mode") or "ask"),
    )
    return {"session": assistant.session_dict(row)}


@team_router.get("/ai/sessions/{session_id}")
def get_ai_session(session_id: int, access: TeamAccess = Depends(team_access)) -> dict:
    access.require_read("issues")
    return assistant.session_detail(access.session, team_id=access.team.id, session_id=session_id)


@team_router.delete("/ai/sessions/{session_id}")
def remove_ai_session(session_id: int, access: TeamAccess = Depends(team_access)) -> dict:
    access.require_read("issues")
    assistant.delete_session(access.session, team_id=access.team.id, session_id=session_id)
    return {"ok": True}


@router.post("/project/draft")
def project_draft(body: dict = Depends(read_body), ctx: Ctx = Depends(get_ctx)) -> dict:
    goal = str(body.get("goal") or "").strip()
    hint = str(body.get("name") or "").strip()
    return assistant.draft_project(ctx.session, goal=goal, name_hint=hint)


@router.post("/project/create")
def project_create(body: dict = Depends(read_body), ctx: Ctx = Depends(get_ctx)) -> dict:
    if not ctx.require_login:
        raise ApiError("关闭登录模式时全平台只有一个团队，请先在设置里开启登录模式", 403)
    user = ctx.require_user()
    draft = body.get("draft")
    if not isinstance(draft, dict):
        raise ApiError("缺少项目草稿")
    clean = assistant.normalize_draft(draft, fallback_name=str(body.get("name") or ""))
    if not clean["issues"] and not clean["milestones"]:
        raise ApiError("草稿里没有里程碑或议题，无法创建")
    session = ctx.session
    result = assistant.create_project(
        session, clean, owner_id=user.id, actor=(user.id, user.display_name),
    )
    team = result["team"]
    return {
        "ok": True,
        "team": team_summary(team, "owner"),
        "milestones": result["milestones"],
        "issues": result["issues"],
        "warnings": clean["warnings"],
    }
