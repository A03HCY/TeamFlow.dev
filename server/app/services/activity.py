"""活动流：写入一条 activity，供概览与活动页展示。"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.base import utcnow_iso
from ..db.models import Activity


def log(session: Session, team_id: int, actor_id, actor_name: str, verb: str,
        subject_type: str, subject_id, subject_text: str = "", detail: str = "") -> Activity:
    entry = Activity(
        team_id=team_id,
        actor_id=actor_id,
        actor_name=actor_name or "",
        verb=verb,
        subject_type=subject_type,
        subject_id=subject_id,
        subject_text=subject_text or "",
        detail=detail or "",
        created_at=utcnow_iso(),
    )
    session.add(entry)
    session.flush()
    return entry


def recent(session: Session, team_id: int, limit: int = 12) -> list[Activity]:
    stmt = (
        select(Activity)
        .where(Activity.team_id == team_id)
        .order_by(Activity.id.desc())
        .limit(limit)
    )
    return list(session.scalars(stmt))


def serialize(entry: Activity) -> dict:
    return {
        "id": entry.id,
        "team_id": entry.team_id,
        "actor_id": entry.actor_id,
        "actor_name": entry.actor_name or "",
        "verb": entry.verb,
        "subject_type": entry.subject_type,
        "subject_id": entry.subject_id,
        "subject_text": entry.subject_text or "",
        "detail": entry.detail or "",
        "created_at": entry.created_at,
    }
