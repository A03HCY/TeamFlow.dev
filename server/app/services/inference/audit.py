"""推断审计日志（哈希链）。

每次「推断产生 / 人工确认 / 人工驳回 / 写回 / 撤销 / 引擎配置变更」都追加一行，
每行带 ``prev_hash`` 与 ``hash``：

    hash = sha256(prev_hash | team_id | actor | action | subject | created_at | payload_json)

链按团队隔离（``prev_hash`` 取该团队上一行的 hash，首个为 64 个 0）。
好处是篡改任意一行的 payload，后面所有行的 hash 都对不上，
``/api/engine/audit/verify`` 能定位到第一处断点；再配合每日备份，
足以应付「谁在什么时候把哪个议题改成了完成」这类追责问题。

注意：这里只做**防篡改留痕**，不做签名（P3 若需要法律级证据链再引入非对称签名）。
"""
from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...db.base import utcnow_iso
from ...db.engine_models import InferenceAudit

GENESIS_HASH = "0" * 64

# 同进程内串行化追加，避免并发写同一团队时拿到相同的 prev_hash
_CHAIN_LOCK = threading.Lock()

ACTIONS = frozenset(
    {
        "insight.created",
        "insight.previewed",
        "insight.confirmed",
        "insight.applied",
        "insight.rejected",
        "insight.reverted",
        "insight.expired",
        "insight.failed",
        "engine.configured",
        "engine.enabled",
        "engine.disabled",
        "device.paired",
        "device.revoked",
        "repo.bound",
    }
)


@dataclass(frozen=True)
class AuditEntry:
    id: int
    team_id: int
    action: str
    subject_type: str
    subject_id: str
    actor_id: int | None
    actor_name: str
    payload: dict
    prev_hash: str
    hash: str
    created_at: str

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "team_id": self.team_id,
            "action": self.action,
            "subject_type": self.subject_type,
            "subject_id": self.subject_id,
            "actor_id": self.actor_id,
            "actor_name": self.actor_name,
            "payload": self.payload,
            "prev_hash": self.prev_hash,
            "hash": self.hash,
            "created_at": self.created_at,
        }


def canonical(payload: Any) -> str:
    """稳定序列化：同样的字典永远得到同样的字符串（用于复算 hash）。"""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def compute_hash(
    *,
    prev_hash: str,
    team_id: int,
    actor_id: int | None,
    actor_name: str,
    action: str,
    subject_type: str,
    subject_id: str,
    created_at: str,
    payload_json: str,
) -> str:
    material = "|".join(
        [
            prev_hash,
            str(team_id),
            "" if actor_id is None else str(actor_id),
            actor_name or "",
            action,
            subject_type or "",
            subject_id or "",
            created_at,
            payload_json,
        ]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _tail_hash(session: Session, team_id: int) -> str:
    row = session.execute(
        select(InferenceAudit.hash)
        .where(InferenceAudit.team_id == team_id)
        .order_by(InferenceAudit.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    return row or GENESIS_HASH


def append(
    session: Session,
    *,
    team_id: int,
    action: str,
    subject_type: str = "",
    subject_id: str | int | None = "",
    payload: dict | None = None,
    actor: tuple[int | None, str] | Any = (None, "系统"),
) -> InferenceAudit:
    """追加一条审计；调用方负责 commit。"""
    if not action:
        raise ValueError("action 不能为空")
    actor_id, actor_name = _split_actor(actor)
    created_at = utcnow_iso()
    body = payload or {}
    payload_json = canonical(body)
    with _CHAIN_LOCK:
        prev_hash = _tail_hash(session, team_id)
        digest = compute_hash(
            prev_hash=prev_hash,
            team_id=team_id,
            actor_id=actor_id,
            actor_name=actor_name,
            action=action,
            subject_type=subject_type,
            subject_id="" if subject_id is None else str(subject_id),
            created_at=created_at,
            payload_json=payload_json,
        )
        row = InferenceAudit(
            team_id=team_id,
            actor_id=actor_id,
            actor_name=actor_name[:80],
            action=action[:60],
            subject_type=(subject_type or "")[:40],
            subject_id=("" if subject_id is None else str(subject_id))[:80],
            payload=payload_json,
            prev_hash=prev_hash,
            hash=digest,
            created_at=created_at,
        )
        session.add(row)
        session.flush()
        return row


def verify(
    session: Session,
    *,
    team_id: int | None = None,
    limit: int | None = None,
) -> dict:
    """复算整条（或指定团队的）链，返回第一处断点。"""
    query = select(InferenceAudit).order_by(InferenceAudit.team_id, InferenceAudit.id)
    if team_id is not None:
        query = query.where(InferenceAudit.team_id == team_id)
    if limit:
        query = query.limit(int(limit))
    rows = list(session.execute(query).scalars())
    tails: dict[int, str] = {}
    checked = 0
    for row in rows:
        checked += 1
        prev_expected = tails.get(row.team_id, GENESIS_HASH)
        if row.prev_hash != prev_expected:
            return _broken(row, "prev_hash 与上一条不一致", prev_expected, row.prev_hash, checked)
        payload_json = canonical(_loads(row.payload))
        digest = compute_hash(
            prev_hash=row.prev_hash,
            team_id=row.team_id,
            actor_id=row.actor_id,
            actor_name=row.actor_name or "",
            action=row.action,
            subject_type=row.subject_type or "",
            subject_id=row.subject_id or "",
            created_at=row.created_at or "",
            payload_json=payload_json,
        )
        if digest != row.hash:
            return _broken(row, "hash 复算不一致（内容被改动）", digest, row.hash, checked)
        tails[row.team_id] = row.hash
    return {
        "ok": True,
        "checked": checked,
        "teams": len(tails),
        "tail": {str(key): value for key, value in tails.items()},
    }


def _broken(row: InferenceAudit, reason: str, expected: str, actual: str, checked: int) -> dict:
    return {
        "ok": False,
        "checked": checked,
        "reason": reason,
        "broken_at": {
            "id": row.id,
            "team_id": row.team_id,
            "action": row.action,
            "created_at": row.created_at,
        },
        "expected": expected,
        "actual": actual,
    }


def _loads(raw: str | None) -> Any:
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {"_raw": raw}


def _split_actor(actor: Any) -> tuple[int | None, str]:
    if actor is None:
        return None, "系统"
    if isinstance(actor, tuple):
        actor_id = actor[0] if actor else None
        name = str(actor[1]) if len(actor) > 1 and actor[1] else "系统"
        return (int(actor_id) if actor_id is not None else None), name
    actor_id = getattr(actor, "id", None)
    name = getattr(actor, "display_name", None) or getattr(actor, "username", None) or "系统"
    return (int(actor_id) if actor_id is not None else None), str(name)


def decode(rows: Iterable[InferenceAudit]) -> list[dict]:
    """给 API 用：把 ORM 行转成带 payload 的字典。"""
    return [
        AuditEntry(
            id=row.id,
            team_id=row.team_id,
            action=row.action,
            subject_type=row.subject_type or "",
            subject_id=row.subject_id or "",
            actor_id=row.actor_id,
            actor_name=row.actor_name or "",
            payload=_loads(row.payload),
            prev_hash=row.prev_hash or "",
            hash=row.hash or "",
            created_at=row.created_at or "",
        ).as_dict()
        for row in rows
    ]


__all__ = [
    "ACTIONS",
    "AuditEntry",
    "GENESIS_HASH",
    "append",
    "canonical",
    "compute_hash",
    "decode",
    "verify",
]
