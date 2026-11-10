"""团队级引擎配置的读写。

配置来源优先级（从高到低）：

1. 环境变量 ``TEAMFLOW_ENGINE_BASE_URL`` / ``_API_KEY`` / ``_MODEL``（运维兜底）；
2. ``team_engine`` 表（前端「设置 → 推理引擎」写入）；
3. 全局 ``settings`` 表里的 ``ai_base_url`` / ``ai_api_key`` / ``ai_model``
   （沿用此前「AI 助手」的配置，避免用户重复填一遍）。

密钥在库里是密文（见 ``secrets.py``），``describe()`` 永远只给出打码后的状态。
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from sqlalchemy.orm import Session

from ...config import (
    ENGINE_APPROVAL_TIMEOUT,
    ENGINE_BUDGET_SECONDS,
    ENGINE_BUDGET_TOKENS,
    ENGINE_BUDGET_USD,
    ENGINE_CONFIDENCE_THRESHOLD,
    ENGINE_DEFAULT_EMBEDDING,
)
from ...db.engine_models import TeamEngine
from ...db.base import utcnow_iso
from ...errors import ApiError
from ..settings import get_setting
from .errors import EngineDisabled
from .secrets import decrypt_secret, encrypt_secret, mask_secret, secret_state

MAX_BASE_URL = 300
MAX_MODEL = 120


@dataclass(frozen=True)
class EngineConfig:
    """一次推理需要的全部参数（冻结，避免流水线中途被改）。"""

    team_id: int
    enabled: bool
    base_url: str
    api_key: str
    model: str
    embedding_model: str
    confidence_threshold: float
    approval_timeout: int
    budget_usd: float
    budget_tokens: int
    budget_seconds: float
    dry_run: bool
    auto_writeback: bool
    source: str = "team"          # team | settings | env

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_key and self.model)

    def require_configured(self) -> "EngineConfig":
        from .errors import EngineNotConfigured

        if not self.configured:
            raise EngineNotConfigured(
                "尚未配置推理模型：请在「设置 → 推理引擎」填写接口地址、模型与 API Key"
            )
        return self

    def require_enabled(self) -> "EngineConfig":
        if not self.enabled:
            raise EngineDisabled("该团队未启用推理引擎")
        return self

    def describe(self) -> dict:
        """给前端的配置视图（密钥只给打码与状态）。"""
        return {
            "enabled": self.enabled,
            "base_url": self.base_url,
            "model": self.model,
            "api_key": mask_secret(self.api_key),
            "api_key_state": secret_state(encrypt_secret(self.api_key) if self.api_key else ""),
            "configured": self.configured,
            "embedding_model": self.embedding_model,
            "confidence_threshold": self.confidence_threshold,
            "approval_timeout": self.approval_timeout,
            "budget_usd": self.budget_usd,
            "budget_tokens": self.budget_tokens,
            "budget_seconds": self.budget_seconds,
            "dry_run": self.dry_run,
            "auto_writeback": self.auto_writeback,
            "source": self.source,
        }


# ------------------------------------------------------------------ 读写

def ensure_team_engine(session: Session, team_id: int) -> TeamEngine:
    """取团队配置行，没有就按默认值建（幂等）。"""
    row = session.get(TeamEngine, team_id)
    if row is None:
        row = TeamEngine(
            team_id=team_id,
            enabled=0,
            base_url="",
            model="",
            api_key_enc="",
            embedding_model=ENGINE_DEFAULT_EMBEDDING,
            confidence_threshold=ENGINE_CONFIDENCE_THRESHOLD,
            approval_timeout=ENGINE_APPROVAL_TIMEOUT,
            budget_usd=ENGINE_BUDGET_USD,
            budget_tokens=ENGINE_BUDGET_TOKENS,
            budget_seconds=ENGINE_BUDGET_SECONDS,
            dry_run=1,
            auto_writeback=0,
            updated_at=utcnow_iso(),
        )
        session.add(row)
        session.flush()
    return row


def _env_override() -> dict:
    return {
        "base_url": (os.environ.get("TEAMFLOW_ENGINE_BASE_URL") or "").strip(),
        "api_key": (os.environ.get("TEAMFLOW_ENGINE_API_KEY") or "").strip(),
        "model": (os.environ.get("TEAMFLOW_ENGINE_MODEL") or "").strip(),
    }


def load_engine_config(session: Session, team_id: int) -> EngineConfig:
    """组装 ``EngineConfig``：团队行 → 全局 settings → 环境变量。"""
    row = ensure_team_engine(session, team_id)
    source = "team"

    base_url = (row.base_url or "").strip()
    model = (row.model or "").strip()
    api_key = decrypt_secret(row.api_key_enc) if row.api_key_enc else ""

    if not base_url:
        base_url = str(get_setting(session, "ai_base_url", "") or "").strip()
        if base_url:
            source = "settings"
    if not model:
        model = str(get_setting(session, "ai_model", "") or "").strip()
    if not api_key:
        api_key = str(get_setting(session, "ai_api_key", "") or "").strip()

    override = _env_override()
    if override["base_url"]:
        base_url, source = override["base_url"], "env"
    if override["api_key"]:
        api_key, source = override["api_key"], "env"
    if override["model"]:
        model, source = override["model"], "env"

    return EngineConfig(
        team_id=team_id,
        enabled=bool(row.enabled),
        base_url=base_url,
        api_key=api_key,
        model=model,
        embedding_model=(row.embedding_model or ENGINE_DEFAULT_EMBEDDING).strip(),
        confidence_threshold=float(row.confidence_threshold),
        approval_timeout=int(row.approval_timeout),
        budget_usd=float(row.budget_usd),
        budget_tokens=int(row.budget_tokens),
        budget_seconds=float(row.budget_seconds),
        dry_run=bool(row.dry_run),
        auto_writeback=bool(row.auto_writeback),
        source=source,
    )


def _clamp(value, low, high, field: str, *, as_type=float):  # noqa: ANN001
    try:
        number = as_type(value)
    except (TypeError, ValueError) as exc:
        raise ApiError(f"{field} 必须是数字") from exc
    if number < low or number > high:
        raise ApiError(f"{field} 必须在 {low} ~ {high} 之间")
    return number


def _clean_url(value: str) -> str:
    text = (value or "").strip().rstrip("/")
    if not text:
        return ""
    if not text.startswith(("http://", "https://")):
        raise ApiError("接口地址必须以 http:// 或 https:// 开头")
    if len(text) > MAX_BASE_URL:
        raise ApiError(f"接口地址不能超过 {MAX_BASE_URL} 个字符")
    return text


def save_engine_config(session: Session, team_id: int, data: dict) -> EngineConfig:
    """按字段增量更新团队引擎配置（未出现的字段保持原值）。"""
    row = ensure_team_engine(session, team_id)

    if "enabled" in data:
        row.enabled = 1 if data.get("enabled") else 0
    if "base_url" in data:
        row.base_url = _clean_url(str(data.get("base_url") or ""))
    if "model" in data:
        model = str(data.get("model") or "").strip()
        if len(model) > MAX_MODEL:
            raise ApiError(f"模型名不能超过 {MAX_MODEL} 个字符")
        row.model = model
    if data.get("clear_api_key"):
        row.api_key_enc = ""
    elif "api_key" in data:
        plain = str(data.get("api_key") or "").strip()
        if plain:
            row.api_key_enc = encrypt_secret(plain)
    if "embedding_model" in data:
        row.embedding_model = str(data.get("embedding_model") or ENGINE_DEFAULT_EMBEDDING).strip()
    if "confidence_threshold" in data:
        row.confidence_threshold = _clamp(
            data.get("confidence_threshold"), 0.0, 1.0, "置信度阈值"
        )
    if "approval_timeout" in data:
        row.approval_timeout = _clamp(
            data.get("approval_timeout"), 30, 86_400, "审批超时", as_type=int
        )
    if "budget_usd" in data:
        row.budget_usd = _clamp(data.get("budget_usd"), 0.0, 10.0, "单次预算（美元）")
    if "budget_tokens" in data:
        row.budget_tokens = _clamp(
            data.get("budget_tokens"), 1_000, 2_000_000, "单次预算（tokens）", as_type=int
        )
    if "budget_seconds" in data:
        row.budget_seconds = _clamp(
            data.get("budget_seconds"), 10, 3_600, "单次超时（秒）"
        )
    if "dry_run" in data:
        row.dry_run = 1 if data.get("dry_run") else 0
    if "auto_writeback" in data:
        row.auto_writeback = 1 if data.get("auto_writeback") else 0

    row.updated_at = utcnow_iso()
    session.flush()
    return load_engine_config(session, team_id)


__all__ = [
    "EngineConfig",
    "ensure_team_engine",
    "load_engine_config",
    "save_engine_config",
]
