"""推理引擎适配层（P0 地基）。

对外只暴露这一层，路由与流水线都不直接 ``import orchify``：

    from ..services import engine
    cfg = engine.load_engine_config(session, team_id)
    agents = engine.build_agents(cfg)

分层：

* ``config``     团队配置读写（含密钥解密）
* ``llm``        OpenAI 兼容后端、探活
* ``agents``     四个角色的 Agent 构造
* ``structured`` JSON Schema + 异步结构化输出 + 防幻觉闸门
* ``hooks``      orchify 事件 → 自研回调（可卸载、background）
* ``persist``    事件/检查点/run 索引的落盘位置
* ``budget``     作业预算切分与超限解析
* ``secrets``    密钥加解密
* ``errors``     统一错误类型
"""
from __future__ import annotations

import os

from ...config import engine_enabled
from .agents import EngineAgents, build_agent, build_agents
from .budget import JobBudget, job_budget, parse_exceeded
from .config import (
    EngineConfig,
    ensure_team_engine,
    load_engine_config,
    save_engine_config,
)
from .errors import (
    EngineDisabled,
    EngineError,
    EngineNotConfigured,
    EngineUnavailable,
    EventRejected,
)
from .hooks import (
    HookEvent,
    clear_hooks,
    describe as describe_hooks,
    install_hook,
    install_run_trace,
    known_event_types,
    uninstall_hook,
)
from .llm import aprobe, build_embeddings, build_llm, orchify_module
from .persist import (
    configure_orchify_env,
    engine_paths,
    ensure_dirs,
    make_checkpoint_store,
    make_event_store,
    make_run_store,
    reset_caches,
)
from .secrets import decrypt_secret, encrypt_secret, encryption_available, secret_state
from .structured import (
    ANALYSIS_SCHEMA,
    COORDINATE_SCHEMA,
    PREPROCESS_SCHEMA,
    REPORT_SCHEMA,
    SCHEMAS,
    aask_structured,
    check_analysis,
    clamp_analysis,
    looks_like_injection,
    require_valid,
)

__all__ = [
    "ANALYSIS_SCHEMA",
    "COORDINATE_SCHEMA",
    "PREPROCESS_SCHEMA",
    "REPORT_SCHEMA",
    "SCHEMAS",
    "EngineAgents",
    "EngineConfig",
    "EngineDisabled",
    "EngineError",
    "EngineNotConfigured",
    "EngineUnavailable",
    "EventRejected",
    "HookEvent",
    "JobBudget",
    "aask_structured",
    "aprobe",
    "available",
    "build_agent",
    "build_agents",
    "build_embeddings",
    "build_llm",
    "bootstrap",
    "check_analysis",
    "clamp_analysis",
    "clear_hooks",
    "configure_orchify_env",
    "decrypt_secret",
    "describe_hooks",
    "encrypt_secret",
    "encryption_available",
    "engine_paths",
    "ensure_dirs",
    "ensure_team_engine",
    "install_hook",
    "install_run_trace",
    "job_budget",
    "known_event_types",
    "load_engine_config",
    "looks_like_injection",
    "make_checkpoint_store",
    "make_event_store",
    "make_run_store",
    "orchify_module",
    "parse_exceeded",
    "require_valid",
    "reset_caches",
    "save_engine_config",
    "secret_state",
    "uninstall_hook",
]


def available() -> tuple[bool, str]:
    """引擎是否可用；不可用时给出人话原因（给 /api/engine 状态用）。"""
    if not engine_enabled():
        return False, "引擎已被 TEAMFLOW_ENGINE=0 关闭"
    try:
        orchify_module()
    except EngineUnavailable as exc:
        return False, str(exc)
    return True, ""


def bootstrap() -> dict:
    """应用启动时调用：补齐环境变量、建目录。失败只记录，不阻塞启动。"""
    import logging

    log = logging.getLogger("teamflow.engine")
    ok, reason = available()
    if not ok:
        return {"available": False, "reason": reason}
    try:
        applied = configure_orchify_env()
    except Exception as exc:  # noqa: BLE001 - 引擎是可选能力，启动不能被它拖垮
        log.warning("推理引擎初始化失败：%s", exc)
        return {"available": False, "reason": str(exc)}
    return {
        "available": True,
        "reason": "",
        "paths": engine_paths().as_dict(),
        "orchify_env": applied,
        "encryption": encryption_available(),
        "approval_timeout": int(os.environ.get("ORCHIFY_APPROVAL_TIMEOUT", "0") or 0),
    }
