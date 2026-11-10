"""周计划端点：``/api/teams/{slug}/week``。

一个周 = ISO 周（``2026-W45``）。返回「按人分组」的本周计划、未排期池，
以及上一周未完成项的顺延提示。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from ..deps import TeamAccess, read_body, team_access
from ..errors import ApiError
from ..services import activity, weekplan
from ..services.serializers import issue_dict
from ..services.teams import member_rows

router = APIRouter(prefix="/api/teams/{team}/week", tags=["week"])

_ROLLOVER_PREVIEW = 20


def _resolve(token) -> str:
    try:
        week = weekplan.resolve(token or "current")
    except ValueError as exc:
        raise ApiError(str(exc)) from exc
    return week or weekplan.current_week()


def _brief(issue) -> dict:
    return {
        "number": issue.number,
        "title": issue.title,
        "status": issue.status or "todo",
        "assignee_name": issue.assignee.display_name if issue.assignee is not None else "",
    }


@router.get("")
def week_plan(week: str | None = None, access: TeamAccess = Depends(team_access)) -> dict:
    access.require_read("issues")
    session = access.session
    target = _resolve(week)
    current = weekplan.current_week()
    previous = weekplan.shift_week(target, -1)

    items = weekplan.issues_in_week(session, access.id, target)
    pool = weekplan.unscheduled(session, access.id)

    buckets: dict[int | None, list] = {}
    for issue in items:
        buckets.setdefault(issue.assignee_id, []).append(issue)

    groups = []
    for member in member_rows(session, access.id):
        assigned = buckets.pop(member["id"], None)
        if not assigned:
            continue
        groups.append({
            "id": member["id"],
            "username": member.get("username", ""),
            "name": member.get("display_name") or member.get("username", ""),
            "avatar_url": member.get("avatar_url", ""),
            "items": [issue_dict(issue) for issue in assigned],
        })
    # 剩下的是「没有负责人」和「负责人已不在团队」的 Issue
    leftovers = [issue for bucket in buckets.values() for issue in bucket]
    leftovers.sort(key=lambda issue: issue.number)
    unassigned = [issue_dict(issue) for issue in leftovers]

    prev_open = weekplan.issues_in_week(session, access.id, previous, state="open")
    done = sum(1 for issue in items if issue.state == "closed")
    start, end = weekplan.week_start(target), weekplan.week_end(target)

    return {
        "week": target,
        "label": weekplan.label(target),
        "range": weekplan.range_text(target),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "prev": previous,
        "next": weekplan.shift_week(target, 1),
        "current": current,
        "groups": groups,
        "unassigned": unassigned,
        "unscheduled": [issue_dict(issue) for issue in pool],
        "stats": {
            "planned": len(items),
            "open": len(items) - done,
            "done": done,
            "unscheduled": len(pool),
        },
        "rollover": {
            "from": previous,
            "count": len(prev_open),
            "items": [_brief(issue) for issue in prev_open[:_ROLLOVER_PREVIEW]],
        },
        "can_edit": access.can_edit(),
    }


@router.post("/rollover")
def rollover(body: dict = Depends(read_body),
             access: TeamAccess = Depends(team_access)) -> dict:
    """把上一周没做完的 Issue 顺延到本周（可以只挑一部分）。"""
    access.require_edit()
    session, team = access.session, access.team
    target = _resolve(body.get("to") or "current")
    source = _resolve(body.get("from") or weekplan.shift_week(target, -1))
    if source == target:
        raise ApiError("来源周和目标周不能相同")
    raw_numbers = body.get("numbers")
    numbers: list[int] | None = None
    if isinstance(raw_numbers, (list, tuple)):
        numbers = [int(value) for value in raw_numbers if str(value).isdigit()]
        if not numbers:
            raise ApiError("没有选中要顺延的 Issue")
    moved = weekplan.rollover(session, access.id, source, target, numbers)
    if moved:
        actor_id, actor_name = access.ctx.actor()
        activity.log(
            session, team.id, actor_id, actor_name, "updated", "week", 0,
            target, f"从 {source} 顺延 {len(moved)} 条",
        )
    return {
        "ok": True,
        "count": len(moved),
        "from": source,
        "to": target,
        "numbers": [issue.number for issue in moved],
    }
