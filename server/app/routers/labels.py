"""标签端点：``/api/teams/{slug}/labels/{name}``（GitHub 用名字定位标签）。"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from ..deps import TeamAccess, read_body, team_access
from ..errors import ApiError
from ..services import labels as label_service

router = APIRouter(prefix="/api/teams/{team}", tags=["labels"])


@router.get("/labels")
def list_labels(access: TeamAccess = Depends(team_access)) -> dict:
    return {
        "labels": label_service.list_labels(
            access.session, access.id, with_uses=access.can_edit()
        ),
        "can_edit": access.can_edit(),
    }


@router.post("/labels")
def create_label(body: dict = Depends(read_body),
                 access: TeamAccess = Depends(team_access)) -> dict:
    access.require_edit()
    label = label_service.create(access.session, access.id, body.get("name"), body.get("color"))
    return {"label": label_service.label_dict(label)}


@router.patch("/labels/{name}")
def update_label(name: str, body: dict = Depends(read_body),
                 access: TeamAccess = Depends(team_access)) -> dict:
    access.require_edit()
    session = access.session
    label = label_service.by_name(session, access.id, name)
    if label is None:
        raise ApiError("标签不存在", 404)
    label = label_service.update(session, label, body.get("name"), body.get("color"))
    return {"label": label_service.label_dict(label)}


@router.delete("/labels/{name}")
def delete_label(name: str, access: TeamAccess = Depends(team_access)) -> dict:
    access.require_edit()
    session = access.session
    label = label_service.by_name(session, access.id, name)
    if label is None:
        raise ApiError("标签不存在", 404)
    label_service.remove(session, label)
    return {"ok": True}
