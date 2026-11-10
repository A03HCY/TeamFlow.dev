"""WS 网关：连接登记、帧协议、下行广播与反向查询（P1）。

设计要点：

* **token 走 hello 帧，不走 URL**：URL 会被 nginx/访问日志记下来，token 等于
  设备口令，不能出现在 query string 里（端侧 SDK 也是这么实现的）。
* **一个设备只保留一条连接**：新连接进来会踢掉旧的（同机重连的常见场景）。
* **反向查询需要请求-响应**：服务端给某台设备发 ``query``，端侧回
  ``query_result``；这里用 ``asyncio.Future`` + request_id 做等待，超时返回 None，
  让上层降级（拿不到文件内容就只用事件包里的元数据）。
* 心跳由端侧驱动（``heartbeat`` 帧），服务端只刷新 ``last_seen_at``；服务端不发
  WS ping，避免与端侧库自带的 ping 打架（SDK 里已 ``ping_interval=None``）。
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from dataclasses import dataclass, field

from fastapi import WebSocket

from ...config import (
    ENGINE_EVENT_MAX_FILES,
    ENGINE_EVENT_RATE_LIMIT,
    ENGINE_MAX_EVENT_BYTES,
    ENGINE_QUERY_TIMEOUT,
)

logger = logging.getLogger("teamflow.edge")

# ---- 帧类型（与 client/core/network.py 的常量保持一致）
FRAME_HELLO = "hello"
FRAME_EVENT = "event"
FRAME_HEARTBEAT = "heartbeat"
FRAME_QUERY = "query"
FRAME_QUERY_RESULT = "query_result"
# ---- 服务端下发
FRAME_HELLO_OK = "hello_ok"
FRAME_ACK = "ack"
FRAME_PAUSE = "pause"
FRAME_RESUME = "resume"
FRAME_PING = "ping"
FRAME_ERROR = "error"

ACK_STORED = "stored"
ACK_DUPLICATE = "duplicate"
ACK_REJECTED = "rejected"

# ---- 反向查询（服务端点名要哪些文件，端侧在沙箱里回答）
QUERY_MODE_STAT = "stat"
QUERY_MODE_READ = "read"
QUERY_MODES = (QUERY_MODE_STAT, QUERY_MODE_READ)
MAX_QUERY_FILES = 20
MAX_QUERY_BYTES = 256 * 1024
DEFAULT_QUERY_BYTES = 64 * 1024

# 关闭码：和协议错误区分开，端侧可据此决定「重试」还是「重新配对」
CLOSE_FORBIDDEN = 4401
CLOSE_REPLACED = 4409
CLOSE_ERROR = 1011


@dataclass
class Connection:
    device_id: int
    team_id: int
    websocket: WebSocket
    device_name: str = ""
    platform: str = ""
    agent_version: str = ""
    paused: bool = False
    sent: int = 0
    received: int = 0
    opened_at: float = field(default_factory=time.time)
    last_frame_at: float = field(default_factory=time.time)
    pending: dict[str, asyncio.Future] = field(default_factory=dict)
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # 每台设备自己的速率窗口（事件条数/分钟）
    window: deque = field(default_factory=deque)

    def as_dict(self) -> dict:
        return {
            "device_id": self.device_id,
            "team_id": self.team_id,
            "name": self.device_name,
            "platform": self.platform,
            "agent_version": self.agent_version,
            "paused": self.paused,
            "sent": self.sent,
            "received": self.received,
            "seconds": round(time.time() - self.opened_at, 1),
        }


class ConnectionRegistry:
    """进程内连接表（单进程部署成立；多进程要换 Redis pub/sub）。"""

    def __init__(self) -> None:
        self._by_device: dict[int, Connection] = {}
        self._counters = {"accepted": 0, "rejected": 0, "replaced": 0, "queries": 0}

    # ------------------------------------------------------------ 注册
    async def register(self, connection: Connection) -> None:
        previous = self._by_device.get(connection.device_id)
        if previous is not None and previous.websocket is not connection.websocket:
            self._counters["replaced"] += 1
            with contextlib.suppress(Exception):
                await previous.websocket.close(code=CLOSE_REPLACED, reason="设备在其他地方重新连接")
        self._by_device[connection.device_id] = connection
        self._counters["accepted"] += 1

    def unregister(self, connection: Connection) -> None:
        current = self._by_device.get(connection.device_id)
        if current is connection:
            self._by_device.pop(connection.device_id, None)

    def get(self, device_id: int) -> Connection | None:
        return self._by_device.get(device_id)

    def online_ids(self, team_id: int | None = None) -> set[int]:
        return {
            device_id
            for device_id, conn in self._by_device.items()
            if team_id is None or conn.team_id == team_id
        }

    def count(self, team_id: int | None = None) -> int:
        return len(self.online_ids(team_id))

    def paused_ids(self, team_id: int | None = None) -> set[int]:
        """当前被管理员暂停上报的设备（前端用它决定显示「暂停」还是「恢复」）。"""
        return {
            device_id
            for device_id, conn in self._by_device.items()
            if conn.paused and (team_id is None or conn.team_id == team_id)
        }

    def snapshot(self, team_id: int | None = None) -> list[dict]:
        return [
            conn.as_dict()
            for conn in list(self._by_device.values())
            if team_id is None or conn.team_id == team_id
        ]

    def stats(self) -> dict:
        return {**self._counters, "online": len(self._by_device)}

    # ------------------------------------------------------------ 发送
    async def send(self, connection: Connection, frame: dict) -> bool:
        """单连接发送；失败返回 False（调用方通常只记日志）。"""
        try:
            async with connection.send_lock:
                await connection.websocket.send_json(frame)
            connection.sent += 1
            return True
        except Exception as exc:  # noqa: BLE001 - 断连原因很多，统一吞掉
            logger.debug("向设备 %s 发送失败：%s", connection.device_id, exc)
            return False

    async def send_to(self, device_id: int, frame: dict) -> bool:
        connection = self._by_device.get(device_id)
        if connection is None:
            return False
        return await self.send(connection, frame)

    async def broadcast(self, team_id: int, frame: dict) -> int:
        """按团队广播（洞察卡片刷新用），返回成功条数。"""
        targets = [
            conn for conn in list(self._by_device.values()) if conn.team_id == team_id
        ]
        results = await asyncio.gather(
            *(self.send(conn, frame) for conn in targets), return_exceptions=True
        )
        return sum(1 for item in results if item is True)

    async def set_paused(self, device_id: int, paused: bool, reason: str = "") -> bool:
        connection = self._by_device.get(device_id)
        if connection is None:
            return False
        connection.paused = bool(paused)
        frame = {
            "type": FRAME_PAUSE if paused else FRAME_RESUME,
            "reason": reason or ("用户暂停了推断" if paused else ""),
            "ts": _now_iso(),
        }
        return await self.send(connection, frame)

    async def close(self, device_id: int, *, code: int = 1000, reason: str = "") -> None:
        connection = self._by_device.pop(device_id, None)
        if connection is None:
            return
        for future in list(connection.pending.values()):
            if not future.done():
                future.cancel()
        with contextlib.suppress(Exception):
            await connection.websocket.close(code=code, reason=reason)

    async def close_all(self, *, code: int = 1000, reason: str = "服务重启") -> None:
        for device_id in list(self._by_device):
            await self.close(device_id, code=code, reason=reason)

    # ------------------------------------------------------------ 限流
    def allow_event(self, connection: Connection, *, limit: int = ENGINE_EVENT_RATE_LIMIT) -> bool:
        """滑动一分钟窗口；超限返回 False（端侧会退避重试）。"""
        now = time.time()
        while connection.window and now - connection.window[0] > 60.0:
            connection.window.popleft()
        if len(connection.window) >= limit:
            return False
        connection.window.append(now)
        return True

    # ------------------------------------------------------------ 反向查询
    async def query(
        self,
        device_id: int,
        files: list[str],
        *,
        mode: str = QUERY_MODE_STAT,
        max_bytes: int = DEFAULT_QUERY_BYTES,
        reason: str = "",
        timeout: float = ENGINE_QUERY_TIMEOUT,
    ) -> dict | None:
        """点名让端侧回报指定文件的元信息（``read`` 模式才附带内容），超时返回 None。

        帧里同时带 ``files`` 与 ``paths``：新 SDK 读 ``files``，老 SDK 读 ``paths``，
        两种客户端都能收到同一次查询。
        """
        connection = self._by_device.get(device_id)
        if connection is None or connection.paused:
            return None
        wanted = [str(item) for item in files if str(item or "").strip()][:MAX_QUERY_FILES]
        if not wanted:
            return None
        normalized_mode = mode if mode in QUERY_MODES else QUERY_MODE_STAT
        trimmed_bytes = max(1024, min(MAX_QUERY_BYTES, int(max_bytes or DEFAULT_QUERY_BYTES)))
        request_id = f"q{connection.device_id}-{int(time.time() * 1000)}-{len(connection.pending)}"
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        connection.pending[request_id] = future
        self._counters["queries"] += 1
        frame = {
            "type": FRAME_QUERY,
            "request_id": request_id,
            "files": wanted,
            "paths": list(wanted),
            "mode": normalized_mode,
            "max_bytes": trimmed_bytes,
            "reason": reason[:200],
        }
        try:
            if not await self.send(connection, frame):
                return None
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            logger.debug("设备 %s 反向查询超时（%s）", device_id, reason)
            return None
        finally:
            connection.pending.pop(request_id, None)

    def resolve_query(self, connection: Connection, request_id: str, result: dict) -> bool:
        future = connection.pending.get(request_id)
        if future is None or future.done():
            return False
        future.set_result(result)
        return True


def _now_iso() -> str:
    from ...db.base import utcnow_iso

    return utcnow_iso()


# 模块级单例：WS 端点与 REST 兜底共用同一张表
registry = ConnectionRegistry()


def hello_ok_frame(
    *, device_id: int, team_id: int, device_name: str, server_time: str, paused: bool
) -> dict:
    return {
        "type": FRAME_HELLO_OK,
        "schema": 1,
        "device_id": device_id,
        "team_id": team_id,
        "device_name": device_name,
        "server_time": server_time,
        "heartbeat": 20,
        "paused": bool(paused),
        "limits": {
            "event_bytes": ENGINE_MAX_EVENT_BYTES,
            "files": ENGINE_EVENT_MAX_FILES,
            "events_per_minute": ENGINE_EVENT_RATE_LIMIT,
        },
    }


def ack_frame(event_uid: str, status: str, message: str = "") -> dict:
    return {"type": FRAME_ACK, "event_uid": event_uid, "status": status, "message": message}


def error_frame(message: str, *, code: str = "edge_error") -> dict:
    return {"type": FRAME_ERROR, "code": code, "message": message}


__all__ = [
    "ACK_DUPLICATE",
    "ACK_REJECTED",
    "ACK_STORED",
    "CLOSE_ERROR",
    "CLOSE_FORBIDDEN",
    "CLOSE_REPLACED",
    "Connection",
    "ConnectionRegistry",
    "DEFAULT_QUERY_BYTES",
    "FRAME_ACK",
    "FRAME_ERROR",
    "FRAME_EVENT",
    "FRAME_HEARTBEAT",
    "FRAME_HELLO",
    "FRAME_HELLO_OK",
    "FRAME_PAUSE",
    "FRAME_PING",
    "FRAME_QUERY",
    "FRAME_QUERY_RESULT",
    "FRAME_RESUME",
    "MAX_QUERY_BYTES",
    "MAX_QUERY_FILES",
    "QUERY_MODES",
    "QUERY_MODE_READ",
    "QUERY_MODE_STAT",
    "ack_frame",
    "error_frame",
    "hello_ok_frame",
    "registry",
]
