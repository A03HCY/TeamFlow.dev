"""推断管线（P1：规则版；P2 把 ``analyze`` 换成 Analyst Agent）。

一次作业 = 一批事件（合并窗口内累积），处理步骤：

1. 取作业 payload 里的事件 id，读回 ``code_events.payload``（完整事件包）；
2. 对每条事件跑 ``rules.analyze``，取置信度最高的那条当「主证据」；
3. 过一遍 ``engine.check_analysis``（与 LLM 版同一套防幻觉闸门）；
4. 置信度太低 → 只留事件不建洞察（``ENGINE_RULES_MIN_CONFIDENCE``）；
5. 否则 upsert 洞察：同一 ``dedupe_key`` 的 draft 在合并窗口内**合并**而不是
   重复刷卡片，合并动作也记进 ``insight_events``（可回放）。

洞察状态机（P1 只会产生 ``draft``）：draft → confirmed / rejected → reverted。
确认与写回在 P2 由收件箱 API 触发；这里保证「产生」这一步幂等且可审计。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...config import (
    ENGINE_INSIGHT_MERGE_MINUTES,
    ENGINE_RULES_MIN_CONFIDENCE,
)
from ...db.base import utcnow_iso
from ...db.engine_models import CodeEvent, InferenceJob, Insight, InsightEvent
from ...db.models import Issue
from ..engine import check_analysis, clamp_analysis, looks_like_injection
from ..inference import audit, rules
from ..inference.events import CodeEventEnvelope

SUGGESTED_KINDS = ("progress", "unscoped", "hint")
MODEL_TAG = "rules/p1"


@dataclass
class PipelineResult:
    ok: bool
    job_id: int
    insight_id: int | None = None
    created: bool = False
    merged: bool = False
    skipped: str = ""
    events: int = 0
    confidence: float = 0.0

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "job_id": self.job_id,
            "insight_id": self.insight_id,
            "created": self.created,
            "merged": self.merged,
            "skipped": self.skipped,
            "events": self.events,
            "confidence": self.confidence,
        }


def _loads(raw: str | None, fallback):
    if not raw:
        return fallback
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return fallback


def _dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def next_seq(session: Session, insight_id: int) -> int:
    row = session.execute(
        select(InsightEvent.seq)
        .where(InsightEvent.insight_id == insight_id)
        .order_by(InsightEvent.seq.desc())
        .limit(1)
    ).scalar_one_or_none()
    return int(row or 0) + 1


def append_insight_event(
    session: Session, insight: Insight, event_type: str, detail: dict | None = None
) -> InsightEvent:
    row = InsightEvent(
        insight_id=insight.id,
        seq=next_seq(session, insight.id),
        type=event_type[:40],
        detail=_dumps(detail or {}),
        created_at=utcnow_iso(),
    )
    session.add(row)
    session.flush()
    return row


def dedupe_key_for(issue_id: int | None, branch: str, kind: str) -> str:
    """同一议题 + 同一分支 + 同一类型的洞察视为同一条（合并窗口内不重复）。"""
    head = f"issue{issue_id}" if issue_id else "unscoped"
    branch_part = (branch or "-").strip().lower()[:80]
    return f"{head}:{branch_part}:{kind}"[:160]


def _envelope(event: CodeEvent) -> CodeEventEnvelope | None:
    payload = _loads(event.payload, {})
    if not payload:
        return None
    try:
        return CodeEventEnvelope.model_validate(payload)
    except Exception:  # noqa: BLE001 - 历史数据可能不完整，跳过即可
        return None


def _pick_best(results: list[dict]) -> dict | None:
    if not results:
        return None
    return max(results, key=lambda item: float(item["analysis"].get("confidence") or 0))


def _merge_window_cutoff() -> str:
    return (
        datetime.now(timezone.utc) - timedelta(minutes=ENGINE_INSIGHT_MERGE_MINUTES)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")


def _find_open_insight(session: Session, team_id: int, dedupe_key: str) -> Insight | None:
    return session.execute(
        select(Insight)
        .where(
            Insight.team_id == team_id,
            Insight.dedupe_key == dedupe_key,
            Insight.state == "draft",
            Insight.created_at >= _merge_window_cutoff(),
        )
        .order_by(Insight.id.desc())
        .limit(1)
    ).scalar_one_or_none()


def _issue(session: Session, team_id: int, number: int) -> Issue | None:
    if not number:
        return None
    return session.execute(
        select(Issue).where(Issue.team_id == team_id, Issue.number == int(number))
    ).scalar_one_or_none()


def run_job(session: Session, job: InferenceJob) -> PipelineResult:
    """跑一个作业；不动 job 的状态（由 worker 负责），只产生洞察。"""
    payload = _loads(job.payload, {})
    event_ids = [int(item) for item in (payload.get("events") or []) if str(item).isdigit()]
    events = (
        list(
            session.execute(
                select(CodeEvent).where(CodeEvent.id.in_(event_ids)).order_by(CodeEvent.id)
            ).scalars()
        )
        if event_ids
        else []
    )
    if not events:
        return PipelineResult(ok=True, job_id=job.id, skipped="没有可用事件")

    analyzed: list[dict] = []
    for event in events:
        envelope = _envelope(event)
        if envelope is None:
            continue
        issue = (
            _issue(session, job.team_id, rules.branch_issue_number(envelope.repo.branch) or 0)
            if envelope.repo.branch
            else None
        )
        item = rules.analyze(envelope, issue_title=issue.title if issue else None)
        item["issue"] = issue
        analyzed.append(item)

    best = _pick_best(analyzed)
    if best is None:
        return PipelineResult(ok=True, job_id=job.id, events=len(events), skipped="事件包无法解析")

    analysis = best["analysis"]
    signals: rules.Signals = best["signals"]
    issue: Issue | None = best.get("issue")
    confidence = float(analysis.get("confidence") or 0.0)

    # 语义闸门：规则版也走同一套（证据为空/无测试判完成/注入文本都会被挡）
    problems = check_analysis(analysis)
    if problems:
        return PipelineResult(
            ok=True,
            job_id=job.id,
            events=len(events),
            confidence=confidence,
            skipped="未通过防幻觉闸门：" + "；".join(problems),
        )
    analysis = clamp_analysis(analysis)

    if confidence < ENGINE_RULES_MIN_CONFIDENCE:
        return PipelineResult(
            ok=True,
            job_id=job.id,
            events=len(events),
            confidence=confidence,
            skipped=f"置信度 {confidence:.2f} 低于阈值 {ENGINE_RULES_MIN_CONFIDENCE:.2f}",
        )

    kind = "progress" if issue is not None else "unscoped"
    dedupe_key = dedupe_key_for(issue.id if issue else None, signals.branch, kind)
    evidence = list(analysis.get("evidence") or [])
    risks = list(analysis.get("risks") or [])
    suggestion = {
        "status": analysis.get("status") or "in_progress",
        "plan_week": best.get("plan_week") or "",
        "comment": "",
        "completion": analysis.get("completion"),
        "dry_run": True,
    }
    if looks_like_injection(json.dumps(signals.top_files, ensure_ascii=False)):
        risks.append("文件路径里出现疑似提示词注入内容，已按数据处理")

    summary_text = str(analysis.get("summary") or "")[:4000]
    title = str(best.get("title") or "进度线索")[:240]

    existing = _find_open_insight(session, job.team_id, dedupe_key)
    if existing is not None:
        existing.title = title
        existing.summary = summary_text
        existing.confidence = confidence
        existing.evidence = _dumps(evidence)
        existing.suggestion = _dumps(suggestion)
        existing.job_id = job.id
        existing.model = MODEL_TAG
        session.flush()
        append_insight_event(
            session,
            existing,
            "merged",
            {
                "job_id": job.id,
                "confidence": confidence,
                "events": [event.id for event in events],
                "notes": best.get("notes") or [],
            },
        )
        return PipelineResult(
            ok=True,
            job_id=job.id,
            insight_id=existing.id,
            merged=True,
            events=len(events),
            confidence=confidence,
        )

    insight = Insight(
        team_id=job.team_id,
        device_id=events[-1].device_id if events else None,
        job_id=job.id,
        issue_id=issue.id if issue else None,
        milestone_id=getattr(issue, "milestone_id", None) if issue else None,
        kind=kind,
        title=title,
        summary=summary_text,
        confidence=confidence,
        state="draft",
        evidence=_dumps(evidence),
        suggestion=_dumps(suggestion),
        applied="{}",
        model=MODEL_TAG,
        dedupe_key=dedupe_key,
        created_at=utcnow_iso(),
        decided_at=None,
        decided_by=None,
        expires_at=None,
    )
    session.add(insight)
    session.flush()
    append_insight_event(
        session,
        insight,
        "created",
        {
            "job_id": job.id,
            "confidence": confidence,
            "risks": risks,
            "notes": best.get("notes") or [],
            "events": [event.id for event in events],
        },
    )
    audit.append(
        session,
        team_id=job.team_id,
        action="insight.created",
        subject_type="insight",
        subject_id=insight.id,
        payload={
            "issue_id": insight.issue_id,
            "issue_number": issue.number if issue else None,
            "confidence": confidence,
            "kind": kind,
            "job_id": job.id,
            "model": MODEL_TAG,
        },
        actor=(None, "推理引擎"),
    )
    return PipelineResult(
        ok=True,
        job_id=job.id,
        insight_id=insight.id,
        created=True,
        events=len(events),
        confidence=confidence,
    )


def insight_dict(
    insight: Insight,
    *,
    events: list[InsightEvent] | None = None,
    issue_number: int | None = None,
) -> dict:
    """收件箱/前端用的视图。"""
    return {
        "id": insight.id,
        "team_id": insight.team_id,
        "device_id": insight.device_id,
        "job_id": insight.job_id,
        "issue_id": insight.issue_id,
        "issue_number": issue_number,
        "milestone_id": insight.milestone_id,
        "kind": insight.kind,
        "title": insight.title,
        "summary": insight.summary,
        "confidence": float(insight.confidence or 0.0),
        "state": insight.state,
        "evidence": _loads(insight.evidence, []),
        "suggestion": _loads(insight.suggestion, {}),
        "applied": _loads(insight.applied, {}),
        "model": insight.model,
        "dedupe_key": insight.dedupe_key,
        "created_at": insight.created_at,
        "decided_at": insight.decided_at,
        "decided_by": insight.decided_by,
        "decided_by_user_id": insight.decided_by_user_id,
        "expires_at": insight.expires_at,
        "events": [
            {
                "seq": item.seq,
                "type": item.type,
                "detail": _loads(item.detail, {}),
                "created_at": item.created_at,
            }
            for item in (events or [])
        ],
    }


__all__ = [
    "MODEL_TAG",
    "PipelineResult",
    "SUGGESTED_KINDS",
    "append_insight_event",
    "dedupe_key_for",
    "insight_dict",
    "next_seq",
    "run_job",
]
