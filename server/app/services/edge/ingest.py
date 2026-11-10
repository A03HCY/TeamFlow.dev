"""事件入库：幂等落 code_events、绑定仓库、排队推理作业（P1）。

端侧可能因为网络抖动重发同一条事件，所以入库必须以
``(device_id, event_uid)`` 为幂等键——唯一索引兜底，查到已存在就回
``duplicate``，**不** 重复建作业。

作业侧做「合并窗口」：窗口（``ENGINE_COALESCE_SECONDS``，默认 5s）内同一
``(设备, 仓库, 分支)`` 的多次保存只启一个作业，事件 id 累积到 payload 里。
理由：一次 ``git checkout`` 会连着触发几十个文件保存事件，逐条推理既慢又贵，
而进度判断只需要「这批改动合起来看」。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...config import ENGINE_COALESCE_SECONDS
from ...db.base import utcnow_iso
from ...db.engine_models import CodeEvent, Device, InferenceJob, RepoBinding
from ...db.models import Issue
from ..inference import audit
from ..inference.events import CodeEventEnvelope, audit_secrets, summary
from ..inference.rules import collect_signals

MAX_EVENTS_PER_JOB = 40


@dataclass
class IngestResult:
    status: str  # stored | duplicate | rejected
    message: str = ""
    event_id: int | None = None
    job_id: int | None = None
    issue_id: int | None = None
    issue_number: int | None = None

    @property
    def stored(self) -> bool:
        return self.status == "stored"


def _loads(raw: str | None, fallback):
    if not raw:
        return fallback
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return fallback


def _dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def find_event(session: Session, device_id: int, event_uid: str) -> CodeEvent | None:
    return session.execute(
        select(CodeEvent).where(
            CodeEvent.device_id == device_id, CodeEvent.event_uid == event_uid
        )
    ).scalar_one_or_none()


def resolve_issue(session: Session, team_id: int, envelope: CodeEventEnvelope) -> Issue | None:
    """用分支名里的编号找议题；找不到就当「无归属变更」。"""
    number = collect_signals(envelope).issue_number
    if not number:
        return None
    return session.execute(
        select(Issue).where(Issue.team_id == team_id, Issue.number == int(number))
    ).scalar_one_or_none()


def ensure_repo_binding(
    session: Session, device: Device, envelope: CodeEventEnvelope
) -> RepoBinding:
    """按 ``(device_id, path_hash)`` 绑定仓库；分支/远端变化时更新。"""
    repo = envelope.repo
    binding = session.execute(
        select(RepoBinding).where(
            RepoBinding.device_id == device.id, RepoBinding.path_hash == repo.path_hash
        )
    ).scalar_one_or_none()
    created = False
    if binding is None:
        binding = RepoBinding(
            team_id=device.team_id,
            device_id=device.id,
            path_hash=repo.path_hash,
            label=(repo.label or "")[:200],
            branch=(repo.branch or "")[:160],
            remote_hash=(repo.remote_hash or "")[:64],
            created_at=utcnow_iso(),
        )
        session.add(binding)
        session.flush()
        created = True
    else:
        changed = False
        if repo.label and repo.label[:200] != binding.label:
            binding.label = repo.label[:200]
            changed = True
        if (repo.branch or "")[:160] != binding.branch:
            binding.branch = (repo.branch or "")[:160]
            changed = True
        if (repo.remote_hash or "")[:64] != binding.remote_hash:
            binding.remote_hash = (repo.remote_hash or "")[:64]
            changed = True
        if changed:
            session.flush()
    if created:
        audit.append(
            session,
            team_id=device.team_id,
            action="repo.bound",
            subject_type="repo",
            subject_id=binding.id,
            payload={
                "device_id": device.id,
                "label": binding.label,
                "branch": binding.branch,
                "path_hash": binding.path_hash,
            },
            actor=(device.user_id, device.name or "端侧设备"),
        )
    return binding


def _queue_job(
    session: Session, device: Device, envelope: CodeEventEnvelope, event: CodeEvent
) -> InferenceJob:
    """同一合并窗口内复用未开始的作业，否则新建。"""
    coalesce_key = f"{device.id}:{envelope.repo.path_hash}:{envelope.repo.branch or ''}"
    cutoff = (
        datetime.now(timezone.utc) - timedelta(seconds=ENGINE_COALESCE_SECONDS)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    existing = session.execute(
        select(InferenceJob)
        .where(
            InferenceJob.coalesce_key == coalesce_key,
            InferenceJob.status == "queued",
            InferenceJob.created_at >= cutoff,
        )
        .order_by(InferenceJob.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if existing is not None:
        payload = _loads(existing.payload, {})
        events = list(payload.get("events") or [])
        if event.id not in events and len(events) < MAX_EVENTS_PER_JOB:
            events.append(event.id)
        payload["events"] = events
        payload["latest"] = event.id
        existing.payload = _dumps(payload)
        session.flush()
        return existing

    payload = {
        "events": [event.id],
        "latest": event.id,
        "device_id": device.id,
        "branch": envelope.repo.branch or "",
        "path_hash": envelope.repo.path_hash,
        "issue_number": collect_signals(envelope).issue_number or 0,
    }
    job = InferenceJob(
        team_id=device.team_id,
        status="queued",
        coalesce_key=coalesce_key[:160],
        payload=_dumps(payload),
        attempts=0,
        error="",
        run_id="",
        budget="{}",
        created_at=utcnow_iso(),
        started_at=None,
        finished_at=None,
    )
    session.add(job)
    session.flush()
    return job


def ingest(
    session: Session,
    *,
    device: Device,
    envelope: CodeEventEnvelope,
    source: str = "ws",
) -> IngestResult:
    """落一条事件并（可能）排队作业。调用方负责 commit。

    校验顺序：密钥兜底扫描 → 幂等检查 → 落库 → 绑仓库 → 排队。
    """
    audit_secrets(envelope)

    existing = find_event(session, device.id, envelope.event_uid)
    if existing is not None:
        return IngestResult(
            status="duplicate",
            message="该事件已入库",
            event_id=existing.id,
            issue_id=None,
            issue_number=None,
        )

    body = summary(envelope)
    issue = resolve_issue(session, device.team_id, envelope)
    now = utcnow_iso()
    event = CodeEvent(
        team_id=device.team_id,
        device_id=device.id,
        user_id=device.user_id,
        event_uid=envelope.event_uid,
        kind=envelope.kind[:40],
        ts=(envelope.ts or now)[:32],
        branch=(envelope.repo.branch or "")[:160],
        head_commit=(envelope.repo.head or "")[:64],
        remote_hash=(envelope.repo.remote_hash or "")[:64],
        files=_dumps(body["files"]),
        stats=_dumps(body["stats"]),
        payload=_dumps({**body["payload"], "source": source}),
        redacted=1 if envelope.redacted else 0,
        bytes=len(_dumps(body["payload"]).encode("utf-8")),
        created_at=now,
    )
    session.add(event)
    session.flush()

    ensure_repo_binding(session, device, envelope)
    job = _queue_job(session, device, envelope, event)

    return IngestResult(
        status="stored",
        event_id=event.id,
        job_id=job.id,
        issue_id=issue.id if issue else None,
        issue_number=issue.number if issue else None,
    )


def recent_events(session: Session, team_id: int, *, limit: int = 50) -> list[CodeEvent]:
    return list(
        session.execute(
            select(CodeEvent)
            .where(CodeEvent.team_id == team_id)
            .order_by(CodeEvent.id.desc())
            .limit(limit)
        ).scalars()
    )


def event_dict(event: CodeEvent) -> dict:
    """给 API/前端的事件视图（不含完整 payload，避免泄漏代码特征）。"""
    files = _loads(event.files, [])
    stats = _loads(event.stats, {})
    return {
        "id": event.id,
        "team_id": event.team_id,
        "device_id": event.device_id,
        "event_uid": event.event_uid,
        "kind": event.kind,
        "ts": event.ts,
        "branch": event.branch,
        "head_commit": event.head_commit,
        "files": files[:20],
        "file_count": len(files),
        "stats": stats,
        "redacted": bool(event.redacted),
        "bytes": event.bytes,
        "created_at": event.created_at,
    }


__all__ = [
    "IngestResult",
    "MAX_EVENTS_PER_JOB",
    "ensure_repo_binding",
    "event_dict",
    "find_event",
    "ingest",
    "recent_events",
    "resolve_issue",
]
