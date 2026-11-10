"""端侧接入服务：设备配对、WS 网关、事件入库、洞察收件箱（P1）。

对外一共三块：

* ``/api/edge/ws``：端侧长连接（握手 hello → 事件 event → 心跳 heartbeat →
  下行 query/pause/resume/insight）；
* ``/api/teams/{team}/engine/edge/*``：管理员侧的设备管理、事件检索、REST 兜底上报；
* ``/api/teams/{team}/engine/{insights,jobs,audit}``：洞察收件箱、作业与审计。

WS 端点不能用 ``get_ctx``（Starlette 的 WS 路由不注入 ``Request``），所以握手时
由 ``hello.token`` 自己解析设备与团队；REST 走常规的 ``team_access``。
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect

from ..config import ENGINE_PAIR_TTL_MINUTES
from ..deps import Ctx, TeamAccess, get_ctx, read_body, team_access
from ..errors import ApiError
from ..services.edge import devices as device_service
from ..services.edge.gateway import (
    CLOSE_ERROR,
    CLOSE_FORBIDDEN,
    FRAME_EVENT,
    FRAME_HEARTBEAT,
    FRAME_HELLO,
    FRAME_HELLO_OK,
    FRAME_QUERY_RESULT,
    Connection,
    ack_frame,
    error_frame,
    hello_ok_frame,
    registry,
)
from ..services.edge.ingest import event_dict, ingest, recent_events
from ..services.inference import apply as apply_service
from ..services.inference import audit, pipeline, proposals
from ..services.inference.events import EventRejected, parse_envelope
from ..services.inference.worker import get_worker, job_dict, pending_count
from ..services.engine import available, load_engine_config
from ..services.issues import find_by_number
from ..services.milestones import get_by_number as get_milestone
from ..db.base import utcnow_iso
from ..db.models import Issue
from ..db.engine_models import (
    CodeEvent,
    InferenceAudit,
    InferenceJob,
    Insight,
    InsightEvent,
)
from ..db.session import session_scope
from sqlalchemy import select

logger = logging.getLogger("teamflow.edge")

router = APIRouter(prefix="/api/teams/{team}", tags=["engine"])
ws_router = APIRouter(tags=["edge"])


# ---------------------------------------------------------------- 公共小工具

def _require_engine() -> None:
    ok, reason = available()
    if not ok:
        raise ApiError(f"推理引擎不可用：{reason}", 503, code="engine_unavailable")


def _device_or_404(session, team_id: int, device_id: int):
    from ..db.engine_models import Device

    device = session.get(Device, int(device_id))
    if device is None or device.team_id != team_id:
        raise ApiError("设备不存在", 404, code="device_not_found")
    return device


def _count_events(session, team_id: int, device_id: int) -> int:
    return len(
        list(
            session.execute(
                select(CodeEvent.id).where(
                    CodeEvent.team_id == team_id, CodeEvent.device_id == device_id
                )
            ).scalars()
        )
    )


# ---------------------------------------------------------------- 设备与状态

@router.get("/engine/edge")
def edge_overview(access: TeamAccess = Depends(team_access)) -> dict:
    """端侧面板：设备清单 + 在线状态 + 最近事件 + 作业 + 洞察计数。"""
    session = access.session
    online = registry.online_ids(access.id)
    paused = registry.paused_ids(access.id)
    devices = [
        device_service.device_dict(
            device,
            online=device.id in online,
            event_count=_count_events(session, access.id, device.id),
        )
        for device in device_service.list_devices(session, access.id)
    ]
    events = [event_dict(item) for item in recent_events(session, access.id, limit=20)]
    jobs = [
        job_dict(item)
        for item in session.execute(
            select(InferenceJob)
            .where(InferenceJob.team_id == access.id)
            .order_by(InferenceJob.id.desc())
            .limit(10)
        ).scalars()
    ]
    draft = len(
        list(
            session.execute(
                select(Insight.id).where(
                    Insight.team_id == access.id, Insight.state == "draft"
                )
            ).scalars()
        )
    )
    return {
        "devices": devices,
        "online": sorted(online),
        "paused": sorted(paused),
        "events": events,
        "jobs": jobs,
        "pending_jobs": pending_count(),
        "insights_draft": draft,
        "worker": get_worker().stats(),
        "pair_ttl_minutes": ENGINE_PAIR_TTL_MINUTES,
        "can_manage": access.can_edit(),
    }


@router.post("/engine/edge/pair")
async def pair_device(
    access: TeamAccess = Depends(team_access),
    ctx: Ctx = Depends(get_ctx),
) -> dict:
    """生成配对码：管理员点了就算批准（方案 §13 C7），不需要二次审批。"""
    _require_engine()
    access.require_admin()
    body = await read_body(ctx.request)
    with session_scope() as session:
        device, token = device_service.pair_device(
            session,
            team_id=access.id,
            name=str(body.get("name") or ""),
            platform=str(body.get("platform") or ""),
            user_id=ctx.user_id,
        )
        audit.append(
            session,
            team_id=access.id,
            action="device.paired",
            subject_type="device",
            subject_id=device.id,
            payload={"name": device.name, "platform": device.platform},
            actor=ctx.actor(),
        )
        payload = device_service.device_dict(device)
    return {
        "device": payload,
        "token": token,
        "expires_in_minutes": ENGINE_PAIR_TTL_MINUTES,
        "ws_path": "/api/edge/ws",
        "note": "配对码只显示这一次；填进端侧配置后保存即可自动接入。",
    }


@router.delete("/engine/edge/devices/{device_id}")
async def revoke_device(
    device_id: int,
    access: TeamAccess = Depends(team_access),
    ctx: Ctx = Depends(get_ctx),
) -> dict:
    access.require_admin()
    with session_scope() as session:
        device = _device_or_404(session, access.id, device_id)
        device_service.revoke(session, device)
        audit.append(
            session,
            team_id=access.id,
            action="device.revoked",
            subject_type="device",
            subject_id=device.id,
            payload={"name": device.name},
            actor=ctx.actor(),
        )
        payload = device_service.device_dict(device)
    await registry.close(int(device_id), code=CLOSE_FORBIDDEN, reason="设备已被吊销")
    return {"ok": True, "device": payload}


@router.post("/engine/edge/devices/{device_id}/pause")
async def pause_device(
    device_id: int,
    access: TeamAccess = Depends(team_access),
    ctx: Ctx = Depends(get_ctx),
) -> dict:
    access.require_admin()
    body = await read_body(ctx.request)
    paused = bool(body.get("paused", True))
    with session_scope() as session:
        device = _device_or_404(session, access.id, device_id)
        payload = device_service.device_dict(device, online=int(device_id) in registry.online_ids())
    delivered = await registry.set_paused(int(device_id), paused, reason=str(body.get("reason") or ""))
    return {"ok": True, "paused": paused, "delivered": delivered, "device": payload}


@router.get("/engine/edge/events")
def list_events(
    limit: int = 50,
    access: TeamAccess = Depends(team_access),
) -> dict:
    session = access.session
    limit = max(1, min(200, int(limit)))
    return {
        "events": [event_dict(item) for item in recent_events(session, access.id, limit=limit)],
    }


@router.post("/engine/edge/events")
async def post_event(
    access: TeamAccess = Depends(team_access),
    ctx: Ctx = Depends(get_ctx),
) -> dict:
    """REST 兜底上报（WS 连不上时端侧可以走这里），语义与 WS 完全一致。"""
    _require_engine()
    access.require_edit()
    body = await read_body(ctx.request)
    envelope = parse_envelope(body.get("envelope") or body)
    with session_scope() as session:
        device = device_service.find_by_token(session, body.get("token"))
        device_service.require_usable(device)
        result = ingest(session, device=device, envelope=envelope, source="rest")
        if result.stored:
            device_service.touch(session, device)
    return {
        "ok": result.stored,
        "status": result.status,
        "message": result.message,
        "event_id": result.event_id,
        "job_id": result.job_id,
        "issue_number": result.issue_number,
    }


# ---------------------------------------------------------------- 洞察收件箱

def _issue_numbers(session, insights) -> dict[int, int]:
    """洞察 → 议题编号，前端要拿它拼 /{slug}/issues/{number} 链接。"""
    ids = {item.issue_id for item in insights if item.issue_id}
    if not ids:
        return {}

    return {
        row.id: row.number
        for row in session.execute(
            select(Issue.id, Issue.number).where(Issue.id.in_(ids))
        ).all()
    }


@router.get("/engine/insights")
def list_insights(
    state: str = "draft",
    kind: str = "",
    limit: int = 50,
    access: TeamAccess = Depends(team_access),
) -> dict:
    session = access.session
    limit = max(1, min(200, int(limit)))
    query = select(Insight).where(Insight.team_id == access.id)
    if state and state != "all":
        query = query.where(Insight.state == state)
    if kind:
        query = query.where(Insight.kind == kind)
    rows = list(session.execute(query.order_by(Insight.id.desc()).limit(limit)).scalars())
    # 收件箱卡片要显示「创建 → 合并 → 驳回」这类时间线，这里一次查完避免 N+1
    timeline: dict[int, list] = {}
    if rows:
        from ..db.engine_models import InsightEvent

        for item in session.execute(
            select(InsightEvent)
            .where(InsightEvent.insight_id.in_([row.id for row in rows]))
            .order_by(InsightEvent.insight_id, InsightEvent.seq)
        ).scalars():
            timeline.setdefault(item.insight_id, []).append(item)
    counts = {
        "draft": len(
            list(
                session.execute(
                    select(Insight.id).where(
                        Insight.team_id == access.id, Insight.state == "draft"
                    )
                ).scalars()
            )
        )
    }
    numbers = _issue_numbers(session, rows)
    return {
        "insights": [
            pipeline.insight_dict(
                item, events=timeline.get(item.id), issue_number=numbers.get(item.issue_id)
            )
            for item in rows
        ],
        "counts": counts,
        "can_edit": access.can_edit(),
    }


@router.get("/engine/insights/{insight_id}")
def get_insight(insight_id: int, access: TeamAccess = Depends(team_access)) -> dict:
    session = access.session
    insight = session.get(Insight, int(insight_id))
    if insight is None or insight.team_id != access.id:
        raise ApiError("洞察不存在", 404, code="insight_not_found")
    from ..db.engine_models import InsightEvent

    events = list(
        session.execute(
            select(InsightEvent)
            .where(InsightEvent.insight_id == insight.id)
            .order_by(InsightEvent.seq)
        ).scalars()
    )
    numbers = _issue_numbers(session, [insight])
    return {
        "insight": pipeline.insight_dict(
            insight, events=events, issue_number=numbers.get(insight.issue_id)
        )
    }


@router.post("/engine/insights/{insight_id}/reject")
async def reject_insight(
    insight_id: int,
    access: TeamAccess = Depends(team_access),
    ctx: Ctx = Depends(get_ctx),
) -> dict:
    """驳回一条洞察（确认与写回在 P2 接上，驳回现在就能用）。"""
    access.require_edit()
    body = await read_body(ctx.request)
    reason = str(body.get("reason") or "")[:500]
    with session_scope() as session:
        insight = session.get(Insight, int(insight_id))
        if insight is None or insight.team_id != access.id:
            raise ApiError("洞察不存在", 404, code="insight_not_found")
        if insight.state != "draft":
            raise ApiError(f"当前状态是 {insight.state}，只有草稿可以驳回", 409, code="bad_state")
        insight.state = "rejected"
        insight.decided_at = utcnow_iso()
        actor_id, actor_name = ctx.actor()
        insight.decided_by = actor_name[:80]
        insight.decided_by_user_id = actor_id
        pipeline.append_insight_event(session, insight, "rejected", {"reason": reason})
        audit.append(
            session,
            team_id=access.id,
            action="insight.rejected",
            subject_type="insight",
            subject_id=insight.id,
            payload={"reason": reason},
            actor=ctx.actor(),
        )
        payload = pipeline.insight_dict(insight)
    return {"ok": True, "insight": payload}


# ---------------------------------------------------------------- 规划与提案

@router.post("/engine/proposals")
def create_proposal(
    access: TeamAccess = Depends(team_access),
    body: dict = Depends(read_body),
) -> dict:
    """按需生成一条提案（拆分 / 任务树 / 验收标准 / 负责人）。"""
    access.require_edit()
    _require_engine()
    session, team = access.session, access.team
    kind = str(body.get("kind") or "split").strip().lower()
    issue = None
    milestone = None
    if body.get("issue") not in (None, ""):
        number = issue_service_number(body.get("issue"))
        issue = find_by_number(session, team.id, number)
        if issue is None:
            raise ApiError("议题不存在", 404)
    if body.get("milestone") not in (None, ""):
        number = issue_service_number(body.get("milestone"))
        milestone = get_milestone(session, team.id, number)
        if milestone is None:
            raise ApiError("里程碑不存在", 404)
    insight = proposals.build_proposal(session, team, kind, issue=issue, milestone=milestone)
    if insight is None:
        return {"ok": True, "created": False, "insight": None,
                "message": "当前没有可拆分的依据（缺少代码事件或清单项）"}
    insight_id = insight.id
    payload = pipeline.insight_dict(
        insight,
        events=proposals_events(session, insight_id),
        issue_number=issue.number if issue is not None else None,
    )
    return {"ok": True, "created": True, "insight": payload, "preview": apply_service.preview(insight)}


@router.post("/engine/insights/{insight_id}/apply")
async def apply_insight(
    insight_id: int,
    access: TeamAccess = Depends(team_access),
    ctx: Ctx = Depends(get_ctx),
) -> dict:
    """确认并执行提案：默认按团队配置的 ``dry_run`` 只回预览。"""
    access.require_edit()
    _require_engine()
    session = access.session
    insight = session.get(Insight, int(insight_id))
    if insight is None or insight.team_id != access.id:
        raise ApiError("洞察不存在", 404, code="insight_not_found")
    if insight.kind == "progress":
        raise ApiError("进度洞察请在引擎页确认，不通过该接口执行", 400)
    if insight.state != "draft":
        raise ApiError(f"当前状态是 {insight.state}，只有草稿可以执行", 409, code="bad_state")
    try:
        body = await read_body(ctx.request)
    except ApiError:
        body = {}
    cfg = load_engine_config(session, access.id)
    dry_run = bool(cfg.dry_run)
    if isinstance(body, dict) and "dry_run" in body:
        dry_run = bool(body.get("dry_run"))
    actor_id, actor_name = ctx.actor()
    result = apply_service.execute(
        session, access.team, insight,
        actor_id=actor_id, actor_name=actor_name, dry_run=dry_run,
    )
    payload = pipeline.insight_dict(
        insight,
        events=proposals_events(session, insight.id),
        issue_number=_issue_number_of(session, insight),
    )
    return {**result, "ok": True, "insight": payload}


@router.post("/engine/insights/{insight_id}/revert")
def revert_insight(
    insight_id: int,
    access: TeamAccess = Depends(team_access),
    ctx: Ctx = Depends(get_ctx),
) -> dict:
    access.require_edit()
    _require_engine()
    session = access.session
    insight = session.get(Insight, int(insight_id))
    if insight is None or insight.team_id != access.id:
        raise ApiError("洞察不存在", 404, code="insight_not_found")
    actor_id, actor_name = ctx.actor()
    result = apply_service.revert(
        session, access.team, insight, actor_id=actor_id, actor_name=actor_name
    )
    payload = pipeline.insight_dict(
        insight,
        events=proposals_events(session, insight.id),
        issue_number=_issue_number_of(session, insight),
    )
    return {**result, "insight": payload}


def issue_service_number(value) -> int:
    text = str(value).strip().lstrip("#")
    if not text.isdigit():
        raise ApiError("编号必须是数字")
    return int(text)


def _issue_number_of(session, insight) -> int | None:
    """洞察关联议题的团队内编号（议题可能已被删除）。"""
    if not insight.issue_id:
        return None
    return session.scalar(select(Issue.number).where(Issue.id == insight.issue_id))


def proposals_events(session, insight_id: int) -> list[InsightEvent]:
    """`pipeline.insight_dict` 需要 ORM 行（自己会把 detail 反序列化）。"""
    return list(
        session.scalars(
            select(InsightEvent)
            .where(InsightEvent.insight_id == insight_id)
            .order_by(InsightEvent.seq)
        )
    )


# ---------------------------------------------------------------- 作业与审计

@router.get("/engine/jobs")
def list_jobs(
    status: str = "",
    limit: int = 20,
    access: TeamAccess = Depends(team_access),
) -> dict:
    session = access.session
    query = select(InferenceJob).where(InferenceJob.team_id == access.id)
    if status:
        query = query.where(InferenceJob.status == status)
    rows = list(
        session.execute(
            query.order_by(InferenceJob.id.desc()).limit(max(1, min(100, int(limit))))
        ).scalars()
    )
    return {"jobs": [job_dict(item) for item in rows], "pending": pending_count()}


@router.post("/engine/jobs/drain")
def drain_jobs(
    limit: int = 10,
    access: TeamAccess = Depends(team_access),
) -> dict:
    """手动跑一批作业（运维排障；worker 开着时一般用不到）。"""
    _require_engine()
    access.require_admin()
    results = get_worker().drain_once(limit=max(1, min(50, int(limit))))
    return {"ok": True, "processed": len(results), "results": results}


@router.get("/engine/audit")
def list_audit(
    limit: int = 50,
    access: TeamAccess = Depends(team_access),
) -> dict:
    session = access.session
    rows = list(
        session.execute(
            select(InferenceAudit)
            .where(InferenceAudit.team_id == access.id)
            .order_by(InferenceAudit.id.desc())
            .limit(max(1, min(200, int(limit))))
        ).scalars()
    )
    return {"entries": audit.decode(rows)}


@router.get("/engine/audit/verify")
def verify_audit(access: TeamAccess = Depends(team_access)) -> dict:
    return audit.verify(access.session, team_id=access.id)


# ---------------------------------------------------------------- WS 网关

@ws_router.websocket("/api/edge/ws")
async def edge_socket(websocket: WebSocket) -> None:  # noqa: C901 - 状态机本来就长
    await websocket.accept()
    try:
        hello = await asyncio.wait_for(websocket.receive_json(), timeout=15.0)
    except (asyncio.TimeoutError, WebSocketDisconnect, ValueError):
        with contextlib.suppress(Exception):
            await websocket.close(code=CLOSE_FORBIDDEN, reason="需要先发送 hello 帧")
        return
    if str(hello.get("type") or "") != FRAME_HELLO:
        with contextlib.suppress(Exception):
            await websocket.send_json(error_frame("第一帧必须是 hello", code="hello_required"))
            await websocket.close(code=CLOSE_FORBIDDEN, reason="hello required")
        return

    ok, reason = available()
    if not ok:
        with contextlib.suppress(Exception):
            await websocket.send_json(error_frame(f"推理引擎不可用：{reason}", code="engine_unavailable"))
            await websocket.close(CLOSE_ERROR, reason="engine unavailable")
        return

    token = str(hello.get("token") or "")
    name = str(hello.get("name") or (hello.get("device") or {}).get("name") or "")
    platform = str(hello.get("platform") or (hello.get("device") or {}).get("platform") or "")
    agent_version = str(hello.get("agent_version") or "")

    try:
        with session_scope() as session:
            device = device_service.find_by_token(session, token)
            device_service.require_usable(device)
            first_seen = not device.last_seen_at
            device_service.claim(
                session,
                device,
                name=name,
                platform=platform,
                agent_version=agent_version,
                meta=hello.get("device") if isinstance(hello.get("device"), dict) else None,
            )
            if first_seen:
                audit.append(
                    session,
                    team_id=device.team_id,
                    action="device.paired",
                    subject_type="device",
                    subject_id=device.id,
                    payload={"name": device.name, "platform": device.platform},
                    actor=(device.user_id, device.name or "端侧设备"),
                )
            device_id = device.id
            team_id = device.team_id
            device_name = device.name or ""
    except ApiError as exc:
        with contextlib.suppress(Exception):
            await websocket.send_json(error_frame(exc.message, code=exc.code or "device_error"))
            await websocket.close(code=CLOSE_FORBIDDEN, reason=exc.message[:100])
        return

    connection = Connection(
        device_id=device_id,
        team_id=team_id,
        websocket=websocket,
        device_name=device_name,
        platform=platform,
        agent_version=agent_version,
    )
    await registry.register(connection)
    await registry.send(
        connection,
        hello_ok_frame(
            device_id=device_id,
            team_id=team_id,
            device_name=device_name,
            server_time=utcnow_iso(),
            paused=False,
        ),
    )
    logger.info("端侧设备已接入：team=%s device=%s", team_id, device_id)

    try:
        while True:
            frame = await websocket.receive_json()
            await _handle_frame(connection, frame)
    except WebSocketDisconnect:
        logger.info("端侧设备断开：team=%s device=%s", team_id, device_id)
    except Exception as exc:  # noqa: BLE001 - 进程不能因为一条坏帧挂掉
        logger.warning("端侧连接异常（device=%s）：%s", device_id, exc)
        with contextlib.suppress(Exception):
            await websocket.close(code=CLOSE_ERROR, reason="internal error")
    finally:
        registry.unregister(connection)


async def _handle_frame(connection: Connection, frame: dict) -> None:
    kind = str(frame.get("type") or "")
    if kind == FRAME_HEARTBEAT:
        frame["received_at"] = utcnow_iso()
        with session_scope() as session:
            from ..db.engine_models import Device

            device = session.get(Device, connection.device_id)
            if device is not None:
                device_service.touch(session, device)
        return
    if kind == FRAME_QUERY_RESULT:
        request_id = str(frame.get("request_id") or "")
        registry.resolve_query(connection, request_id, frame)
        return
    if kind != FRAME_EVENT:
        await registry.send(
            connection, error_frame(f"不认识的帧类型：{kind or '（空）'}", code="unknown_frame")
        )
        return

    if not registry.allow_event(connection):
        await registry.send(
            connection,
            ack_frame(
                str((frame.get("envelope") or {}).get("event_uid") or ""),
                "rejected",
                "上报过于频繁，请稍后重试",
            ),
        )
        return

    try:
        envelope = parse_envelope(frame.get("envelope"))
        with session_scope() as session:
            from ..db.engine_models import Device

            device = session.get(Device, connection.device_id)
            device_service.require_usable(device)
            result = ingest(session, device=device, envelope=envelope, source="ws")
            if result.stored:
                device_service.touch(session, device)
    except EventRejected as exc:
        await registry.send(connection, ack_frame("", "rejected", exc.message))
        return
    except ApiError as exc:
        await registry.send(connection, ack_frame("", "rejected", exc.message))
        return
    except Exception as exc:  # noqa: BLE001
        logger.warning("事件入库失败（device=%s）：%s", connection.device_id, exc)
        await registry.send(connection, ack_frame("", "rejected", "服务端处理失败，请重试"))
        return

    await registry.send(
        connection,
        ack_frame(envelope.event_uid, result.status, result.message),
    )


__all__ = ["router", "ws_router"]
