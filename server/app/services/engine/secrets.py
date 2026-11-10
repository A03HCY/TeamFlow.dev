"""密钥的加解密（模型 API Key、GitHub/Jira Token）。

优先用 ``cryptography`` 的 Fernet；没装或没配 ``TEAMFLOW_SECRET_KEY`` 时降级为
``plain:`` 前缀的明文存储 —— 这样开发环境不用折腾，但部署检查会提示补依赖。

存储格式：

    fernet:<urlsafe-base64 密文>
    plain:<原文>

解密对两种都认，因此「先明文、后加密」的升级路径是平滑的。
"""
from __future__ import annotations

import base64
import hashlib
import os

from .errors import EngineNotConfigured

FERNET_PREFIX = "fernet:"
PLAIN_PREFIX = "plain:"


def _fernet():  # noqa: ANN202 - 返回 cryptography.fernet.Fernet | None
    try:
        from cryptography.fernet import Fernet  # 延迟导入：cryptography 是可选依赖
    except ModuleNotFoundError:  # pragma: no cover - 取决于部署环境
        return None
    material = (os.environ.get("TEAMFLOW_SECRET_KEY") or "").strip()
    if not material:
        return None
    key = base64.urlsafe_b64encode(hashlib.sha256(material.encode("utf-8")).digest())
    return Fernet(key)


def encryption_available() -> bool:
    """是否能真正加密（装了 cryptography 且配了 TEAMFLOW_SECRET_KEY）。"""
    return _fernet() is not None


def encrypt_secret(plain: str | None) -> str:
    """把明文密钥变成可存库的字符串；空值仍是空串。"""
    text = (plain or "").strip()
    if not text:
        return ""
    box = _fernet()
    if box is None:
        return PLAIN_PREFIX + text
    return FERNET_PREFIX + box.encrypt(text.encode("utf-8")).decode("ascii")


def decrypt_secret(stored: str | None) -> str:
    """还原密钥；密文但缺少解密条件时抛 EngineNotConfigured。"""
    text = (stored or "").strip()
    if not text:
        return ""
    if text.startswith(PLAIN_PREFIX):
        return text[len(PLAIN_PREFIX):]
    if text.startswith(FERNET_PREFIX):
        box = _fernet()
        if box is None:
            raise EngineNotConfigured(
                "密钥是加密存储的，但当前进程缺少 TEAMFLOW_SECRET_KEY 或 cryptography"
            )
        from cryptography.fernet import InvalidToken  # 局部导入，避免可选依赖泄漏

        try:
            return box.decrypt(text[len(FERNET_PREFIX):].encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            raise EngineNotConfigured("密钥解密失败：TEAMFLOW_SECRET_KEY 与加密时不一致") from exc
    # 兼容历史/手工写入的裸值
    return text


def secret_state(stored: str | None) -> str:
    """「未配置」/「已配置（加密）」/「已配置（明文）」，给前端展示用。"""
    text = (stored or "").strip()
    if not text:
        return "未配置"
    if text.startswith(FERNET_PREFIX):
        return "已配置（加密）"
    return "已配置（明文）"


def mask_secret(plain: str | None, *, keep: int = 4) -> str:
    """把明文密钥打码，形如 ``sk-a***9f2c``（长度不足时全打码）。"""
    text = (plain or "").strip()
    if not text:
        return ""
    if len(text) <= keep * 2:
        return "*" * len(text)
    return f"{text[:keep]}{'*' * 4}{text[-keep:]}"


__all__ = [
    "FERNET_PREFIX",
    "PLAIN_PREFIX",
    "decrypt_secret",
    "encrypt_secret",
    "encryption_available",
    "mask_secret",
    "secret_state",
]
