"""引擎运行数据的落盘位置与 orchify 持久化对象。

目录布局（默认 ``server/data/engine``，可用 ``TEAMFLOW_ENGINE_DIR`` 改）::

    data/engine/
    ├─ events/       # 每个 run 一个 JSONL（EventStore）
    ├─ checkpoints/  # 步级检查点（CheckpointStore）
    ├─ runs.db       # run 索引（SQLiteRunStore）
    └─ vectors/      # 每个团队的议题向量（VectorIndex 的 JSONL）

「不落盘」指的是**原始 diff 不落盘**：上报的原文只用于一次推理，落库的是脱敏后的
特征与结论；这里的事件 JSONL 记录的是编排过程（事件名、步号、摘要），不是代码内容。
"""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from pathlib import Path

from ...config import engine_data_dir
from .llm import orchify_module

_LOCK = threading.Lock()
_RUN_STORE = None
_EVENT_STORE = None
_CHECKPOINT_STORE = None


@dataclass(frozen=True)
class EnginePaths:
    root: Path
    events: Path
    checkpoints: Path
    vectors: Path

    @property
    def runs_db(self) -> Path:
        return self.root / "runs.db"

    def as_dict(self) -> dict:
        return {
            "root": str(self.root),
            "events": str(self.events),
            "checkpoints": str(self.checkpoints),
            "vectors": str(self.vectors),
            "runs_db": str(self.runs_db),
        }


def engine_paths() -> EnginePaths:
    root = engine_data_dir()
    return EnginePaths(
        root=root,
        events=root / "events",
        checkpoints=root / "checkpoints",
        vectors=root / "vectors",
    )


def ensure_dirs() -> EnginePaths:
    """建好全部子目录（幂等），返回路径。"""
    paths = engine_paths()
    for directory in (paths.events, paths.checkpoints, paths.vectors):
        directory.mkdir(parents=True, exist_ok=True)
    return paths


def configure_orchify_env() -> dict:
    """给 orchify 的进程级配置补默认值（不覆盖用户已设的值）。

    关键项：
    * ``ORCHIFY_APPROVAL_TIMEOUT``：orchify 默认 None=永久等待，这里给出有限值，
      保证审批超时后 fail-closed；
    * ``ORCHIFY_RUN_STORE``：run 索引落到 ``runs.db``，重启后仍可查；
    * ``ORCHIFY_LOG_LEVEL``：默认 INFO，避免 DEBUG 淹没服务日志。
    """
    from ...config import ENGINE_APPROVAL_TIMEOUT

    paths = ensure_dirs()
    defaults = {
        "ORCHIFY_APPROVAL_TIMEOUT": str(ENGINE_APPROVAL_TIMEOUT),
        "ORCHIFY_RUN_STORE": f"sqlite:///{paths.runs_db}",
        "ORCHIFY_LOG_LEVEL": "INFO",
    }
    applied: dict = {}
    for key, value in defaults.items():
        if not os.environ.get(key):
            os.environ[key] = value
            applied[key] = value
        else:
            applied[key] = os.environ[key]
    return applied


# ------------------------------------------------------------------ 持久化对象

def make_event_store():
    """进程内共享的 ``EventStore``（每 run 一个 JSONL）。"""
    global _EVENT_STORE
    with _LOCK:
        if _EVENT_STORE is None:
            orchify_module()
            from orchify.persistence import EventStore

            _EVENT_STORE = EventStore(str(ensure_dirs().events))
        return _EVENT_STORE


def make_checkpoint_store():
    """进程内共享的 ``CheckpointStore``（崩溃后 ``resume`` 用）。"""
    global _CHECKPOINT_STORE
    with _LOCK:
        if _CHECKPOINT_STORE is None:
            orchify_module()
            from orchify.persistence import CheckpointStore

            _CHECKPOINT_STORE = CheckpointStore(str(ensure_dirs().checkpoints))
        return _CHECKPOINT_STORE


def make_run_store():
    """进程内共享的 run 索引（SQLite，多进程可共享同一文件）。"""
    global _RUN_STORE
    with _LOCK:
        if _RUN_STORE is None:
            orchify_module()
            from orchify.store import SQLiteRunStore

            paths = ensure_dirs()
            _RUN_STORE = SQLiteRunStore(str(paths.runs_db))
        return _RUN_STORE


def reset_caches() -> None:
    """释放缓存的持久化对象（测试收尾 / 换目录后重配）。"""
    global _RUN_STORE, _EVENT_STORE, _CHECKPOINT_STORE
    with _LOCK:
        for store in (_EVENT_STORE, _CHECKPOINT_STORE):
            close = getattr(store, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001
                    pass
        _EVENT_STORE = None
        _CHECKPOINT_STORE = None
        _RUN_STORE = None


__all__ = [
    "EnginePaths",
    "configure_orchify_env",
    "engine_paths",
    "ensure_dirs",
    "make_checkpoint_store",
    "make_event_store",
    "make_run_store",
    "reset_caches",
]
