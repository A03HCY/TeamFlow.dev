"""提案执行器：把 `insights.suggestion.actions` 落到业务表（事务 + 快照）。

安全约束（见 docs/改型方案 §5.8）：

* ``dry_run``（默认）只回预览，不写业务表；
* 真实执行整段在**一个事务**里，任一动作失败全部回滚；
* 执行前记录 ``before`` 快照到 ``insights.applied``，5 分钟内可 ``revert``；
* 每一步都写 ``insight_events`` 与 ``inference_audit``（哈希链）。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...db.base import utcnow_iso
from ...db.engine_models import Insight, InsightEvent
from ...db.models import Comment, Issue, Label, Milestone, Team, User
from ...errors import ApiError
from .. import hierarchy
from ..inference import audit
from ..issues import create_issue, find_by_number, update_issue
from ..milestones import create as create_milestone
from ..milestones import get_by_number as get_milestone
from ..teams import next_issue_number

#: 允许执行的 op（白名单，未知 op 直接报错而不是静默跳过）
OPS = ("create_issue", "create_milestone", "update_issue", "update_milestone",
       "close_issue", "assign")

#: 撤销窗口（分钟）
REVERT_MINUTES = 5

_SNAPSHOT_FIELDS = ("title", "body", "state", "status", "priority", "milestone_id",
                    "assignee_id", "due_date", "plan_week", "parent_id",
                    "sort_order", "estimate_hours")


def _suggestion(insight: Insight) -> dict:
    try:
        data = json.loads(insight.suggestion or "{}")
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def actions_of(insight: Insight) -> list[dict]:
    return [item for item in _suggestion(insight).get("actions") or [] if isinstance(item, dict)]


def describe(action: dict) -> str:
    op = action.get("op")
    title = str(action.get("title") or "")
    if op == "create_issue":
        parent = action.get("parent")
        return f"新建议题「{title}」" + (f"（父议题 #{parent}）" if parent else "")
    if op == "create_milestone":
        return f"新建里程碑「{title}」"
    if op == "update_issue":
        return f"更新议题 #{action.get('issue')}"
    if op == "update_milestone":
        return f"更新里程碑 #{action.get('milestone')}（验收标准）"
    if op == "close_issue":
        return f"关闭议题 #{action.get('issue')}"
    if op == "assign":
        return f"把 #{action.get('issue')} 指派给 {action.get('assignee_name') or action.get('assignee')}"
    return f"未知动作 {op}"


def preview(insight: Insight) -> list[dict]:
    return [
        {"op": action.get("op"), "text": describe(action)}
        for action in actions_of(insight)
        if action.get("op") in OPS
    ]


# ------------------------------------------------------------------ 字段解析

def _resolve_issue(session: Session, team_id: int, value) -> Issue:
    number = int(str(value).strip()) if str(value).strip().isdigit() else None
    issue = find_by_number(session, team_id, number) if number else None
    if issue is None:
        raise ApiError(f"提案引用的议题不存在：#{value}")
    return issue


def _resolve_milestone(session: Session, team_id: int, value) -> Milestone:
    number = int(str(value).strip()) if str(value).strip().isdigit() else None
    milestone = get_milestone(session, team_id, number) if number else None
    if milestone is None:
        raise ApiError(f"提案引用的里程碑不存在：#{value}")
    return milestone


def _resolve_assignee(session: Session, team: Team, value) -> int | None:
    if value in (None, "", "null"):
        return None
    text = str(value).strip()
    if text.isdigit():
        return int(text) if session.get(User, int(text)) else None
    user = session.scalar(select(User).where(User.username == text))
    if user is None:
        user = session.scalar(select(User).where(User.display_name == text))
    return user.id if user is not None else None


def _resolve_labels(session: Session, team_id: int, action: dict) -> list[int]:
    ids: list[int] = []
    for raw in action.get("label_ids") or []:
        if str(raw).strip().isdigit():
            ids.append(int(str(raw).strip()))
    for name in action.get("labels") or []:
        found = session.scalar(
            select(Label.id).where(Label.team_id == team_id, Label.name == str(name))
        )
        if found is not None:
            ids.append(int(found))
    return list(dict.fromkeys(ids))


def _issue_before(issue: Issue) -> dict:
    return {field: getattr(issue, field, None) for field in _SNAPSHOT_FIELDS}


# ------------------------------------------------------------------ 执行

def execute(
    session: Session,
    team: Team,
    insight: Insight,
    *,
    actor_id: int | None,
    actor_name: str,
    dry_run: bool = True,
) -> dict:
    """执行提案；``dry_run`` 为真时只返回预览。"""
    actions = [item for item in actions_of(insight) if item.get("op") in OPS]
    if not actions:
        raise ApiError("这条提案没有可执行的动作")

    if dry_run:
        snapshot = {
            "at": utcnow_iso(),
            "actor": actor_name,
            "dry_run": True,
            "preview": preview(insight),
        }
        insight.applied = json.dumps(snapshot, ensure_ascii=False)
        _event(session, insight, "previewed", {"actions": len(actions)})
        audit.append(
            session,
            team_id=team.id,
            action="insight.previewed",
            subject_type="insight",
            subject_id=str(insight.id),
            payload={"actions": len(actions)},
            actor=(actor_id, actor_name),
        )
        session.flush()
        return {"dry_run": True, "applied": 0, "preview": snapshot["preview"]}

    created_issues: list[int] = []
    created_milestones: list[int] = []
    updated: list[dict] = []
    results: list[dict] = []

    for action in actions:
        op = action.get("op")
        if op == "create_issue":
            data = {
                "title": str(action.get("title") or "").strip(),
                "body": action.get("body") or "",
                "status": action.get("status") or "todo",
                "priority": action.get("priority") or "medium",
                "assignee_id": _resolve_assignee(session, team, action.get("assignee")),
                "estimate_hours": action.get("estimate_hours"),
                "due_date": action.get("due_date"),
                "plan_week": action.get("plan_week"),
                "label_ids": _resolve_labels(session, team.id, action),
            }
            if not data["title"]:
                raise ApiError("提案里的新议题缺少标题")
            if action.get("parent") not in (None, ""):
                parent = _resolve_issue(session, team.id, action.get("parent"))
                data["parent_id"] = parent.number
                if data.get("milestone_id") in (None, ""):
                    data["milestone_id"] = parent.milestone_id
            if action.get("milestone") not in (None, ""):
                milestone = _resolve_milestone(session, team.id, action.get("milestone"))
                data["milestone_id"] = milestone.id
            issue = create_issue(session, team, data, next_issue_number(session, team))
            created_issues.append(issue.id)
            results.append({"op": op, "ok": True, "number": issue.number,
                            "text": f"新建 #{issue.number} {issue.title}"})
        elif op == "create_milestone":
            title = str(action.get("title") or "").strip()
            if not title:
                raise ApiError("提案里的新里程碑缺少名称")
            milestone = create_milestone(session, team, {
                "title": title,
                "description": action.get("description") or "",
                "acceptance": action.get("acceptance") or "",
                "kind": action.get("kind") or "scheduled",
                "start_date": action.get("start_date"),
                "due_date": action.get("due_date"),
            })
            created_milestones.append(milestone.id)
            results.append({"op": op, "ok": True, "number": milestone.number,
                            "text": f"新建里程碑 #{milestone.number} {milestone.title}"})
        elif op == "update_issue":
            issue = _resolve_issue(session, team.id, action.get("issue"))
            before = _issue_before(issue)
            payload = {key: action[key] for key in _SNAPSHOT_FIELDS if key in action}
            if "milestone" in action and action.get("milestone") not in (None, ""):
                payload["milestone_id"] = _resolve_milestone(
                    session, team.id, action.get("milestone")
                ).id
            if "assignee" in action:
                payload["assignee_id"] = _resolve_assignee(session, team, action.get("assignee"))
            if "parent" in action:
                payload["parent_id"] = (
                    None if action.get("parent") in (None, "", "none")
                    else _resolve_issue(session, team.id, action.get("parent")).number
                )
            update_issue(session, issue, payload)
            updated.append({"id": issue.id, "kind": "issue", "before": before})
            results.append({"op": op, "ok": True, "number": issue.number,
                            "text": f"更新 #{issue.number}"})
        elif op == "close_issue":
            issue = _resolve_issue(session, team.id, action.get("issue"))
            before = _issue_before(issue)
            update_issue(session, issue, {"status": "done"})
            updated.append({"id": issue.id, "kind": "issue", "before": before})
            results.append({"op": op, "ok": True, "number": issue.number,
                            "text": f"关闭 #{issue.number}"})
        elif op == "assign":
            issue = _resolve_issue(session, team.id, action.get("issue"))
            before = _issue_before(issue)
            assignee_id = _resolve_assignee(session, team, action.get("assignee"))
            if assignee_id is None:
                raise ApiError("提案里的负责人不存在")
            update_issue(session, issue, {"assignee_id": assignee_id})
            updated.append({"id": issue.id, "kind": "issue", "before": before})
            results.append({"op": op, "ok": True, "number": issue.number,
                            "text": f"指派 #{issue.number}"})
        elif op == "update_milestone":
            milestone = _resolve_milestone(session, team.id, action.get("milestone"))
            before = {
                "title": milestone.title,
                "description": milestone.description,
                "acceptance": milestone.acceptance,
                "state": milestone.state,
                "start_date": milestone.start_date,
                "due_date": milestone.due_date,
            }
            payload = {key: action[key] for key in before if key in action}
            if payload:
                from ..milestones import update as update_milestone

                update_milestone(session, milestone, payload)
            updated.append({"id": milestone.id, "kind": "milestone", "before": before})
            results.append({"op": op, "ok": True, "number": milestone.number,
                            "text": f"更新里程碑 #{milestone.number}"})
        else:  # pragma: no cover - 白名单已过滤
            raise ApiError(f"不支持的动作：{op}")

    snapshot = {
        "at": utcnow_iso(),
        "actor": actor_name,
        "dry_run": False,
        "created": {"issues": created_issues, "milestones": created_milestones},
        "updated": updated,
        "results": results,
    }
    insight.applied = json.dumps(snapshot, ensure_ascii=False)
    insight.state = "confirmed"
    insight.decided_at = utcnow_iso()
    insight.decided_by = actor_name[:80]
    insight.decided_by_user_id = actor_id
    _event(session, insight, "confirmed", {"actions": len(actions)})
    _event(session, insight, "applied", {"created": snapshot["created"]})
    audit.append(
        session,
        team_id=team.id,
        action="insight.applied",
        subject_type="insight",
        subject_id=str(insight.id),
        payload={
            "kind": insight.kind,
            "created_issues": created_issues,
            "created_milestones": created_milestones,
            "updated": [item["id"] for item in updated],
        },
        actor=(actor_id, actor_name),
    )
    session.flush()
    return {
        "dry_run": False,
        "applied": len(results),
        "created": snapshot["created"],
        "results": results,
    }


# ------------------------------------------------------------------ 撤销

def revert(
    session: Session,
    team: Team,
    insight: Insight,
    *,
    actor_id: int | None,
    actor_name: str,
) -> dict:
    if insight.state != "confirmed":
        raise ApiError("只有已执行的提案可以撤销", status=409, code="already_decided")
    snapshot = {}
    try:
        snapshot = json.loads(insight.applied or "{}")
    except (TypeError, ValueError):
        snapshot = {}
    if snapshot.get("dry_run") or not snapshot.get("at"):
        raise ApiError("这条提案还没有真正写入", status=409)
    if _minutes_since(snapshot.get("at")) > REVERT_MINUTES:
        raise ApiError(
            f"超过 {REVERT_MINUTES} 分钟的提案不能再撤销，请手工调整",
            status=409,
        )

    removed: list[str] = []
    for issue_id in snapshot.get("created", {}).get("issues", []):
        issue = session.get(Issue, int(issue_id))
        if issue is None:
            continue
        if hierarchy.has_children(session, issue):
            raise ApiError(
                f"#{issue.number} 已经产生了子议题，不能再整体撤销", status=409
            )
        comments = session.scalar(
            select(func.count()).select_from(Comment).where(Comment.issue_id == issue.id)
        ) or 0
        if comments:
            raise ApiError(f"#{issue.number} 已经有 {comments} 条评论，不能再整体撤销", status=409)
        session.delete(issue)
        removed.append(f"issue:#{issue.number}")
    for milestone_id in snapshot.get("created", {}).get("milestones", []):
        milestone = session.get(Milestone, int(milestone_id))
        if milestone is None:
            continue
        attached = session.scalar(
            select(func.count()).select_from(Issue).where(Issue.milestone_id == milestone.id)
        ) or 0
        if attached:
            raise ApiError(f"里程碑「{milestone.title}」下已有议题，不能撤销", status=409)
        session.delete(milestone)
        removed.append(f"milestone:#{milestone.number}")

    restored: list[str] = []
    for item in snapshot.get("updated", []):
        model = Issue if item.get("kind") == "issue" else Milestone
        row = session.get(model, int(item.get("id") or 0))
        if row is None:
            continue
        for field, value in (item.get("before") or {}).items():
            if hasattr(row, field):
                setattr(row, field, value)
        restored.append(f"{item.get('kind')}:{item.get('id')}")

    session.flush()
    insight.state = "reverted"
    insight.applied = json.dumps(
        {**snapshot, "reverted_at": utcnow_iso(), "reverted_by": actor_name},
        ensure_ascii=False,
    )
    _event(session, insight, "reverted", {"removed": removed, "restored": restored})
    audit.append(
        session,
        team_id=team.id,
        action="insight.reverted",
        subject_type="insight",
        subject_id=str(insight.id),
        payload={"removed": removed, "restored": restored},
        actor=(actor_id, actor_name),
    )
    session.flush()
    return {"ok": True, "removed": removed, "restored": restored}


def _minutes_since(iso_text: str | None) -> float:
    if not iso_text:
        return 1e9
    try:
        stamp = datetime.strptime(iso_text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return 1e9
    return (datetime.now(timezone.utc) - stamp).total_seconds() / 60


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
