"""Issue 查询/写入逻辑（GitHub 风格：团队内按 number 定位）。"""
from __future__ import annotations

from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session

from ..config import VALID_PRIORITY, VALID_STATUS
from ..db.base import today_iso, utcnow_iso
from ..db.models import Issue, Label, Milestone, Team
from . import weekplan
from .serializers import issue_dict

#: 列表排序：updated（最近更新，默认）| created | number | priority | due | week
SORTS = {
    "updated": (Issue.updated_at.desc(),),
    "created": (Issue.created_at.desc(),),
    "number": (Issue.number.desc(),),
    "week": (
        Issue.plan_week.is_(None),
        Issue.plan_week.asc(),
        Issue.number.desc(),
    ),
    "priority": (
        func.coalesce(
            func.nullif(Issue.priority, "high"), "zzz"
        ),
        Issue.number.desc(),
    ),
    "due": (
        Issue.due_date.is_(None),
        Issue.due_date.asc(),
        Issue.number.desc(),
    ),
}


def _int_or_none(value) -> int | None:
    text = str(value).strip() if value is not None else ""
    return int(text) if text.isdigit() else None


def find_by_number(session: Session, team_id: int, number: int) -> Issue | None:
    return session.scalar(
        select(Issue).where(Issue.team_id == team_id, Issue.number == number)
    )


def build_query(session: Session, team_id: int, filters: dict) -> Select:
    """按 query string 组装列表查询（GitHub 风格的过滤参数）。"""
    stmt = select(Issue).where(Issue.team_id == team_id)

    state = (filters.get("state") or "open").lower()
    if state in ("open", "closed"):
        stmt = stmt.where(Issue.state == state)

    status = filters.get("status")
    if status in VALID_STATUS:
        stmt = stmt.where(Issue.status == status)

    priority = filters.get("priority")
    if priority in VALID_PRIORITY:
        stmt = stmt.where(Issue.priority == priority)

    text = (filters.get("q") or "").strip()
    if text:
        like = f"%{text}%"
        stmt = stmt.where(or_(Issue.title.ilike(like), Issue.body.ilike(like)))

    label = _int_or_none(filters.get("label") or filters.get("label_id"))
    if label is not None:
        stmt = stmt.where(Issue.labels.any(Label.id == label))

    for key, column in (("assignee", "assignee_id"), ("milestone", "milestone_id")):
        raw = filters.get(key)
        if raw is None:
            raw = filters.get(f"{key}_id")
        text_value = str(raw).strip() if raw is not None else ""
        if text_value == "none":
            stmt = stmt.where(getattr(Issue, column).is_(None))
        elif text_value.isdigit():
            stmt = stmt.where(getattr(Issue, column) == int(text_value))

    raw_week = str(filters.get("week") or "").strip()
    if raw_week:
        if raw_week.lower() in weekplan.EMPTY_TOKENS:
            stmt = stmt.where(Issue.plan_week.is_(None))
        else:
            parsed_week = weekplan.parse_week(raw_week)
            if parsed_week:
                stmt = stmt.where(Issue.plan_week == parsed_week)

    order = SORTS.get((filters.get("sort") or "updated").lower(), SORTS["updated"])
    return stmt.order_by(*order)


def list_issues(session: Session, team_id: int, filters: dict) -> list[Issue]:
    return list(session.scalars(build_query(session, team_id, filters)))


def counts(session: Session, team_id: int) -> dict:
    total = session.scalar(
        select(func.count()).select_from(Issue).where(Issue.team_id == team_id)
    ) or 0
    closed = session.scalar(
        select(func.count()).select_from(Issue).where(
            Issue.team_id == team_id, Issue.state == "closed"
        )
    ) or 0
    return {"total": total, "open": total - closed, "closed": closed}


def parse_label_ids(values) -> list[int]:
    if values is None:
        return []
    if not isinstance(values, (list, tuple)):
        values = [values]
    out: list[int] = []
    for value in values:
        parsed = _int_or_none(value)
        if parsed is not None and parsed not in out:
            out.append(parsed)
    return out


