"""引擎层的错误类型。

统一从 ``EngineError`` 继承，方便 ``routers`` 里一个 ``except EngineError``
就能映射成 503/400，而不是把 orchify 的异常类型漏到 HTTP 层。
"""
from __future__ import annotations


class EngineError(RuntimeError):
    """引擎层通用错误（默认按 500 处理）。"""

    status = 500
    code = "engine_error"

    def __init__(self, message: str, **extra):
        super().__init__(message)
        self.message = message
        self.extra = extra


class EngineUnavailable(EngineError):
    """orchify 未安装或进程级开关关闭 → 503。"""

    status = 503
    code = "engine_unavailable"


class EngineNotConfigured(EngineError):
    """团队还没配置 base_url / api_key / model → 400。"""

    status = 400
    code = "engine_not_configured"


class EngineDisabled(EngineError):
    """团队级开关打开但未启用引擎 → 409。"""

    status = 409
    code = "engine_disabled"


class EventRejected(EngineError):
    """端侧事件包不合规（schema/大小/脱敏检查）→ 400。"""

    status = 400
    code = "event_rejected"


__all__ = [
    "EngineDisabled",
    "EngineError",
    "EngineNotConfigured",
    "EngineUnavailable",
    "EventRejected",
]
