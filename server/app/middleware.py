"""写操作保护：写前自动备份 + 单团队模式的编辑口令拦截。

* 非 GET/HEAD/OPTIONS 的 ``/api/*`` 请求会先做一次 sqlite 快照（有节流，
  见 :func:`app.db.backup.backup_now`），数据被误改时可以直接回滚。
* 设置了编辑口令且未解锁时，写操作返回 401 + ``code=edit_locked``，
  前端据此弹出解锁框（``/api/edit/unlock`` 本身放行）。
"""
from __future__ import annotations

import gzip

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from .config import COMPRESS_MIN_BYTES
from .db.backup import backup_now
from .db.session import session_scope
from .deps import edit_enabled, edit_unlocked
from .security import EDIT_COOKIE
from .services.settings import require_login_on

SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}
EXEMPT_PATHS = {"/api/edit/unlock"}
# 只读的 AI 端点：不改数据库，允许未解锁的访客使用（真正的写操作仍被口令拦住）。
EXEMPT_SUFFIXES = ("/ai/chat",)
EXEMPT_PREFIXES = ("/api/ai/project/draft",)


def _exempt(path: str) -> bool:
    if path in EXEMPT_PATHS:
        return True
    if path.startswith(EXEMPT_PREFIXES):
        return True
    return path.endswith(EXEMPT_SUFFIXES)


class WriteGuardMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.method in SAFE_METHODS or not request.url.path.startswith("/api"):
            return await call_next(request)
        if _exempt(request.url.path):
            return await call_next(request)

        try:
            backup_now()
        except Exception:  # pragma: no cover - 备份失败不应阻断业务
            pass

        with session_scope() as session:
            locked = (
                not require_login_on(session)
                and edit_enabled(session)
                and not edit_unlocked(session, request.cookies.get(EDIT_COOKIE))
            )
        if locked:
            return JSONResponse(
                {"error": "需要编辑口令", "code": "edit_locked"}, status_code=401
            )
        return await call_next(request)


# ------------------------------------------------------------------ 响应压缩

# 只压缩这几种类型：文本、JSON、脚本、SVG。图片/视频/下载一律不碰，
# 否则 gzip 白烧 CPU，还会让断点续传/下载进度变得难看。
COMPRESSIBLE_PREFIXES = (b"text/",)
COMPRESSIBLE_TYPES = {
    b"application/json",
    b"application/javascript",
    b"application/xml",
    b"image/svg+xml",
}
SKIP_HEADERS = {b"content-encoding", b"content-length"}


def _header(headers, name: bytes) -> bytes | None:
    for key, value in headers:
        if key.lower() == name:
            return value
    return None


class CompressionMiddleware:
    """给文本响应加 gzip。

    不用 Starlette 自带的 ``GZipMiddleware``，因为那会把云盘里 200 MB 的
    文件下载也压一遍；这里按 Content-Type 与 Content-Disposition 精确放行。
    """

    def __init__(self, app) -> None:  # noqa: ANN001
        self.app = app

    async def __call__(self, scope, receive, send):  # noqa: ANN001
        if scope["type"] != "http" or scope["method"] not in ("GET",):
            return await self.app(scope, receive, send)

        accepted = b""
        for key, value in scope["headers"]:
            if key.lower() == b"accept-encoding":
                accepted = value.lower()
                break
        if b"gzip" not in accepted:
            return await self.app(scope, receive, send)

        held: dict = {"start": None, "chunks": [], "on": False}

        async def send_wrapper(message):  # noqa: ANN001
            if message["type"] == "http.response.start":
                headers = message["headers"]
                ctype = (_header(headers, b"content-type") or b"").split(b";")[0]
                ctype = ctype.strip().lower()
                disposition = _header(headers, b"content-disposition") or b""
                length = _header(headers, b"content-length")
                compressible = (
                    ctype.startswith(COMPRESSIBLE_PREFIXES)
                    or ctype in COMPRESSIBLE_TYPES
                )
                big_enough = length is None or int(length) >= COMPRESS_MIN_BYTES
                held["on"] = (
                    compressible
                    and big_enough
                    and not _header(headers, b"content-encoding")
                    and b"attachment" not in disposition.lower()
                )
                if held["on"]:
                    held["start"] = message
                    return
                await send(message)
                return

            if message["type"] == "http.response.body" and held["on"]:
                held["chunks"].append(message.get("body", b""))
                if message.get("more_body"):
                    return
                body = gzip.compress(b"".join(held["chunks"]), compresslevel=6)
                start = held["start"]
                headers = [
                    (key, value) for key, value in start["headers"]
                    if key.lower() not in SKIP_HEADERS
                ]
                headers.append((b"content-encoding", b"gzip"))
                headers.append((b"content-length", str(len(body)).encode()))
                headers.append((b"vary", b"Accept-Encoding"))
                await send({
                    "type": "http.response.start",
                    "status": start["status"],
                    "headers": headers,
                })
                await send({"type": "http.response.body", "body": body})
                return

            await send(message)

        await self.app(scope, receive, send_wrapper)
