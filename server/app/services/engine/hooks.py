"""把 orchify 事件桥接成 TeamFlow 自己的结构。

为什么要这层：

* orchify 的 hook 默认在**编排循环线程里同步执行**，慢 hook 会直接卡住 run，
  所以统一用 ``background=True`` 注册；
* hook 抛异常会污染编排流程（甚至让 run 失败），所以这里统一 try/except 兜住，
  只记日志；
* 事件对象是 dataclass，字段名与我们的 JSON 契约不同（``event_type`` / ``turn`` /
  ``payload``），这里归一成 ``{"type", "run_id", "step", "detail", "at"}``；
* 注册是进程级全局的，必须能精确卸载、可枚举（``describe()``），否则测试之间会互相污染。

用法::

    binding = install_hook("run:approval:required", on_approval)
    ...
    uninstall_hook(binding)
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from .errors import EngineError
from .llm import orchify_module

log = logging.getLogger("teamflow.engine.hooks")

# 我们真正关心的事件（其余保持不订阅，减少无谓唤醒）
USEFUL_EVENTS = (
    "agent:start",
    "agent:finish",
    "agent:abort",
    "agent:call",
    "run:start",
    "run:finish",
    "run:paused",
    "run:resumed",
    "run:approval:required",
    "run:approval:resolved",
    "run:budget:exceeded",
    "run:call:blocked",
)

_LOCK = threading.Lock()
# id(binding) -> 元信息；用 id 而不是 binding 本身做 key 是为了兼容不可哈希对象
_BINDINGS: dict[int, dict] = {}


def known_event_types() -> frozenset[str]:
    """orchify 声明支持的全部事件名（校验用）。"""
    orchify_module()
    from orchify.event import EVENT_TYPES

    return frozenset(EVENT_TYPES)


@dataclass(frozen=True)
class HookEvent:
    """归一后的事件视图。"""

    type: str
    run_id: str
    step: int
    agent_name: str
    detail: dict
    at: float

    def as_dict(self) -> dict:
        return {
            "type": self.type,
            "run_id": self.run_id,
            "step": self.step,
            "agent_name": self.agent_name,
            "detail": self.detail,
            "at": self.at,
        }


def normalize(event: Any) -> HookEvent:
    """把 orchify 事件对象（或已经是 dict 的东西）变成 ``HookEvent``。"""
    if isinstance(event, HookEvent):
        return event
    if isinstance(event, dict):
        return HookEvent(
            type=str(event.get("type") or event.get("event_type") or ""),
            run_id=str(event.get("run_id") or ""),
            step=int(event.get("step") or event.get("turn") or 0),
            agent_name=str(event.get("agent_name") or ""),
            detail=dict(event.get("detail") or event.get("payload") or {}),
            at=float(event.get("at") or event.get("created_at") or time.time()),
        )
    detail = getattr(event, "payload", None) or {}
    return HookEvent(
        type=str(getattr(event, "event_type", "") or ""),
        run_id=str(getattr(event, "run_id", "") or ""),
        step=int(getattr(event, "turn", 0) or 0),
        agent_name=str(getattr(event, "agent_name", "") or ""),
        detail=dict(detail) if isinstance(detail, dict) else {"value": detail},
        at=float(getattr(event, "created_at", 0.0) or time.time()),
    )


def _guard(
    listener: Callable[[HookEvent], Any],
    raw_listener: Callable[[Any], Any] | None = None,
) -> Callable[[Any], None]:
    """包一层：异常不外抛，事件归一后再交给业务回调。

    ``raw_listener`` 拿到 orchify 的原始事件对象（给 ``EventStore.record`` 这类
    需要原始字段的消费者用），同样被异常兜住。
    """

    def _wrapped(event: Any) -> None:
        if raw_listener is not None:
            try:
                raw_listener(event)
            except Exception:  # noqa: BLE001 - 落盘失败不影响编排
                log.exception("engine hook 原始事件处理失败：%s", getattr(event, "event_type", event))
        try:
            listener(normalize(event))
        except Exception:  # noqa: BLE001 - hook 失败不能影响编排
            log.exception("engine hook 处理失败：%s", getattr(event, "event_type", event))

    _wrapped.__name__ = f"tf_hook_{getattr(listener, '__name__', 'listener')}"
    return _wrapped


def install_hook(
    event_type: str | list[str] | tuple[str, ...],
    listener: Callable[[HookEvent], Any],
    *,
    run_id: str | None = None,
    agent_name: str | None = None,
    priority: int = 0,
    background: bool = True,
    raw_listener: Callable[[Any], Any] | None = None,
) -> Any:
    """注册一个受管 hook，返回可用于 ``uninstall_hook`` 的绑定。"""
    orchify_module()
    if not callable(listener):
        raise EngineError("hook 必须是可调用对象")
    from orchify import on

    binding = on(
        event_type,
        _guard(listener, raw_listener),
        run_id=run_id,
        agent_name=agent_name,
        priority=priority,
        background=background,
        owner="teamflow.engine",
    )
    with _LOCK:
        _BINDINGS[id(binding)] = {
            "event_type": event_type,
            "run_id": run_id,
            "agent_name": agent_name,
            "priority": priority,
            "background": background,
            "binding": binding,
        }
    return binding


def uninstall_hook(binding: Any) -> bool:
    """按绑定卸载；未知绑定返回 False。"""
    if binding is None:
        return False
    try:
        from orchify import off

        removed = bool(off(binding))
    except Exception:  # noqa: BLE001 - 已经释放过或 orchify 不可用
        removed = False
    with _LOCK:
        _BINDINGS.pop(id(binding), None)
    return removed


def clear_hooks() -> int:
    """卸载本模块注册的全部 hook（测试收尾用），返回卸载数量。"""
    with _LOCK:
        items = list(_BINDINGS.values())
        _BINDINGS.clear()
    count = 0
    for item in items:
        if uninstall_hook(item["binding"]):
            count += 1
    return count


def describe() -> list[dict]:
    """当前注册的 hook 概况（用于 /api/engine 状态与排障）。"""
    with _LOCK:
        return [
            {
                "event_type": item["event_type"],
                "run_id": item["run_id"],
                "agent_name": item["agent_name"],
                "priority": item["priority"],
                "background": item["background"],
                "count": getattr(item["binding"], "count", 0),
                "disposed": bool(getattr(item["binding"], "disposed", False)),
            }
            for item in _BINDINGS.values()
        ]


def install_run_trace(store, sink: Callable[[HookEvent], Any]) -> Any:
    """把一次 run 的关键事件同时写进 orchify ``EventStore`` 并转给业务 sink。

    P2 的作业执行器用它：事件既落 JSONL（可回放），又推进 ``insight_events``。
    """
    return install_hook(
        list(USEFUL_EVENTS),
        sink,
        raw_listener=getattr(store, "record", None),
    )


__all__ = [
    "HookEvent",
    "USEFUL_EVENTS",
    "clear_hooks",
    "describe",
    "install_hook",
    "install_run_trace",
    "known_event_types",
    "normalize",
    "uninstall_hook",
]
