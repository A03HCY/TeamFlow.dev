"""规划与拆分提案（P2.5）：把「接下来该拆成什么」变成可审阅的 draft 洞察。

设计要点（见 docs/改型方案 §5.8 / §5.9）：

* **只产出提案**：写 `insights`（`state=draft`），不直接改业务表；
* **可解释**：每条提案都带 `evidence`、`assumptions`、`warnings` 与置信度；
* **可重复**：`dedupe_key` 保证同一主体 30 分钟内只有一条 draft（重新请求只刷新内容）；
* 当前实现是**规则版**（不需要 LLM 也能跑）；P2 接模型后只需替换 `_draft_*` 的内容来源。

支持 `kind`：

======  ======================================================
kind    触发与产出
======  ======================================================
split   部分完成的议题 → 按目录/文件簇拆成子议题（或平级）
plan    里程碑 → 按描述/验收清单生成任务树
criteria 里程碑缺少验收标准 → 从描述生成清单草稿
assignment 按技能 + 历史 + 负载给出负责人建议
======  ======================================================
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...db.base import utcnow_iso
from ...db.engine_models import CodeEvent, Insight, InsightEvent
from ...db.models import Issue, Label, Milestone, Team, TeamMember, User
from ...errors import ApiError
from .. import hierarchy
from ..engine import looks_like_injection
from ..inference import audit
from ..inference.events import CodeEventEnvelope
from ..inference.rules import branch_issue_number, is_test_path
from ..teams import parse_skills

#: 单次提案最多生成多少条动作（超出截断，避免刷屏）
MAX_ACTIONS = 8
#: 同主体重复提案的最小间隔（分钟）
MERGE_MINUTES = 30
#: 提案置信度的上下限
MIN_CONFIDENCE = 0.35
MAX_CONFIDENCE = 0.85

KINDS = ("split", "plan", "criteria", "assignment")


# ------------------------------------------------------------------ 小工具

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ago_iso(minutes: int) -> str:
    return (_now() - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def proposal_key(team_id: int, kind: str, subject: str, digest: str = "") -> str:
    raw = f"{team_id}|{kind}|{subject}|{digest}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:32]


def _evidence(kind: str, ref: str, note: str = "") -> dict:
    return {"type": kind, "ref": ref[:200], "note": note[:200]}


def _member_skills(session: Session, team_id: int) -> dict[int, list[dict]]:
    rows = session.execute(
        select(TeamMember.user_id, TeamMember.skills).where(TeamMember.team_id == team_id)
    )
    return {int(uid): parse_skills(skills) for uid, skills in rows}


def _issue_load(session: Session, team_id: int) -> dict[int, int]:
    """每个成员当前的未关闭叶子议题数（负载）。"""
    stmt = (
        select(Issue.assignee_id, func.count())
        .where(
            Issue.team_id == team_id,
            Issue.state != "closed",
            Issue.assignee_id.is_not(None),
            hierarchy.leaf_condition(),
        )
        .group_by(Issue.assignee_id)
    )
    return {int(uid): int(count) for uid, count in session.execute(stmt)}


def _recent_envelopes(
    session: Session, team_id: int, issue_number: int | None = None, limit: int = 200
) -> list[tuple[CodeEvent, CodeEventEnvelope]]:
    rows = list(
        session.scalars(
            select(CodeEvent)
            .where(CodeEvent.team_id == team_id)
            .order_by(CodeEvent.id.desc())
            .limit(limit)
        )
    )
    out: list[tuple[CodeEvent, CodeEventEnvelope]] = []
    for row in rows:
        try:
            payload = json.loads(row.payload or "{}")
            envelope = CodeEventEnvelope.model_validate(payload)
        except Exception:  # noqa: BLE001 - 老数据/坏数据直接跳过
            continue
        if issue_number is not None:
            number = branch_issue_number(envelope.repo.branch or "")
            if number != issue_number:
                continue
        out.append((row, envelope))
    return out


def _record(
    session: Session,
    *,
    team_id: int,
    kind: str,
    title: str,
    summary: str,
    confidence: float,
    evidence: list[dict],
    actions: list[dict],
    rationale: str = "",
    assumptions: list[str] | None = None,
    warnings: list[str] | None = None,
    dedupe_key: str,
    issue_id: int | None = None,
    milestone_id: int | None = None,
    model: str = "rules/p2.5",
) -> Insight:
    """写入或合并一条 draft 提案，并追加 `insight_events` 与审计。"""
    suggestion = {
        "summary": summary,
        "rationale": rationale,
        "actions": actions,
        "assumptions": assumptions or [],
        "warnings": warnings or [],
    }
    existing = session.scalar(
        select(Insight)
        .where(
            Insight.team_id == team_id,
            Insight.dedupe_key == dedupe_key,
            Insight.state == "draft",
            Insight.created_at >= _ago_iso(MERGE_MINUTES),
        )
        .order_by(Insight.id.desc())
    )
    if existing is not None:
        existing.title = title[:240]
        existing.summary = summary
        existing.confidence = round(confidence, 3)
        existing.evidence = json.dumps(evidence, ensure_ascii=False)
        existing.suggestion = json.dumps(suggestion, ensure_ascii=False)
        existing.issue_id = issue_id
        existing.milestone_id = milestone_id
        existing.model = model
        _event(session, existing, "merged", {"actions": len(actions)})
        session.flush()
        return existing

    insight = Insight(
        team_id=team_id,
        device_id=None,
        job_id=None,
        issue_id=issue_id,
        milestone_id=milestone_id,
        kind=kind,
        title=title[:240],
        summary=summary,
        confidence=round(confidence, 3),
        state="draft",
        evidence=json.dumps(evidence, ensure_ascii=False),
        suggestion=json.dumps(suggestion, ensure_ascii=False),
        applied=json.dumps({}, ensure_ascii=False),
        model=model,
        dedupe_key=dedupe_key,
        created_at=utcnow_iso(),
    )
    session.add(insight)
    session.flush()
    _event(session, insight, "created", {"actions": len(actions), "kind": kind})
    audit.append(
        session,
        team_id=team_id,
        action="insight.created",
        subject_type="insight",
        subject_id=str(insight.id),
        payload={"kind": kind, "title": title, "actions": len(actions)},
        actor=(None, "推理引擎"),
    )
    session.flush()
    return insight


def _event(session: Session, insight: Insight, kind: str, detail: dict | None = None) -> None:
    seq = (
        session.scalar(
            select(func.max(InsightEvent.seq)).where(InsightEvent.insight_id == insight.id)
        )
        or 0
    ) + 1
    session.add(
        InsightEvent(
            insight_id=insight.id,
            seq=seq,
            type=kind,
            detail=json.dumps(detail or {}, ensure_ascii=False),
            created_at=utcnow_iso(),
        )
    )


# ------------------------------------------------------------------ split

def _group_files(files: list[str]) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    for path in files:
        parts = path.split("/")
        key = parts[0] if len(parts) > 1 else "根目录"
        groups.setdefault(key, []).append(path)
    return groups


def _checklist_items(text: str) -> list[str]:
    items: list[str] = []
    for line in (text or "").splitlines():
        line = line.strip()
        for prefix in ("- [ ]", "- [x]", "* [ ]", "- ", "* "):
            if line.startswith(prefix):
                line = line[len(prefix):].strip()
                break
        else:
            continue
        if 2 <= len(line) <= 120:
            items.append(line)
    return items


def split_proposal(session: Session, team: Team, issue: Issue) -> Insight | None:
    """对「做了一部分」的议题给出拆分建议。"""
    if hierarchy.has_children(session, issue):
        raise ApiError(
            "该议题已经有子议题，只能对叶子议题继续拆分",
            status=409,
            code="has_children",
        )
    events = _recent_envelopes(session, team.id, issue.number)
    files: list[str] = []
    commits: list[str] = []
    for row, envelope in events:
        files.extend(item.path for item in envelope.files)
        if envelope.repo.head and envelope.repo.head not in commits:
            commits.append(envelope.repo.head)
    unique_files = list(dict.fromkeys(files))
    groups = _group_files(unique_files)

    actions: list[dict] = []
    evidence: list[dict] = []
    warnings: list[str] = []
    assumptions: list[str] = []

    if len(groups) >= 2:
        for key, paths in list(groups.items())[:MAX_ACTIONS]:
            test_count = sum(1 for path in paths if is_test_path(path))
            actions.append({
                "op": "create_issue",
                "parent": issue.number,
                "title": f"{issue.title}：{key} 部分（{len(paths)} 个文件）"[:200],
                "body": "拆分自 #%d。涉及文件：\n\n%s" % (
                    issue.number,
                    "\n".join(f"- `{path}`" for path in paths[:20]),
                ),
                "status": "todo",
                "estimate_hours": max(1, min(16, math.ceil(len(paths) * 1.5))),
            })
            evidence.append(_evidence("file", paths[0], f"{key}：{len(paths)} 个文件"))
            if test_count:
                evidence.append(
                    _evidence("test", next(p for p in paths if is_test_path(p)), "该簇含测试")
                )
        confidence = min(MAX_CONFIDENCE, 0.55 + 0.05 * len(actions))
        rationale = (
            f"近期 {len(events)} 条事件改动了 {len(unique_files)} 个文件，"
            f"分布在 {len(groups)} 个顶层目录，适合按模块拆成子议题。"
        )
    else:
        items = _checklist_items(issue.body or "")
        if len(items) < 2:
            return None
        for item in items[:MAX_ACTIONS]:
            actions.append({
                "op": "create_issue",
                "parent": issue.number,
                "title": item[:200],
                "body": f"拆分自 #{issue.number}（原文清单项）。",
                "status": "todo",
            })
        confidence = MIN_CONFIDENCE
        rationale = "议题正文里已有未完成的清单项，建议逐项落成子议题。"
        warnings.append("没有代码事件证据，拆分依据仅来自议题正文")
        assumptions.append("清单项按原文顺序拆成子议题")

    if commits:
        evidence.append(_evidence("commit", commits[0], f"最近提交 {len(commits)} 个"))
    if any(looks_like_injection(path) for path in unique_files):
        warnings.append("文件路径里出现疑似提示词注入内容，已按数据处理")

    summary = f"建议把 #{issue.number}《{issue.title}》拆成 {len(actions)} 个子议题"
    dedupe = proposal_key(team.id, "split", f"issue:{issue.id}")
    return _record(
        session,
        team_id=team.id,
        kind="split",
        title=f"拆分建议：#{issue.number} {issue.title}",
        summary=summary,
        confidence=confidence,
        evidence=evidence,
        actions=actions,
        rationale=rationale,
        assumptions=assumptions,
        warnings=warnings,
        dedupe_key=dedupe,
        issue_id=issue.id,
    )


# ------------------------------------------------------------------ assignment

def _issue_tags(issue: Issue) -> list[str]:
    return [item.name.strip().lower() for item in (issue.labels or []) if item.name]


def assignment_scores(session: Session, team: Team, issue: Issue) -> list[dict]:
    """确定性打分：0.5*技能覆盖 + 0.3*历史同类 + 0.2*空闲度。"""
    tags = set(_issue_tags(issue))
    skills = _member_skills(session, team.id)
    load = _issue_load(session, team.id)
    rows = session.execute(
        select(TeamMember.user_id, User.display_name)
        .join(User, User.id == TeamMember.user_id)
        .where(TeamMember.team_id == team.id)
    ).all()
    max_load = max(load.values(), default=0)

    # 历史同类：该成员关闭过带相同标签的议题数量
    history: dict[int, int] = {}
    if tags:
        stmt = (
            select(Issue.assignee_id, Label.name, func.count())
            .join(Issue.labels)
            .where(Issue.team_id == team.id, Issue.state == "closed")
            .group_by(Issue.assignee_id, Label.name)
        )
        for user_id, name, count in session.execute(stmt):
            if user_id is None or (name or "").strip().lower() not in tags:
                continue
            history[int(user_id)] = history.get(int(user_id), 0) + int(count)
    max_history = max(history.values(), default=0)

    out: list[dict] = []
    for user_id, display_name in rows:
        member_skills = {item["name"].lower() for item in skills.get(int(user_id), [])}
        coverage = (len(tags & member_skills) / len(tags)) if tags else 0.0
        hist = (history.get(int(user_id), 0) / max_history) if max_history else 0.0
        busy = load.get(int(user_id), 0)
        idle = 1 - (busy / max_load) if max_load else 1.0
        score = 0.5 * coverage + 0.3 * hist + 0.2 * idle
        out.append({
            "id": int(user_id),
            "display_name": display_name,
            "score": round(score, 3),
            "coverage": round(coverage, 3),
            "history": round(hist, 3),
            "open_count": busy,
            "skills": sorted(member_skills),
        })
    out.sort(key=lambda row: (-row["score"], row["open_count"]))
    return out


def assignment_proposal(session: Session, team: Team, issue: Issue) -> Insight | None:
    if issue.assignee_id is not None and issue.state != "closed":
        # 已有人负责时只在「负载明显偏高」时提示改派，避免打扰
        rows = assignment_scores(session, team, issue)
        current = next((row for row in rows if row["id"] == issue.assignee_id), None)
        best = rows[0] if rows else None
        if not current or not best or best["id"] == current["id"]:
            return None
        if current["open_count"] - best["open_count"] < 3:
            return None
    else:
        rows = assignment_scores(session, team, issue)
        if not rows:
            return None
        best = rows[0]
        if best["score"] <= 0:
            return None

    best = rows[0]
    tags = _issue_tags(issue)
    evidence = [_evidence("issue", f"#{issue.number}", f"标签：{', '.join(tags) or '无'}")]
    assumptions: list[str] = []
    warnings: list[str] = []
    if not best["skills"]:
        assumptions.append("成员技能未录入，仅按职位与负载建议")
        warnings.append("建议在成员设置里补充技能标签，匹配会更准")
    matched = sorted(set(tags) & set(best["skills"]))
    rationale = (
        f"{best['display_name']} 当前未关闭 {best['open_count']} 个议题，"
        f"技能覆盖率 {int(best['coverage'] * 100)}%"
        + (f"（匹配：{', '.join(matched)}）" if matched else "")
        + "，综合得分最高。"
    )
    action = {
        "op": "assign",
        "issue": issue.number,
        "assignee": best["id"],
        "assignee_name": best["display_name"],
    }
    return _record(
        session,
        team_id=team.id,
        kind="assignment",
        title=f"负责人建议：#{issue.number} {issue.title}",
        summary=f"建议把 #{issue.number} 指派给 {best['display_name']}（得分 {best['score']:.2f}）",
        confidence=min(MAX_CONFIDENCE, 0.4 + 0.4 * best["score"]),
        evidence=evidence,
        actions=[action],
        rationale=rationale,
        assumptions=assumptions,
        warnings=warnings,
        dedupe_key=proposal_key(team.id, "assignment", f"issue:{issue.id}"),
        issue_id=issue.id,
    )


# ------------------------------------------------------------------ plan / criteria

def criteria_proposal(session: Session, team: Team, milestone: Milestone) -> Insight | None:
    if (milestone.acceptance or "").strip():
        return None
    text = (milestone.description or "").strip()
    if not text:
        return None
    items = _checklist_items(text)
    if not items:
        items = [
            part.strip("。;；,. ")
            for part in text.replace("\n", "。").split("。")
            if 2 < len(part.strip()) <= 120
        ][:MAX_ACTIONS]
    if not items:
        return None
    checklist = "\n".join(f"- [ ] {item}" for item in items[:MAX_ACTIONS])
    return _record(
        session,
        team_id=team.id,
        kind="criteria",
        title=f"验收标准草稿：{milestone.title}",
        summary=f"根据里程碑描述生成 {len(items[:MAX_ACTIONS])} 条验收标准，确认后写回里程碑",
        confidence=MIN_CONFIDENCE,
        evidence=[_evidence("issue", f"milestone:{milestone.number}", "来源：里程碑描述")],
        actions=[{
            "op": "update_milestone",
            "milestone": milestone.number,
            "acceptance": checklist,
        }],
        rationale="里程碑缺少验收标准时，先用描述里的条目生成可勾选清单，等人确认。",
        assumptions=["验收标准由里程碑描述拆分而来，需人工补充量化指标"],
        dedupe_key=proposal_key(team.id, "criteria", f"milestone:{milestone.id}"),
        milestone_id=milestone.id,
    )


def plan_proposal(session: Session, team: Team, milestone: Milestone) -> Insight | None:
    items = _checklist_items(milestone.acceptance or "") or _checklist_items(
        milestone.description or ""
    )
    items = [item for item in items if len(item) >= 2][:MAX_ACTIONS]
    if not items:
        return None
    existing = session.scalar(
        select(func.count())
        .select_from(Issue)
        .where(Issue.team_id == team.id, Issue.milestone_id == milestone.id)
    ) or 0
    if existing:
        # 已经有挂在该里程碑下的议题时，只对剩下没覆盖到的条目补拆
        titles = {
            (issue.title or "").strip()
            for issue in session.scalars(
                select(Issue).where(
                    Issue.team_id == team.id, Issue.milestone_id == milestone.id
                )
            )
        }
        items = [item for item in items if item not in titles]
        if not items:
            return None
    actions = [
        {
            "op": "create_issue",
            "milestone": milestone.number,
            "title": item[:200],
            "body": f"来自里程碑《{milestone.title}》的规划。",
            "status": "todo",
            "estimate_hours": 4,
        }
        for item in items
    ]
    return _record(
        session,
        team_id=team.id,
        kind="plan",
        title=f"任务拆解建议：{milestone.title}",
        summary=f"建议把里程碑《{milestone.title}》拆成 {len(actions)} 个议题",
        confidence=MIN_CONFIDENCE + 0.05,
        evidence=[_evidence("issue", f"milestone:{milestone.number}", "来源：里程碑验收条目")],
        actions=actions,
        rationale="里程碑验收清单里的每条可交付项落成一个议题，便于排期与指派。",
        assumptions=["按验收条目一对一建议题，未做工作量评估"],
        dedupe_key=proposal_key(team.id, "plan", f"milestone:{milestone.id}"),
        milestone_id=milestone.id,
    )


# ------------------------------------------------------------------ 入口

def build_proposal(
    session: Session,
    team: Team,
    kind: str,
    *,
    issue: Issue | None = None,
    milestone: Milestone | None = None,
) -> Insight | None:
    if kind not in KINDS:
        raise ApiError(f"不支持的提案类型：{kind}", code="bad_proposal_kind")
    if kind in ("split", "assignment"):
        if issue is None:
            raise ApiError("该提案需要指定议题")
        return split_proposal(session, team, issue) if kind == "split" else assignment_proposal(
            session, team, issue
        )
    if milestone is None:
        raise ApiError("该提案需要指定里程碑")
    return (
        criteria_proposal(session, team, milestone)
        if kind == "criteria"
        else plan_proposal(session, team, milestone)
    )
