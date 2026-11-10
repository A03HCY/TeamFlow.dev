"""里程碑端点：``/api/teams/{slug}/milestones/{number}``。"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from ..deps import TeamAccess, read_body, team_access
from ..errors import ApiError
from ..services import activity
from ..services import milestones as milestone_service
from ..services.serializers import milestone_counts, milestone_dict

router = APIRouter(prefix="/api/teams/{team}", tags=["milestones"])


def _payload(access: TeamAccess, milestone) -> dict:
    counts = milestone_counts(access.session, access.id)
    return milestone_dict(milestone, counts)


@router.get("/milestones")
def list_milestones(access: TeamAccess = Depends(team_access)) -> dict:
    access.require_read("milestones")
    return {
        "milestones": milestone_service.listing(access.session, access.id),
        "can_edit": access.can_edit(),
    }


@router.post("/milestones")
def create_milestone(body: dict = Depends(read_body),
                     access: TeamAccess = Depends(team_access)) -> dict:
    access.require_edit()
    session, team = access.session, access.team
    milestone = milestone_service.create(session, team, body)
    actor_id, actor_name = access.ctx.actor()
    activity.log(session, team.id, actor_id, actor_name, "created", "milestone",
                 milestone.id, milestone.title)
    return {"milestone": _payload(access, milestone)}


@router.patch("/milestones/{number}")
def update_milestone(number: int, body: dict = Depends(read_body),
                     access: TeamAccess = Depends(team_access)) -> dict:
    access.require_edit()
    session = access.session
    milestone = milestone_service.get_by_number(session, access.id, number)
    if milestone is None:
        raise ApiError("里程碑不存在", 404)
    try:
        milestone, note = milestone_service.update(session, milestone, body)
    except LookupError as exc:
        raise ApiError("没有需要更新的字段") from exc
    actor_id, actor_name = access.ctx.actor()
    if note in ("closed", "reopened"):
        activity.log(session, access.id, actor_id, actor_name, "closed" if note == "closed"
                     else "reopened", "milestone", milestone.id, milestone.title)
    else:
        activity.log(session, access.id, actor_id, actor_name, "updated", "milestone",
                     milestone.id, milestone.title)
    return {"milestone": _payload(access, milestone)}


@router.delete("/milestones/{number}")
def delete_milestone(number: int, access: TeamAccess = Depends(team_access)) -> dict:
    access.require_edit()
    session = access.session
    milestone = milestone_service.get_by_number(session, access.id, number)
    if milestone is None:
        raise ApiError("里程碑不存在", 404)
    title = milestone.title
    milestone_id = milestone.id
    milestone_service.remove(session, milestone)
    actor_id, actor_name = access.ctx.actor()
    activity.log(session, access.id, actor_id, actor_name, "deleted", "milestone", milestone_id,
                 title)
    return {"ok": True}
