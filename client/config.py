"""TeamFlow 端侧配置：环境变量优先，其次 ``~/.teamflow/edge.json``。

只是把「连哪台服务端、用哪个配对码、离线队列放哪」集中到一处，探针/托盘/CI
都可以复用；不涉及任何探针逻辑。

优先级（从高到低）：

1. 显式传入的 ``EdgeConfig`` 字段 / CLI 参数；
2. 环境变量 ``TEAMFLOW_EDGE_*``；
3. 配置文件（默认 ``~/.teamflow/edge.json``，可用 ``TEAMFLOW_EDGE_CONFIG`` 指定）；
4. 代码里的默认值。

::

    from client.config import load_config, save_config

    cfg = load_config()                     # 读环境变量 + 配置文件
    cfg.url, cfg.token, cfg.resolved_platform()
    save_config(cfg)                        # 落盘（尽力 chmod 600）
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

from .core.network import (
    DEFAULT_HEARTBEAT,
    DEFAULT_QUEUE_LIMIT,
    DEFAULT_WINDOW,
    platform_name,
)

CONFIG_NAME = "edge.json"
DEFAULT_DIR_NAME = ".teamflow"

# 环境变量 -> 字段（同一个字段可以有多个别名，后者覆盖前者）
ENV_MAP = {
    "TEAMFLOW_EDGE_URL": "url",
    "TEAMFLOW_EDGE_SERVER_URL": "url",
    "TEAMFLOW_EDGE_TOKEN": "token",
    "TEAMFLOW_EDGE_TEAM": "team",
    "TEAMFLOW_EDGE_DEVICE_NAME": "device_name",
    "TEAMFLOW_EDGE_DEVICE": "device_name",
    "TEAMFLOW_EDGE_PLATFORM": "platform",
    "TEAMFLOW_EDGE_AGENT_VERSION": "agent_version",
    "TEAMFLOW_EDGE_SPOOL_DIR": "spool_dir",
    "TEAMFLOW_EDGE_HEARTBEAT": "heartbeat",
    "TEAMFLOW_EDGE_QUEUE_LIMIT": "queue_limit",
    "TEAMFLOW_EDGE_WINDOW": "window",
    "TEAMFLOW_EDGE_VALIDATE": "validate",
}


class ConfigError(Exception):
    """配置缺项或取值不合法。"""


@dataclass
class EdgeConfig:
    """端侧 SDK 的启动参数。"""

    url: str = ""
    token: str = ""
    team: str = ""
    device_name: str = ""
    platform: str = ""
    agent_version: str = ""
    spool_dir: str = ""
    heartbeat: float = DEFAULT_HEARTBEAT
    queue_limit: int = DEFAULT_QUEUE_LIMIT
    window: int = DEFAULT_WINDOW
    validate: bool = True

    # -- 派生值
    def resolved_platform(self) -> str:
        return self.platform or platform_name()

    def device(self) -> dict:
        """hello 帧里的 ``device``：服务端会存成设备名/平台/版本。"""
        payload = {
            "name": self.device_name or self.resolved_platform(),
            "platform": self.resolved_platform(),
        }
        if self.agent_version:
            payload["agent_version"] = self.agent_version
        return payload

    def spool_path(self) -> Path:
        base = Path(self.spool_dir) if self.spool_dir else config_dir()
        return base / "events.jsonl"

    def masked(self) -> dict:
        data = asdict(self)
        data["token"] = mask_token(self.token)
        data["spool_path"] = str(self.spool_path())
        data["platform"] = self.resolved_platform()
        return data

    def missing(self) -> list[str]:
        """还缺哪些必填项（给 CLI / UI 提示用）。"""
        gaps = []
        if not self.url:
            gaps.append("url（TEAMFLOW_EDGE_URL 或 edge.json 的 url）")
        if not self.token:
            gaps.append("token（「引擎 → 生成配对码」里的 tfd_ 串）")
        return gaps

    def ensure_ready(self) -> None:
        gaps = self.missing()
        if gaps:
            raise ConfigError("端侧配置缺少：" + "、".join(gaps))
        scheme = self.url.split("://", 1)[0].lower() if "://" in self.url else ""
        if scheme not in ("ws", "wss"):
            raise ConfigError(f"url 必须是 ws:// 或 wss:// 开头，当前是：{self.url}")
        if not 5 <= float(self.heartbeat) <= 600:
            raise ConfigError("heartbeat 必须在 5–600 秒之间")
        if not 1 <= int(self.queue_limit) <= 10000:
            raise ConfigError("queue_limit 必须在 1–10000 之间")
        if not 1 <= int(self.window) <= 32:
            raise ConfigError("window 必须在 1–32 之间")

    def client_kwargs(self) -> dict:
        """给 ``EdgeClient(**cfg.client_kwargs())`` 用的参数。"""
        return {
            "device": self.device(),
            "agent_version": self.agent_version,
            "spool_path": self.spool_path(),
            "queue_limit": self.queue_limit,
            "heartbeat": self.heartbeat,
            "window": self.window,
            "validate": self.validate,
        }


def config_dir() -> Path:
    base = os.environ.get("TEAMFLOW_EDGE_DIR")
    return Path(base) if base else Path.home() / DEFAULT_DIR_NAME


def config_path(path: str | os.PathLike[str] | None = None) -> Path:
    if path:
        return Path(path)
    explicit = os.environ.get("TEAMFLOW_EDGE_CONFIG")
    return Path(explicit) if explicit else config_dir() / CONFIG_NAME


def _coerce(name: str, value: Any) -> Any:
    if value is None:
        return None
    if name == "heartbeat":
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    if name in ("queue_limit", "window"):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    if name == "validate":
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() not in ("0", "false", "no", "off", "")
    return str(value)


def load_config(
    path: str | os.PathLike[str] | None = None,
    *,
    env: dict | None = None,
) -> EdgeConfig:
    """读配置文件，再用环境变量覆盖；返回的配置不保证完整（用 ``ensure_ready`` 检查）。"""
    env = os.environ if env is None else env
    known = {item.name for item in fields(EdgeConfig)}
    data: dict[str, Any] = {}
    target = config_path(path)
    if target.exists():
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = {}
        if isinstance(raw, dict):
            for key, value in raw.items():
                name = "url" if key in ("server_url", "url") else str(key)
                if name in known:
                    coerced = _coerce(name, value)
                    if coerced is not None:
                        data[name] = coerced
    for variable, name in ENV_MAP.items():
        value = env.get(variable)
        if value is None or value == "" or name not in known:
            continue
        coerced = _coerce(name, value)
        if coerced is not None:
            data[name] = coerced
    return EdgeConfig(**data)


def save_config(cfg: EdgeConfig, path: str | os.PathLike[str] | None = None) -> Path:
    """落盘为 JSON；token 是明文，尽力把权限收紧到 0600。"""
    target = config_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {key: value for key, value in asdict(cfg).items() if value not in ("", None)}
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:  # Windows 上 chmod 基本无效，失败也无所谓
        os.chmod(target, 0o600)
    except OSError:  # pragma: no cover - 平台差异
        pass
    return target


def mask_token(token: str, keep: int = 4) -> str:
    text = str(token or "")
    if not text:
        return ""
    if len(text) <= keep * 2:
        return "*" * len(text)
    return f"{text[:keep]}{'*' * 6}{text[-keep:]}"


def describe(cfg: EdgeConfig) -> str:
    """给 CLI / 托盘显示的一行摘要。"""
    gaps = cfg.missing()
    state = "就绪" if not gaps else f"缺少 {len(gaps)} 项"
    return (
        f"{state} · {cfg.url or '(未设置地址)'} · {mask_token(cfg.token) or '(无配对码)'}"
        f" · 队列 {cfg.spool_path()}（上限 {cfg.queue_limit}）"
        f" · 平台 {cfg.resolved_platform()}"
    )


__all__ = [
    "CONFIG_NAME",
    "ConfigError",
    "EdgeConfig",
    "config_dir",
    "config_path",
    "describe",
    "load_config",
    "mask_token",
    "save_config",
]
