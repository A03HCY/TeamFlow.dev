"""日程端点：翻页式 CRUD、日历订阅（.ics）、AI 截图导入。"""
from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select

from ..db.base import utcnow_iso
from ..db.models import ScheduleEntry
from ..db.session import get_db
from ..deps import TeamAccess, read_body, team_access
from ..errors import ApiError
from ..services import activity
from ..services import ai_import
from ..services import ics as ics_service
from ..services import schedules as schedule_service
from ..services import settings as settings_service
from ..services.serializers import schedule_dict

router = APIRouter(prefix="/api/teams/{team}", tags=["schedules"])


def _entry_payload(access: TeamAccess, entry: ScheduleEntry) -> dict:
    access.session.refresh(entry)
    return schedule_dict(entry)


@router.get("/schedules")
def list_schedules(access: TeamAccess = Depends(team_access)) -> dict:
    if not access.can_edit() and not access.is_public("schedules"):
        raise ApiError("日程未对外公开", 403)
    entries = schedule_service.list_schedules(access.session, access.id)
    payload = {
        "schedules": [schedule_dict(entry) for entry in entries],
        "is_admin": access.is_admin(),
        "can_edit": access.can_edit(),
    }
    if access.is_admin():
        token = settings_service.get_setting(
            access.session, schedule_service.ics_token_setting(access.id)
        )
        payload["ics_token"] = token or None
    return payload


@router.post("/schedules")
def create_schedule(body: dict = Depends(read_body),
                    access: TeamAccess = Depends(team_access)) -> dict:
    access.require_edit()
    session, team = access.session, access.team
    data = schedule_service.normalize_schedule({}, body)
    color = schedule_service.normalize_color(body.get("color"))
    owner = schedule_service.resolve_owner(access, body.get("user_id"))
    participants = schedule_service.validate_participants(
        session, team.id, body.get("participant_ids")
    )
    now = utcnow_iso()
    entry = ScheduleEntry(
        team_id=team.id,
        user_id=owner,
        title=data["title"],
        note=data["note"],
        kind=data["kind"],
        weekday=data["weekday"],
        date=data["date"],
        start_time=data["start_time"],
        end_time=data["end_time"],
        start_date=data["start_date"],
        end_date=data["end_date"],
        color=color,
        created_at=now,
        updated_at=now,
    )
    session.add(entry)
    session.flush()
    schedule_service.replace_participants(session, entry, participants)
    actor_id, actor_name = access.ctx.actor()
    activity.log(session, team.id, actor_id, actor_name, "created", "schedule", entry.id,
                 entry.title)
    return {"schedule": _entry_payload(access, entry)}


@router.patch("/schedules/{schedule_id}")
def update_schedule(schedule_id: int, body: dict = Depends(read_body),
                    access: TeamAccess = Depends(team_access)) -> dict:
    access.require_edit()
    session = access.session
    entry = schedule_service.get_or_404(session, schedule_id)
    if entry.team_id != access.id:
        raise ApiError("日程不存在", 404)
    schedule_service.ensure_schedule_owner(access, entry)
    data = schedule_service.normalize_schedule(schedule_service.entry_fields(entry), body)
    color_source = body.get("color") if "color" in body else entry.color
    entry.title = data["title"]
    entry.note = data["note"]
    entry.kind = data["kind"]
    entry.weekday = data["weekday"]
    entry.date = data["date"]
    entry.start_time = data["start_time"]
    entry.end_time = data["end_time"]
    entry.start_date = data["start_date"]
    entry.end_date = data["end_date"]
    entry.color = schedule_service.normalize_color(color_source)
    entry.updated_at = utcnow_iso()
    if "participant_ids" in body:
        participants = schedule_service.validate_participants(
            session, access.id, body.get("participant_ids")
        )
        schedule_service.replace_participants(session, entry, participants)
    session.flush()
    actor_id, actor_name = access.ctx.actor()
    activity.log(session, access.id, actor_id, actor_name, "updated", "schedule", entry.id,
                 entry.title)
    return {"schedule": _entry_payload(access, entry)}


@router.delete("/schedules/{schedule_id}")
def delete_schedule(schedule_id: int, access: TeamAccess = Depends(team_access)) -> dict:
    access.require_edit()
    session = access.session
    entry = schedule_service.get_or_404(session, schedule_id)
    if entry.team_id != access.id:
        raise ApiError("日程不存在", 404)
    schedule_service.ensure_schedule_owner(access, entry)
    title = entry.title
    entry_id = entry.id
    session.delete(entry)
    session.flush()
    actor_id, actor_name = access.ctx.actor()
    activity.log(session, access.id, actor_id, actor_name, "deleted", "schedule", entry_id, title)
    return {"ok": True}


# ---------------------------------------------------------------- 日历订阅

@router.get("/schedules.ics")
def schedules_ics(team: str, token: str = "", session=Depends(get_db)) -> Response:
    """凭 token 访问（订阅工具无法带 Cookie），不走团队访问控制。"""
    from ..services import teams as team_service

    found = team_service.resolve(session, team)
    if found is None:
        raise ApiError("团队不存在或已被删除", 404)
    stored = settings_service.get_setting(session, schedule_service.ics_token_setting(found.id))
    if not stored or not token or not secrets.compare_digest(token, stored):
        raise ApiError("订阅链接无效或已被重置", 403)
    entries = schedule_service.list_schedules(session, found.id)
    body = ics_service.build_ics(found, entries)
    return Response(content=body.encode("utf-8"),
                    media_type="text/calendar; charset=utf-8")


@router.post("/schedules/ics-reset")
def reset_ics_token(access: TeamAccess = Depends(team_access)) -> dict:
    access.require_admin()
    token = schedule_service.new_ics_token()
    settings_service.set_setting(
        access.session, schedule_service.ics_token_setting(access.id), token
    )
    actor_id, actor_name = access.ctx.actor()
    activity.log(access.session, access.id, actor_id, actor_name, "updated", "setting", None,
                 "日历订阅链接", "已生成 / 重置")
    return {"token": token}


# ---------------------------------------------------------------- AI 导入

@router.post("/schedules/import-preview")
async def import_preview(request: Request, access: TeamAccess = Depends(team_access)) -> dict:
    access.require_edit()
    body = await _read_body(request)
    return ai_import.preview(
        access,
        body.get("images") or [],
        answers=str(body.get("answers") or ""),
        previous=body.get("previous_draft"),
    )


@router.post("/schedules/import-confirm")
async def import_confirm(request: Request, access: TeamAccess = Depends(team_access)) -> dict:
    access.require_edit()
    body = await _read_body(request)
    return ai_import.confirm(
        access,
        body.get("entries"),
        user_id=body.get("user_id"),
        replace=bool(body.get("replace")),
    )


async def _read_body(request: Request) -> dict:
    from ..deps import read_body as parse_body

    return await parse_body(request)
