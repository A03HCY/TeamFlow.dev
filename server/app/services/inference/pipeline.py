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

from ...config import ENGINE_INSIGHT_MERGE_MINUTES
from ...db.base import utcnow_iso
from ...db.engine_models import CodeEvent, InferenceJob, Insight, InsightEvent
from ...db.models import Issue
from ..engine import check_analysis, clamp_analysis, looks_like_injection
from ..inference import admission, audit, rules
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
    #: 准入结论（verdict / reason / source），skip 时也要能复盘「为什么没建卡」
    admission: dict | None = None

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
            "admission": self.admission,
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


def _progress_comment(signals, summary_text: str, progress: str = "") -> str:
    """写回议题的进度记录（人话版：这一轮到底做了什么）。

    ``progress`` 是模型给的「这一步实现了什么」；有它就先说能力，
    再列改动事实——纯文件清单对人没有阅读价值。
    """
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    detail = rules.digest_line(signals)
    lines = [f"**端侧自动分析（{stamp}）**", ""]
    if progress:
        lines.append(f"- 进度：{progress}")
    lines.append(f"- 本轮改动：{detail}")
    lines.append(f"- 判断结论：{summary_text}")
    return "\n".join(lines)[:2000]


def _progress_body(signals) -> str:
    """追加到议题正文末尾的进度段（保留历史，不覆盖人工写的描述）。"""
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    return f"\n\n---\n**自动进度 · {stamp}**\n\n- {rules.digest_line(signals)}\n"


