"""进程内滑动窗口限流：给登录这类敏感接口挡住暴力尝试。

实现刻意做得很小：一个 dict + deque，时间用 ``time.monotonic()``，全程加锁。
本项目的会议房间同样是进程内存态，单 worker 部署一致；多 worker / 多实例
部署时把 :func:`allow` 换成 Redis 版本即可，调用方不需要改。
"""
from __future__ import annotations

import threading
import time
from collections import deque

_LOCK = threading.Lock()
_HITS: dict[str, deque[float]] = {}


def allow(key: str, limit: int, window: float) -> tuple[bool, float]:
    """登记一次尝试。

    :returns: ``(是否放行, 还需等待的秒数)``；``limit <= 0`` 表示关闭限流。
    """
    if limit <= 0:
        return True, 0.0
    now = time.monotonic()
    cutoff = now - window
    with _LOCK:
        bucket = _HITS.setdefault(key, deque())
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= limit:
            return False, max(0.0, window - (now - bucket[0]))
        bucket.append(now)
        return True, 0.0


def forget(key: str) -> None:
    """成功后清空计数，避免合法用户被历史失败次数拖累。"""
    with _LOCK:
        _HITS.pop(key, None)


def clear() -> None:
    """清空全部计数（测试用）。"""
    with _LOCK:
        _HITS.clear()


def key_of(ip: str, subject: str) -> str:
    return f"{ip}|{(subject or '').strip().lower()}"
