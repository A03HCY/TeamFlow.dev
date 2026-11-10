"""头像存储：一张图片进，一个 URL 出。

落盘在本地目录（默认 ``data/avatars``，可用 ``TEAMFLOW_AVATAR_DIR`` 换位置）。
数据库里只存 key（``12/ab12cd34.png``），浏览器统一通过
``GET /api/users/{id}/avatar`` 读取，所以文件目录不需要暴露成静态资源。
"""
from __future__ import annotations

import hashlib
import secrets
from pathlib import Path

from sqlalchemy.orm import Session

from ..config import AVATAR_MAX_BYTES, avatar_dir
from ..db.models import User

# 只收位图：按文件头判断，不信任 Content-Type
_SIGNATURES: tuple[tuple[bytes, str, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "png", "image/png"),
    (b"\xff\xd8\xff", "jpg", "image/jpeg"),
    (b"GIF87a", "gif", "image/gif"),
    (b"GIF89a", "gif", "image/gif"),
)
_MIME = {"png": "image/png", "jpg": "image/jpeg", "gif": "image/gif", "webp": "image/webp"}


def sniff(data: bytes) -> tuple[str, str] | None:
    """返回 (扩展名, MIME)；不是支持的位图就返回 None。"""
    for magic, ext, mime in _SIGNATURES:
        if data.startswith(magic):
            return ext, mime
    if len(data) > 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp", "image/webp"
    return None


def mime_of(key: str) -> str:
    return _MIME.get((key.rsplit(".", 1)[-1] or "").lower(), "application/octet-stream")


def path_of(key: str) -> Path:
    """key → 磁盘路径；拒绝逃逸出头像目录的 key。"""
    rel = (key or "").strip().replace("\\", "/")
    # 兼容旧版以 avatars/ 开头的 key
    if rel.startswith("avatars/"):
        rel = rel[len("avatars/"):]
    base = avatar_dir().resolve()
    target = (base / rel).resolve()
    if target != base and base not in target.parents:
        raise LookupError("头像路径非法")
    return target


def url(user: User | None) -> str:
    """前端的 <img src>。没设过头像就返回空串（前端回落成姓名首字）。"""
    if user is None or not (user.avatar or "").strip():
        return ""
    return f"/api/users/{user.id}/avatar"


def etag(user: User) -> str:
    """内容指纹：key 里带随机段，换头像就是换 key。"""
    return hashlib.sha1(f"{user.id}:{user.avatar}".encode()).hexdigest()[:16]


def save(session: Session, user: User, data: bytes) -> dict:
    """校验并写入头像，返回 {avatar_url, size}。"""
    if not data:
        raise ValueError("请选择一张图片")
    if len(data) > AVATAR_MAX_BYTES:
        raise ValueError(f"图片不能超过 {AVATAR_MAX_BYTES // (1024 * 1024)} MB")
    found = sniff(data)
    if found is None:
        raise ValueError("只支持 PNG / JPEG / GIF / WEBP 图片")
    ext, _ = found

    previous = (user.avatar or "").strip()
    key = f"{user.id}/{secrets.token_hex(8)}.{ext}"
    try:
        target = path_of(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    except OSError as exc:  # 目录不可写
        raise ValueError(f"头像存储不可用：{exc}") from exc

    user.avatar = key
    session.flush()
    if previous:
        discard(previous)
    return {"avatar_url": url(user), "size": len(data)}


def remove(session: Session, user: User) -> dict:
    key = (user.avatar or "").strip()
    if not key:
        return {"avatar_url": "", "removed": False}
    user.avatar = ""
    session.flush()
    discard(key)
    return {"avatar_url": "", "removed": True}


def discard(key: str) -> None:
    """删旧文件；删不掉也不算错误（不然换头像会被历史垃圾卡住）。"""
    try:
        path_of(key).unlink(missing_ok=True)
    except (LookupError, OSError):
        pass


def read(user: User) -> bytes:
    key = (user.avatar or "").strip()
    if not key:
        raise LookupError("还没有设置头像")
    try:
        return path_of(key).read_bytes()
    except LookupError as exc:
        raise LookupError("头像文件已丢失") from exc
    except FileNotFoundError as exc:
        raise LookupError("头像文件已丢失") from exc
    except OSError as exc:
        raise LookupError(f"头像存储不可用：{exc}") from exc
