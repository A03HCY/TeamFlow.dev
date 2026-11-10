"""口令哈希与会话令牌。

格式与旧版一致：``pbkdf2$<salt>$<digest>``，PBKDF2-HMAC-SHA256 迭代 18 万次。
"""
from __future__ import annotations

import hashlib
import secrets

from .config import COOKIE_SECURE_MODE, PASSWORD_ITERATIONS

SID_COOKIE = "sid"
EDIT_COOKIE = "edit"


def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), salt.encode(), PASSWORD_ITERATIONS
    ).hex()
    return f"pbkdf2${salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _algo, salt, _digest = stored.split("$", 2)
    except (AttributeError, ValueError):
        return False
    return secrets.compare_digest(hash_password(password, salt), stored)


def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


# ------------------------------------------------------------------ 部署相关

def is_https(request) -> bool:
    """判断「浏览器侧」是不是 HTTPS。

    直接跑 uvicorn 时看请求协议；放在 nginx / Caddy 后面时看
    ``X-Forwarded-Proto``（uvicorn 开 ``--proxy-headers`` 也会改写 scheme）。
    """
    forwarded = (request.headers.get("x-forwarded-proto") or "").split(",")[0]
    forwarded = forwarded.strip().lower()
    if forwarded:
        return forwarded == "https"
    return request.url.scheme == "https"


def cookie_secure(request) -> bool:
    """Cookie 的 ``Secure`` 标记。

    ``TEAMFLOW_COOKIE_SECURE=1/0`` 可以强制打开/关闭；留空时跟随协议自动判断，
    这样本地 http 调试不会因为 Secure 而丢 Cookie。
    """
    mode = COOKIE_SECURE_MODE
    if mode in ("1", "true", "yes", "on"):
        return True
    if mode in ("0", "false", "no", "off"):
        return False
    return is_https(request)


def client_ip(request) -> str:
    """限流用的来源标识：反代后取 ``X-Forwarded-For`` 的第一跳。"""
    forwarded = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    if forwarded:
        return forwarded
    client = getattr(request, "client", None)
    return getattr(client, "host", "") or "unknown"