def _actions_for(
    issue,
    analysis: dict,
    signals,
    title: str,
    summary_text: str,
    best: dict,
    decision: admission.Decision | None = None,
) -> list[dict]:
    """把一条洞察翻成可执行动作。

    没有 ``actions`` 的洞察在「预览并应用」里会直接报「没有可执行的动作」，
    于是永远停在待确认——这不是提示问题，是链路断在这里。

    有议题时必须**同时**带上 ``comment`` 与 ``body_append``：只改 status 的话，
    议题状态本来就在「进行中」时点了应用等于什么都没发生，用户会认为 AI 没干活。

    模型给出的建议（改状态 / 改正文 / 新建议题 / 新增或修改里程碑）在这里被翻成
    动作，但**不直接落库**——仍然要人在卡片上点「预览并应用」。模型能提议，
    人保留否决权，这是这套系统的底线。
    """
    plan_issue = dict(getattr(decision, "issue_update", None) or {})
    plan_new = dict(getattr(decision, "new_issue", None) or {})
    plan_ms = dict(getattr(decision, "milestone", None) or {})

    status = str(plan_issue.get("status") or analysis.get("status") or "in_progress")
    week = str(best.get("plan_week") or "")
    actions: list[dict] = []

    # 里程碑要先建、后挂：新建的议题可以直接落到这个里程碑下（milestone="new"）
    if plan_ms.get("action") == "create":
        actions.append({
            "op": "create_milestone",
            "title": plan_ms["title"],
            "description": plan_ms.get("description") or "",
            "acceptance": plan_ms.get("acceptance") or "",
            "kind": "scheduled",
        })

    if issue is not None:
        action = {
            "op": "update_issue",
            "issue": issue.number,
            "status": status,
            # 进度记录：落到 comments 表，议题页立刻能看到
            "comment": _progress_comment(signals, summary_text, plan_issue.get("progress", "")),
            # 覆写：把本轮进度追加到正文，不覆盖原有描述
            "body_append": plan_issue.get("body_append") or _progress_body(signals),
        }
        if plan_issue.get("priority"):
            action["priority"] = plan_issue["priority"]
        if week:
            action["plan_week"] = week
        actions.append(action)
    else:
        branch = str(getattr(signals, "branch", "") or "主分支")
        detail = rules.digest_line(signals)
        body = plan_new.get("body") or f"{summary_text}\n\n本轮改动：{detail}"
        action = {
            "op": "create_issue",
            # 模型起的标题更像人话；没有就用端侧模板兜底
            "title": str(plan_new.get("title") or f"[端侧] {branch}：{title}")[:240],
            "body": body[:4000],
            "status": "todo" if status == "todo" else "in_progress",
            "plan_week": week,
        }
        if plan_new.get("priority"):
            action["priority"] = plan_new["priority"]
        if plan_new.get("milestone"):
            action["milestone"] = plan_new["milestone"]
        elif plan_ms.get("action") == "create":
            # 本轮同时新建了里程碑：把新议题挂上去，由执行器按顺序解析
            action["milestone"] = "new"
        actions.append(action)

    if plan_ms.get("action") == "update":
        action = {"op": "update_milestone", "milestone": plan_ms["number"]}
        for key in ("title", "description", "acceptance", "state"):
            if plan_ms.get(key):
                action[key] = plan_ms[key]
        actions.append(action)

    return actions


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

    # 准入闸门：这一段决定「这条事件该不该变成洞察、属于哪个议题」。
    # 以前这里只有一句 `confidence < 0.2 就丢`，而置信度只看文件后缀，
    # 于是「改一行注释」能建卡、「写一页需求文档」被丢掉。判断逻辑现在
    # 集中在 admission：硬性噪声先挡、分支号优先归属、灰色地带问模型。
    decision = admission.decide(
        session,
        team_id=job.team_id,
        signals=signals,
        confidence=confidence,
        has_issue=issue is not None,
    )
    if decision.skips:
        return PipelineResult(
            ok=True,
            job_id=job.id,
            events=len(events),
            confidence=confidence,
            skipped=f"准入拒绝：{decision.reason}",
            admission=decision.as_dict(),
        )

    # 模型可能在候选议题里找到了分支名之外的归属；找不到才维持原判断
    if decision.issue_number and (issue is None or issue.number != decision.issue_number):
        matched = _issue(session, job.team_id, decision.issue_number)
        if matched is not None:
            issue = matched
    if decision.verdict == admission.VERDICT_CREATE:
        # 明确要求新开一条：即使分支名指向某个议题，也不要把新功能塞进旧议题
        issue = None

    kind = "progress" if issue is not None else "unscoped"
    dedupe_key = dedupe_key_for(issue.id if issue else None, signals.branch, kind)
    evidence = list(analysis.get("evidence") or [])
    risks = list(analysis.get("risks") or [])
    summary_text = str(analysis.get("summary") or "")[:4000]
    title = str(best.get("title") or "进度线索")[:240]
    if issue is not None:
        # 归属可能是准入阶段才由模型定下来的（分支名没带编号），标题必须跟着改，
        # 否则会出现「标题写未关联议题、卡片却挂着 #17」这种自相矛盾
        title = f"#{issue.number} {issue.title}"[:240]
    elif decision.title:
        # 模型读懂了这次改动的主题，标题比「新增了 xxx()」更像人话
        title = f"未关联议题的变更：{decision.title}"[:240]
    suggestion = {
        "status": analysis.get("status") or "in_progress",
        "plan_week": best.get("plan_week") or "",
        # 预览区展示「应用后会追加什么」，避免用户点了之后看不出变化
        "comment": _progress_comment(
            signals, summary_text, str(decision.issue_update.get("progress") or "")
        )[:800],
        "completion": analysis.get("completion"),
        "dry_run": True,
        # 没有 actions 的洞察在「预览并应用」里是死路（会提示没有可执行动作），
        # 所以每条洞察都必须能落到业务表：有议题就更新状态，没议题就建议一条新议题
        "actions": _actions_for(issue, analysis, signals, title, summary_text, best, decision),
        # 把「为什么建这张卡 / 为什么挂这个议题」一并落库，事后可复盘
        "admission": decision.as_dict(),
    }
    model_tag = MODEL_TAG if decision.source == "rules" else f"{MODEL_TAG}+ai-gate"
    if looks_like_injection(json.dumps(signals.top_files, ensure_ascii=False)):
        risks.append("文件路径里出现疑似提示词注入内容，已按数据处理")
    # 注释线索是端侧的自由文本（虽然已限长），同样按不可信输入处理
    if signals.notes_added and looks_like_injection(json.dumps(signals.notes_added, ensure_ascii=False)):
        risks.append("改动说明里出现疑似提示词注入内容，已按数据处理")

    existing = _find_open_insight(session, job.team_id, dedupe_key)
    if existing is not None:
        existing.title = title
        existing.summary = summary_text
        existing.confidence = confidence
        existing.evidence = _dumps(evidence)
        existing.suggestion = _dumps(suggestion)
        existing.job_id = job.id
        existing.model = model_tag
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
            admission=decision.as_dict(),
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
        model=model_tag,
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
            "model": model_tag,
            "admission": decision.as_dict(),
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
        admission=decision.as_dict(),
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
