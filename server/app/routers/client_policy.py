"""客户端采集策略：管理端配置、端侧读取、端侧日志上行。

三组端点：

* ``/api/teams/{team}/client-policy``：管理员在「权限」页读写策略；
* ``/api/teams/{team}/client-logs``：权限页展示最近上报的日志；
* ``/api/edge/policy`` + ``/api/edge/logs``：端侧只带 ``token`` 调用，
  不需要登录会话（与 WS 网关同一套设备令牌）。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query

from ..db.base import utcnow_iso
from ..db.engine_models import CodeEvent
from ..db.models import ClientLog, Team
from ..db.session import session_scope
from ..deps import Ctx, TeamAccess, get_ctx, read_body, team_access
from ..errors import ApiError
from ..services.edge import devices as device_service
from ..services.edge import pairing
from ..services.edge.ingest import ingest
from ..services.inference import rules as inference_rules
from ..services.inference.events import EventRejected, parse_envelope
from ..services.log_analysis import analyze_log

log = logging.getLogger("teamflow.client_policy")

router = APIRouter(prefix="/api", tags=["client-policy"])

DEFAULT_IDLE_MINUTES = 3
#: 早期版本的默认值：从未配置过的团队会被它拖住，演示时等 30 分钟也不会出洞察
LEGACY_IDLE_MINUTES = 30
MIN_IDLE_MINUTES = 1
MAX_IDLE_MINUTES = 24 * 60


def _idle_of(team) -> int:
    """团队配置的窗口时长；历史默认值且从未编辑过时按新默认走。"""
    try:
        value = int(getattr(team, "client_idle_minutes", 0) or 0)
    except (TypeError, ValueError):
        value = 0
    if value <= 0:
        return DEFAULT_IDLE_MINUTES
    if value == LEGACY_IDLE_MINUTES and not (getattr(team, "client_policy_updated_at", "") or ""):
        return DEFAULT_IDLE_MINUTES
    return value


def _policy_dict(team, *, can_admin: bool) -> dict:
    return {
        "idle_minutes": _idle_of(team),
        "allow_view": bool(getattr(team, "client_allow_view", 0)),
        "updated_at": getattr(team, "client_policy_updated_at", "") or "",
        "can_admin": bool(can_admin),
    }


def _log_dict(row: ClientLog) -> dict:
    return {
        "id": row.id,
        "device_id": row.device_id,
        "repo_label": row.repo_label or "",
        "branch": row.branch or "",
        "window_start": row.window_start or "",
        "window_end": row.window_end or "",
        "idle_minutes": int(row.idle_minutes or 0),
        "file_count": int(row.file_count or 0),
        "additions": int(row.additions or 0),
        "deletions": int(row.deletions or 0),
        "summary": row.summary or "",
        "analysis": row.analysis or "",
        "status": row.status or "",
        "created_at": row.created_at,
    }


# --------------------------------------------------------------- 管理端：策略

@router.get("/teams/{team}/client-policy")
def get_client_policy(access: TeamAccess = Depends(team_access)) -> dict:
    """读取团队当前的客户端策略（任何成员可读，只有管理员可改）。"""
    devices = []
    if access.is_admin():
        rows = device_service.list_devices(access.session, access.id, limit=20)
        for row in rows:
            # 在线判定：十分钟内有过心跳（端侧按窗口上报，REST 也会 touch）
            online = device_service.seen_within(row)
            count = (
                access.session.query(CodeEvent)
                .filter(CodeEvent.device_id == row.id)
                .count()
            )
            devices.append(device_service.device_dict(row, online=online, event_count=count))
    return {
        "policy": _policy_dict(access.team, can_admin=access.is_admin()),
        "devices": devices,
        "role": access.effective_role() or "",
    }


@router.put("/teams/{team}/client-policy")
async def update_client_policy(
    access: TeamAccess = Depends(team_access),
    ctx: Ctx = Depends(get_ctx),
) -> dict:
    """更新采集策略：无操作间隔时长 + 是否允许客户端查看变动内容。"""
    access.require_admin()
    body = await read_body(ctx.request)

    raw_idle = body.get("idle_minutes", DEFAULT_IDLE_MINUTES)
    try:
        idle = int(raw_idle)
    except (TypeError, ValueError) as exc:
        raise ApiError("间隔时长必须是整数分钟", 400) from exc
    if not MIN_IDLE_MINUTES <= idle <= MAX_IDLE_MINUTES:
        raise ApiError(f"间隔时长需在 {MIN_IDLE_MINUTES}～{MAX_IDLE_MINUTES} 分钟之间", 400)

    allow_raw = body.get("allow_view", False)
    if isinstance(allow_raw, str):
        allow = allow_raw.strip().lower() in {"1", "true", "on", "yes"}
    else:
        allow = bool(allow_raw)

    with session_scope() as session:
        team = session.merge(access.team)
        team.client_idle_minutes = idle
        team.client_allow_view = 1 if allow else 0
        team.client_policy_updated_at = utcnow_iso()
        session.flush()
        result = _policy_dict(team, can_admin=True)
    return {"ok": True, "policy": result}


# --------------------------------------------------------------- 管理端：日志

@router.get("/teams/{team}/client-logs")
def list_client_logs(
    access: TeamAccess = Depends(team_access),
    limit: int = Query(default=20, ge=1, le=100),
) -> dict:
    access.require_edit()
    with session_scope() as session:
        rows = (
            session.query(ClientLog)
            .filter(ClientLog.team_id == access.id)
            .order_by(ClientLog.id.desc())
            .limit(limit)
            .all()
        )
        items = [_log_dict(row) for row in rows]
    return {"logs": items, "count": len(items)}


# ----------------------------------------------------------------- 端侧：策略

@router.get("/edge/policy")
def edge_policy(token: str = Query(default="")) -> dict:
    """端侧用设备令牌换取当前策略（每次采集前拉一次，管理员改了立刻生效）。"""
    with session_scope() as session:
        device = device_service.find_by_token(session, token)
        device_service.require_usable(device)
        from ..db.models import Team, User  # 局部导入，避免循环

        team = session.get(Team, device.team_id)
        if team is None:
            raise ApiError("团队不存在", 404)
        owner = session.get(User, device.user_id) if device.user_id else None
        policy = _policy_dict(team, can_admin=False)
        device_service.touch(session, device)
        return {
            "ok": True,
            "team": team.slug,
            "team_name": team.name,
            "username": owner.username if owner is not None else "",
            "device": {"id": device.id, "name": device.name or ""},
            "policy": policy,
        }


@router.post("/edge/logs")
async def post_client_log(ctx: Ctx = Depends(get_ctx)) -> dict:
    """端侧上报一条「无人操作窗口」日志；带上 envelope 时顺带触发一次推断。"""
    body = await read_body(ctx.request)
    token = body.get("token") or ""
    payload = body.get("log") or {}
    envelope = body.get("envelope")

    with session_scope() as session:
        device = device_service.find_by_token(session, token)
        device_service.require_usable(device)

        row = ClientLog(
            team_id=device.team_id,
            device_id=device.id,
            repo_label=str(payload.get("repo") or "")[:120],
            branch=str(payload.get("branch") or "")[:120],
            window_start=str(payload.get("window_start") or "")[:32],
            window_end=str(payload.get("window_end") or "")[:32],
            idle_minutes=int(payload.get("idle_minutes") or DEFAULT_IDLE_MINUTES),
            file_count=int(payload.get("file_count") or 0),
            additions=int(payload.get("additions") or 0),
            deletions=int(payload.get("deletions") or 0),
            summary=str(payload.get("summary") or "")[:4000],
            status="received",
            created_at=utcnow_iso(),
        )
        session.add(row)
        session.flush()
        log_id = row.id

        ingest_result = None
        analysis = ""
        if envelope:
            try:
                parsed = parse_envelope(envelope)
                ingest_result = ingest(session, device=device, envelope=parsed, source="log")
                row.status = "ingested" if ingest_result.stored else str(ingest_result.status)
                # 实时分析：规则先给结论，配了模型再润色成话术（失败自动回退）
                verdict = inference_rules.analyze(parsed)
                analysis = analyze_log(session, row, verdict, parsed.files)
                row.analysis = analysis
            except EventRejected as exc:
                row.status = "rejected"
                log.warning("端侧日志携带的事件被拒收：%s", exc)
            except Exception:  # noqa: BLE001 - 日志上行不应因推断失败而丢
                row.status = "ingest_failed"
                log.exception("端侧日志入库失败（日志已保存）")
        else:
            row.analysis = analyze_log(session, row, {})

        device_service.touch(session, device)
        result = {
            "ok": True,
            "log_id": log_id,
            "status": row.status,
            "analysis": analysis,
            "event": None
            if ingest_result is None
            else {
                "stored": ingest_result.stored,
                "status": ingest_result.status,
                "event_id": ingest_result.event_id,
                "job_id": ingest_result.job_id,
                "issue_number": ingest_result.issue_number,
            },
        }
    return result


# ----------------------------------------------------------------- 端侧：配对

@router.post("/teams/{team}/engine/pair-code")
def create_pair_code(access: TeamAccess = Depends(team_access), ctx: Ctx = Depends(get_ctx)) -> dict:
    """管理员生成一次性配对码：给「不是同一账号」的机器用。"""
    access.require_admin()
    with session_scope() as session:
        code, row = pairing.create_code(
            session, team_id=access.id, created_by=ctx.user_id
        )
        expires_at = row.expires_at
    return {
        "code": code,
        "expires_at": expires_at,
        "expires_in_minutes": pairing.PAIR_CODE_TTL_MINUTES,
        "note": "把配对码给对方，在客户端「用配对码接入」里填；用过即失效。",
    }


@router.post("/edge/pair")
async def claim_pair_code(ctx: Ctx = Depends(get_ctx)) -> dict:
    """端侧用配对码换令牌：不需要登录，兑换时按电脑用户名自动建团队成员。"""
    body = await read_body(ctx.request)
    with session_scope() as session:
        result = pairing.claim(
            session,
            code=str(body.get("code") or ""),
            username=str(body.get("username") or ""),
            device_name=str(body.get("device_name") or ""),
            platform=str(body.get("platform") or ""),
        )
        team, user, device = result["team"], result["user"], result["device"]
        payload = {
            "ok": True,
            "token": result["token"],
            "team": team.slug,
            "team_name": team.name,
            "username": user.username,
            "user_created": result["user_created"],
            "device": {"id": device.id, "name": device.name or ""},
            "ws_path": "/api/edge/ws",
        }
    return payload


@router.post("/teams/{team}/devices/self")
async def pair_self_device(access: TeamAccess = Depends(team_access),
                           ctx: Ctx = Depends(get_ctx)) -> dict:
    """同账号接入：已登录的成员直接给自己的机器换令牌，不需要配对码。"""
    access.require_read("issues")
    if access.ctx.user is None:
        raise ApiError("需要登录后再绑定设备", 401, code="login_required")
    body = await read_body(ctx.request)
    with session_scope() as session:
        device, token = device_service.upsert_device(
            session,
            team_id=access.id,
            name=str(body.get("device_name") or ""),
            platform=str(body.get("platform") or ""),
            user_id=ctx.user_id,
        )
        team = session.get(Team, access.id)
        payload = {
            "ok": True,
            "token": token,
            "team": team.slug if team is not None else "",
            "team_name": team.name if team is not None else "",
            "username": ctx.user.username if ctx.user is not None else "",
            "user_created": False,
            "device": {"id": device.id, "name": device.name or ""},
            "ws_path": "/api/edge/ws",
        }
    return payload
