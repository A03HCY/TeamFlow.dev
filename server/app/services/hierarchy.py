"""议题层级（父子议题）：深度校验、环检测、派生状态与叶子计数口径。

设计约束（见 docs/改型方案 §3.10）：

* 深度上限 3 层（顶层 → 子 → 孙）；
* 编号仍用团队内全局 ``issue_seq``，父子关系只存在 ``parent_id`` 里；
* 统计口径一律「只算叶子」，避免父议题与子议题被重复计数；
* 父议题状态派生：子议题全部关闭时提示可关闭，但不自动关闭。
"""
from __future__ import annotations

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from ..db.models import Issue
from ..errors import ApiError

#: 议题层级上限（含顶层）
MAX_DEPTH = 3


def _int_or_none(value) -> int | None:
    text = str(value).strip() if value is not None else ""
    return int(text) if text.isdigit() else None


def leaf_condition():
    """「没有子议题」的 SQL 条件：统计只算叶子时统一用它。"""
    parents = select(Issue.parent_id).where(Issue.parent_id.is_not(None))
    return Issue.id.not_in(parents)


def children_of(session: Session, issue: Issue) -> list[Issue]:
    return list(
        session.scalars(
            select(Issue)
            .where(Issue.parent_id == issue.id)
            .order_by(Issue.sort_order, Issue.number)
        )
    )


def descendants_of(session: Session, issue: Issue) -> list[Issue]:
    """广度优先展开整棵子树（深度已由写入校验保证 ≤3）。"""
    out: list[Issue] = []
    frontier = [issue.id]
    seen = {issue.id}
    while frontier:
        rows = list(
            session.scalars(
                select(Issue)
                .where(Issue.parent_id.in_(frontier))
                .order_by(Issue.sort_order, Issue.number)
            )
        )
        frontier = []
        for row in rows:
            if row.id in seen:
                continue
            seen.add(row.id)
            out.append(row)
            frontier.append(row.id)
    return out


def ancestors_of(session: Session, issue: Issue) -> list[Issue]:
    """根 → 父 的顺序（面包屑用）。"""
    chain: list[Issue] = []
    current = issue
    guard = 0
    while current.parent_id and guard < MAX_DEPTH + 1:
        parent = session.get(Issue, current.parent_id)
        if parent is None:
            break
        chain.append(parent)
        current = parent
        guard += 1
    chain.reverse()
    return chain


def depth_of(session: Session, issue: Issue) -> int:
    """顶层 = 1。"""
    return 1 + len(ancestors_of(session, issue))


def subtree_height(session: Session, issue: Issue) -> int:
    """以自身为根的最大深度（叶子 = 1）。"""
    rows = descendants_of(session, issue)
    if not rows:
        return 1
    by_id = {row.id: row for row in rows}
    parent_map = {row.id: row.parent_id for row in rows}
    best = 1
    for row_id in by_id:
        level = 1
        cursor = parent_map.get(row_id)
        while cursor and cursor in by_id:
            level += 1
            cursor = parent_map.get(cursor)
        best = max(best, level + 1)
    return best


def resolve_parent(
    session: Session,
    team_id: int,
    issue: Issue | None,
    value,
) -> int | None:
    """把请求里的 ``parent_id`` / ``parent``（议题编号）解析成父议题的数据库 id。

    ``None`` / ``""`` / ``"null"`` / ``"none"`` 表示提升为顶层。
    """
    if value in (None, "", "null", "none"):
        return None
    text = str(value).strip()
    parent: Issue | None = None
    if text.isdigit():
        parent = session.get(Issue, int(text))
        if parent is None or parent.team_id != team_id:
            # 兼容前端传「议题编号」而非数据库 id
            parent = session.scalar(
                select(Issue).where(
                    Issue.team_id == team_id, Issue.number == int(text)
                )
            )
    if parent is None:
        raise LookupError("父议题不存在")
    if issue is not None:
        if parent.id == issue.id:
            raise ApiError("不能把议题挂到自己身上", code="cycle_detected")
        # 环检测：沿父链上溯，遇到自己就拒绝
        cursor: Issue | None = parent
        guard = 0
        while cursor is not None and guard < MAX_DEPTH + 2:
            if cursor.id == issue.id:
                raise ApiError(
                    "不能把议题挂到它自己的子议题下", code="cycle_detected"
                )
            cursor = session.get(Issue, cursor.parent_id) if cursor.parent_id else None
            guard += 1
    new_depth = depth_of(session, parent) + 1
    height = subtree_height(session, issue) if issue is not None else 1
    if new_depth + height - 1 > MAX_DEPTH:
        raise ApiError(
            f"议题层级最多 {MAX_DEPTH} 层，当前会变成 {new_depth + height - 1} 层",
            code="depth_exceeded",
        )
    return parent.id


def rollup(session: Session, issue: Issue) -> dict:
    """父议题的进度：叶子子议题里已关闭的比例。"""
    rows = descendants_of(session, issue)
    if not rows:
        return {"total": 0, "closed": 0, "percent": 0}
    parent_ids = {row.parent_id for row in rows}
    leaves = [row for row in rows if row.id not in parent_ids]
    total = len(leaves)
    closed = sum(1 for row in leaves if row.state == "closed")
    percent = round(closed / total * 100) if total else 0
    return {"total": total, "closed": closed, "percent": percent}


def guard_parent_close(session: Session, issue: Issue) -> None:
    """父议题还有未关闭的子议题时，不允许直接标完成。"""
    pending = session.scalar(
        select(func.count())
        .select_from(Issue)
        .where(Issue.parent_id == issue.id, Issue.state != "closed")
    ) or 0
    if pending:
        raise ApiError(
            f"还有 {pending} 个子议题未完成，不能直接关闭父议题",
            status=409,
            code="has_children",
        )


def can_close_parent(session: Session, issue: Issue) -> bool:
    """子议题全部关闭 → 父议题可关闭（前端提示用，不自动执行）。"""
    total = session.scalar(
        select(func.count()).select_from(Issue).where(Issue.parent_id == issue.id)
    ) or 0
    if total == 0:
        return False
    pending = session.scalar(
        select(func.count())
        .select_from(Issue)
        .where(Issue.parent_id == issue.id, Issue.state != "closed")
    ) or 0
    return pending == 0


def has_children(session: Session, issue: Issue) -> bool:
    return bool(
        session.scalar(
            select(func.count()).select_from(Issue).where(Issue.parent_id == issue.id)
        )
    )


def tree_query(session: Session, team_id: int, filters: dict) -> Select:
    """列表的层级过滤：``parent=<id|none>``。"""
    stmt = select(Issue).where(Issue.team_id == team_id)
    raw = filters.get("parent")
    text = str(raw).strip().lower() if raw is not None else ""
    if text in ("none", "root", "top"):
        stmt = stmt.where(Issue.parent_id.is_(None))
    elif text.isdigit():
        parent = session.get(Issue, int(text))
        if parent is None:
            parent = session.scalar(
                select(Issue).where(Issue.team_id == team_id, Issue.number == int(text))
            )
        stmt = stmt.where(Issue.parent_id == (parent.id if parent else -1))
    return stmt