def labels_for_team(session: Session, team_id: int, label_ids: list[int]) -> list[Label]:
    if not label_ids:
        return []
    stmt = select(Label).where(Label.team_id == team_id, Label.id.in_(label_ids))
    return list(session.scalars(stmt))


def apply_labels(session: Session, issue: Issue, label_ids: list[int]) -> None:
    issue.labels = labels_for_team(session, issue.team_id, label_ids)


def validate_milestone(session: Session, team_id: int, value) -> int | None:
    """里程碑必须属于同一团队；``None``/``""``/``null`` 表示清空。"""
    if value in (None, "", "null"):
        return None
    parsed = _int_or_none(value)
    if parsed is None:
        raise LookupError("里程碑不存在")
    milestone = session.get(Milestone, parsed)
    if milestone is None or milestone.team_id != team_id:
        raise LookupError("里程碑不存在")
    return parsed


def create_issue(session: Session, team: Team, data: dict, number: int) -> Issue:
    status = data.get("status") if data.get("status") in VALID_STATUS else "todo"
    priority = data.get("priority") if data.get("priority") in VALID_PRIORITY else "medium"
    state = "closed" if status == "done" else "open"
    now = utcnow_iso()
    issue = Issue(
        team_id=team.id,
        number=number,
        title=str(data["title"]).strip()[:200],
        body=(data.get("body") or "")[:20000],
        state=state,
        status=status,
        priority=priority,
        milestone_id=data.get("milestone_id"),
        assignee_id=_int_or_none(data.get("assignee_id")),
        author_id=_int_or_none(data.get("author_id")),
        due_date=data.get("due_date"),
        plan_week=weekplan.parse_week(data.get("plan_week")),
        created_at=now,
        updated_at=now,
        closed_at=now if state == "closed" else None,
    )
    issue.labels = labels_for_team(session, team.id, parse_label_ids(data.get("label_ids")))
    session.add(issue)
    session.flush()
    return issue


def update_issue(session: Session, issue: Issue, data: dict) -> tuple[Issue, str | None]:
    """返回 (issue, 第一条动态文案)；没有可更新字段时抛 ValueError。"""
    now = utcnow_iso()
    notes: list[str] = []
    touched = False

    if "title" in data:
        title = str(data.get("title") or "").strip()
        if not title:
            raise ValueError("请填写标题")
        issue.title = title[:200]
        touched = True
    if "body" in data:
        issue.body = (data.get("body") or "")[:20000]
        touched = True
    if "priority" in data and data.get("priority") in VALID_PRIORITY:
        issue.priority = data["priority"]
        touched = True
    if "milestone_id" in data:
        issue.milestone_id = validate_milestone(session, issue.team_id, data.get("milestone_id"))
        touched = True
    if "assignee_id" in data:
        issue.assignee_id = _int_or_none(data.get("assignee_id"))
        touched = True
    if "due_date" in data:
        issue.due_date = data.get("due_date") or None
        touched = True
    if "plan_week" in data:
        raw_week = data.get("plan_week")
        issue.plan_week = (
            None if str(raw_week or "").strip().lower() in weekplan.EMPTY_TOKENS | {""}
            else weekplan.require_week(raw_week)
        )
        touched = True

    if "status" in data and data.get("status") in VALID_STATUS:
        status = data["status"]
        previous = issue.status
        issue.status = status
        if status == "done":
            issue.state = "closed"
            issue.closed_at = now
            if previous != "done":
                notes.append("关闭了")
        else:
            issue.state = "open"
            issue.closed_at = None
            if previous == "done":
                notes.append("重新打开")
        touched = True
    elif data.get("state") in ("open", "closed"):
        state = data["state"]
        issue.state = state
        issue.status = "done" if state == "closed" else "todo"
        issue.closed_at = now if state == "closed" else None
        notes.append("关闭了" if state == "closed" else "重新打开")
        touched = True

    if "label_ids" in data:
        apply_labels(session, issue, parse_label_ids(data.get("label_ids")))
        touched = True

    if not touched:
        raise LookupError("empty")
    issue.updated_at = now
    session.flush()
    return issue, (notes[0] if notes else None)


def payload(issue: Issue) -> dict:
    return issue_dict(issue)
