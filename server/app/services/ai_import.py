"""AI 截图导入日程：预览（解析）与确认（落库）。"""
from __future__ import annotations

from sqlalchemy import delete, func, select

from ..db.base import utcnow_iso
from ..db.models import ScheduleEntry
from ..errors import ApiError
from . import ai
from .activity import log
from .schedules import normalize_schedule, resolve_owner

PLACEHOLDER = "请先在设置里配置 AI 服务（base_url / api_key / model）"


def preview(access, images, answers: str = "", previous=None) -> dict:
    if not ai.is_configured(ai.get_config(access.ctx.session)):
        raise ApiError(PLACEHOLDER, 400, code="ai_not_configured")
    try:
        result = ai.parse(access.ctx.session, images, answers=answers, previous=previous)
    except ValueError as exc:
        raise ApiError(str(exc), 502, code="ai_failed") from exc
    entries = result["entries"]
    for entry in entries:
        entry["missing_time"] = not (entry["start_time"] and entry["end_time"])
        entry["complete"] = bool(entry["title"]) and (
            (entry["kind"] == "recurring" and entry["weekday"] is not None)
            or (entry["kind"] == "once" and bool(entry["date"]))
        )
    return {"entries": entries, "questions": result["questions"], "model": result["model"]}


def confirm(access, entries, user_id=None, replace: bool = False) -> dict:
    session = access.ctx.session
    team = access.team
    if not isinstance(entries, list) or not entries:
        raise ApiError("没有可导入的日程")
    if len(entries) > ai.MAX_ENTRIES:
        raise ApiError(f"一次最多导入 {ai.MAX_ENTRIES} 条日程")

    owner = resolve_owner(access, user_id)
    stamp = utcnow_iso()
    replaced = 0
    if replace:
        replaced = session.scalar(
            select(func.count()).select_from(ScheduleEntry).where(
                ScheduleEntry.team_id == team.id, ScheduleEntry.user_id == owner
            )
        ) or 0
        session.execute(
            delete(ScheduleEntry).where(
                ScheduleEntry.team_id == team.id, ScheduleEntry.user_id == owner
            )
        )

    created = 0
    for index, raw in enumerate(entries, 1):
        try:
            data = normalize_schedule({}, raw if isinstance(raw, dict) else {})
        except ApiError as exc:
            raise ApiError(f"第 {index} 条：{exc.message}") from exc
        session.add(
            ScheduleEntry(
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
                color=None,
                created_at=stamp,
                updated_at=stamp,
            )
        )
        created += 1

    actor_id, actor_name = access.ctx.actor()
    detail = f"AI 导入 {created} 条日程"
    if replaced:
        detail += f"（替换原有 {replaced} 条）"
    log(session, team.id, actor_id, actor_name, "created", "schedule", None, detail)
    return {"count": created, "replaced": replaced}
