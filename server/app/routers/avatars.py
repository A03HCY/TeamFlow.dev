"""头像：上传 / 删除 / 读取。

文件存放在本地目录（默认 ``data/avatars``，可用 ``TEAMFLOW_AVATAR_DIR`` 改位置），
业务层只认 key。浏览器统一走 ``GET /api/users/{id}/avatar``。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response, UploadFile

from ..config import AVATAR_MAX_BYTES
from ..db.models import User
from ..deps import Ctx, TeamAccess, get_ctx, team_access
from ..errors import ApiError
from ..services import activity, avatars
from ..services.serializers import public_user

router = APIRouter(prefix="/api/teams/{team}/members", tags=["avatars"])
public_router = APIRouter(prefix="/api", tags=["avatars"])


def _check_write(access: TeamAccess, target: User) -> None:
    """自己改自己的；管理员可以改别人的。"""
    access.require_edit()
    if access.is_admin():
        return
    acting = access.ctx.actor()[0]
    if acting != target.id:
        raise ApiError("只能修改自己的头像", 403)


@router.post("/{username}/avatar")
async def upload_member_avatar(username: str, access: TeamAccess = Depends(team_access),
                               file: UploadFile | None = None):
    user = access.member_or_404(username)
    _check_write(access, user)
    data = await file.read(AVATAR_MAX_BYTES + 1) if file is not None else b""
    try:
        result = avatars.save(access.session, user, data)
    except ValueError as exc:
        raise ApiError(str(exc)) from exc
    activity.log(access.session, access.team.id, *access.ctx.actor(), "updated", "member",
                 user.id, user.display_name, "更新了头像")
    return {"ok": True, "member": public_user(user), **result}


@router.delete("/{username}/avatar")
def remove_member_avatar(username: str, access: TeamAccess = Depends(team_access)):
    user = access.member_or_404(username)
    _check_write(access, user)
    result = avatars.remove(access.session, user)
    activity.log(access.session, access.team.id, *access.ctx.actor(), "updated", "member",
                 user.id, user.display_name, "移除了头像")
    return {"ok": True, "member": public_user(user), **result}


@public_router.get("/users/{user_id}/avatar")
def read_avatar(user_id: int, request: Request, ctx: Ctx = Depends(get_ctx)):
    user = ctx.session.get(User, user_id)
    if user is None:
        raise ApiError("用户不存在", 404)
    try:
        data = avatars.read(user)
    except LookupError as exc:
        raise ApiError(str(exc), 404) from exc

    etag = f'"{avatars.etag(user)}"'
    headers = {
        "ETag": etag,
        "Cache-Control": "no-cache",
        # 头像可能被当成用户内容渲染，禁掉嗅探与内联执行
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; sandbox",
    }
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return Response(content=data, media_type=avatars.mime_of(user.avatar or ""),
                    headers=headers)
