"""FastAPI 应用装配：异常处理、写保护中间件、静态资源。"""
from __future__ import annotations

import logging
import mimetypes
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from .config import APP_NAME, APP_VERSION, STATIC_DIR, hsts_enabled
from .db.session import init_db
from .errors import ApiError
from .middleware import CompressionMiddleware, WriteGuardMiddleware
from .routers import routers
from .security import is_https

INDEX_FILE = "index.html"
# 显式给前端资源定 MIME：mimetypes 在 Windows 上会读注册表，.js 可能被猜成 text/plain，
# 而 ES 模块对 MIME 类型是硬要求（再叠加 nosniff 就会直接拒绝加载）。
MEDIA_TYPES = {
    ".js": "text/javascript",
    ".mjs": "text/javascript",
    ".css": "text/css",
    ".html": "text/html",
    ".json": "application/json",
    ".map": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
}
CHARSET_TYPES = {"text/javascript", "text/css", "text/html", "application/json",
                 "image/svg+xml"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # 推理引擎是可选能力：依赖缺失/配置不对时只记日志，不能拖垮整个工作区启动
    from .services import engine as engine_service

    info = engine_service.bootstrap()
    worker = None
    if not info.get("available"):
        logging.getLogger("teamflow.engine").info(
            "推理引擎未启用：%s", info.get("reason") or "未知原因"
        )
    else:
        # 作业 worker 跟着引擎一起起；TEAMFLOW_INFERENCE_WORKER=0 时保持关闭，
        # 测试里用手动 drain 驱动，避免后台线程与用例抢同一个库。
        from .services.inference.worker import start_worker, stop_worker

        worker = await start_worker()
    try:
        yield
    finally:
        if worker is not None:
            from .services.inference.worker import stop_worker

            await stop_worker()
        # 关闭所有端侧长连接，避免热重载时留下半开连接
        from .services.edge.gateway import registry

        await registry.close_all(reason="服务关闭")


app = FastAPI(
    title=APP_NAME,
    version=APP_VERSION,
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url=None,
    openapi_url="/api/openapi.json",
)

app.add_middleware(WriteGuardMiddleware)
# 后加的中间件在更外层：压缩包住写保护，错误 JSON 也能被压缩
app.add_middleware(CompressionMiddleware)

# 安全响应头：脚本只允许同源外链（前端没有内联脚本，CSP 里不需要 unsafe-inline
# 的 script-src）；样式里还有大量行内 style 属性，所以 style-src 得放开。
def _csp_value() -> str:
    return (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; "
        "media-src 'self' blob:; "
        "font-src 'self'; "
        "connect-src 'self'; "
        "frame-src 'self'; "
        "frame-ancestors 'self'; "
        "base-uri 'none'; "
        "form-action 'self'; "
        "object-src 'none'"
    )


_CSP_CACHE: list[str] = []
PERMISSIONS_POLICY = (
    "fullscreen=(self), geolocation=(), payment=(), usb=(), camera=(), microphone=()"
)
# Swagger UI 是从 CDN 拉的静态页，还带一段内联启动脚本；严格 CSP 会让它整页白屏。
# 只对接口文档放开，应用本身的 CSP 保持严格。
DOCS_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "img-src 'self' data: https://fastapi.tiangolo.com; "
    "font-src 'self' https://cdn.jsdelivr.net data:; "
    "connect-src 'self'; "
    "frame-ancestors 'self'; "
    "base-uri 'none'; "
    "object-src 'none'"
)
HSTS_VALUE = "max-age=31536000; includeSubDomains"


def _is_docs(path: str) -> bool:
    return path.startswith("/api/docs")


def content_security_policy() -> str:
    """算一次就缓存；存储源站不会在进程内变化。"""
    if not _CSP_CACHE:
        _CSP_CACHE.append(_csp_value())
    return _CSP_CACHE[0]


@app.middleware("http")
async def common_headers(request: Request, call_next):
    response = await call_next(request)
    headers = response.headers
    headers.setdefault("X-Content-Type-Options", "nosniff")
    headers.setdefault("Referrer-Policy", "same-origin")
    # SAMEORIGIN 而不是 DENY：允许页面自己嵌入自己，同时挡住别的站点
    headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    policy = DOCS_CSP if _is_docs(request.url.path) else content_security_policy()
    headers.setdefault("Content-Security-Policy", policy)
    headers.setdefault("Permissions-Policy", PERMISSIONS_POLICY)
    # 只在真的走 HTTPS 时下发 HSTS，否则本地 http 调试会把自己锁在 https 上。
    # 反向代理若错误地固定发送 X-Forwarded-Proto: https，可用 TEAMFLOW_HSTS=0 关掉。
    if is_https(request) and hsts_enabled():
        headers.setdefault("Strict-Transport-Security", HSTS_VALUE)
    if request.url.path.startswith("/api"):
        headers.setdefault("Cache-Control", "no-store")
    return response


for router in routers:
    app.include_router(router)


@app.exception_handler(ApiError)
async def handle_api_error(request: Request, exc: ApiError) -> JSONResponse:
    payload = {"error": exc.message}
    if exc.code:
        payload["code"] = exc.code
    return JSONResponse(payload, status_code=exc.status)


@app.exception_handler(Exception)
async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
    import logging

    logging.getLogger("teamflow").exception("未处理的异常：%s %s", request.method,
                                            request.url.path)
    return JSONResponse({"error": "服务器内部错误"}, status_code=500)


# ---------------------------------------------------------------- 静态资源

def _static_path(rel: str) -> str | None:
    """把 URL 路径映射到 static/ 下的真实文件，越界或不存在返回 None。"""
    base = os.path.normpath(STATIC_DIR)
    full = os.path.normpath(os.path.join(base, rel))
    if full != base and not full.startswith(base + os.sep):
        return None
    if os.path.isdir(full) or not os.path.isfile(full):
        return None
    return full


@app.api_route("/{path:path}", methods=["GET", "HEAD"], include_in_schema=False)
def static_files(path: str, request: Request) -> Response:
    if path.startswith("api/"):
        return JSONResponse({"error": "接口不存在", "path": "/" + path}, status_code=404)

    rel = path or INDEX_FILE
    full = _static_path(rel)
    if full is None:
        # 前端使用 History 路由（/default/issues/12）：路径交给 index.html，
        # 但带扩展名的请求仍视为缺失的静态文件。
        if "." in (path or "").rsplit("/", 1)[-1]:
            return JSONResponse({"error": "文件不存在", "path": "/" + path}, status_code=404)
        full = _static_path(INDEX_FILE)
    if full is None:
        return JSONResponse({"error": "文件不存在", "path": "/" + path}, status_code=404)

    stat = os.stat(full)
    etag = f'"{int(stat.st_mtime)}-{stat.st_size}"'
    head_only = request.method == "HEAD"
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "no-cache"})
    ext = os.path.splitext(full)[1].lower()
    media_type = MEDIA_TYPES.get(ext) or mimetypes.guess_type(full)[0] \
        or "application/octet-stream"
    if media_type in CHARSET_TYPES:
        media_type += "; charset=utf-8"
    with open(full, "rb") as handle:
        body = handle.read()
    return Response(
        content=b"" if head_only else body,
        media_type=media_type,
        # HEAD 只回头，但 Content-Length 要如实反映资源大小
        headers={
            "ETag": etag,
            "Cache-Control": "no-cache",
            **({"Content-Length": str(len(body))} if head_only else {}),
        },
    )
