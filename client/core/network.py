"""TeamFlow 端侧 SDK：与服务端推理网关的通信层（P0 唯一交付的 client 代码）。

探针（watchdog、diff 提取、脱敏、spool、UI）由另一侧实现，本模块只负责
「把已经算好的事件包可靠地送到服务端，并把服务端的反向查询正确回传」：

    from client.core.network import EdgeClient, build_event

    client = EdgeClient("wss://teamflow.example.com/ws/edge", token="...",
                        device={"name": "linwei-mbp", "platform": "darwin"})
    client.enqueue(build_event(event_uid=new_event_uid(), repo={...}, files=[...]))
    await client.run_forever()

设计要点（对应方案 §4、§5.1）：

* **离线优先**：未连上或发送失败的事件写进本地 JSONL 队列，重连后按序补传；
  队列有上限（默认 500 条），超出丢最旧的。
* **幂等**：``event_uid`` 当主键用；同一个 uid 只入队一次，服务端也有唯一索引。
* **心跳与背压**：空闲时每 ``heartbeat`` 秒发一次 ``heartbeat``；服务端回
  ``pause`` 就停发（例如引擎被关闭），``resume`` 后从队列继续。
* **反向查询**：服务端可发 ``query`` 帧要求补充某几个文件的指纹；
  回调 ``on_query(paths, reason)`` 返回 ``{path: {"sha256": ..., "size": ...}}``。
* **零重依赖**：``websockets`` 只在真正连接时导入，测试可注入 ``socket_factory``。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import random
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Protocol

LOGGER = logging.getLogger("teamflow.edge")

SCHEMA_VERSION = 1
DEFAULT_HEARTBEAT = 20.0
DEFAULT_BACKOFF = (1.0, 2.0, 5.0, 10.0, 30.0)
DEFAULT_QUEUE_LIMIT = 500
# 单条事件最多重发几次；超过就丢弃并计数（避免一条坏事件卡住整个队列）
MAX_SEND_ATTEMPTS = 5

FRAME_HELLO = "hello"
FRAME_EVENT = "event"
FRAME_HEARTBEAT = "heartbeat"
FRAME_QUERY_RESULT = "query_result"
FRAME_HELLO_OK = "hello_ok"
FRAME_ACK = "ack"
FRAME_QUERY = "query"
FRAME_PAUSE = "pause"
FRAME_RESUME = "resume"
FRAME_PING = "ping"
FRAME_ERROR = "error"

ACK_STORED = "stored"
ACK_DUPLICATE = "duplicate"
ACK_REJECTED = "rejected"


# ------------------------------------------------------------------ 帧构造

def new_event_uid(prefix: str = "evt") -> str:
    """端侧事件幂等键：时间戳 + 随机串，满足服务端 ``[A-Za-z0-9._:-]{8,80}``。"""
    return f"{prefix}-{int(time.time() * 1000)}-{uuid.uuid4().hex[:12]}"


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def build_event(
    *,
    event_uid: str,
    repo: dict,
    files: Iterable[dict] = (),
    kind: str = "save",
    stats: dict | None = None,
    device: dict | None = None,
    notes: str = "",
    redactions: int = 0,
    trigger: str = "save",
    ts: str | None = None,
) -> dict:
    """组装一个 schema v1 事件包（只做本地最基本的自检，服务端仍会全量校验）。"""
    repo_payload = dict(repo or {})
    if not repo_payload.get("path_hash"):
        raise ValueError("repo 缺少必填字段：path_hash")
    return {
        "schema_version": SCHEMA_VERSION,
        "event_uid": event_uid,
        "kind": kind,
        "ts": ts or now_iso(),
        "trigger": trigger,
        "device": dict(device or {}),
        "repo": repo_payload,
        "stats": dict(stats or {}),
        "files": [dict(item) for item in files],
        "redactions": int(redactions),
        "redacted": True,
        "notes": notes or "",
    }


def hello_frame(token: str, device: dict | None = None, *, agent_version: str = "") -> dict:
    return {
        "type": FRAME_HELLO,
        "schema": SCHEMA_VERSION,
        "token": token,
        "agent_version": agent_version,
        "device": dict(device or {}),
    }


def event_frame(envelope: dict) -> dict:
    return {"type": FRAME_EVENT, "envelope": envelope}


def heartbeat_frame(sent: int = 0, queued: int = 0) -> dict:
    return {"type": FRAME_HEARTBEAT, "ts": now_iso(), "sent": int(sent), "queued": int(queued)}


def query_result_frame(request_id: str, files: dict | None = None, error: str = "") -> dict:
    frame: dict[str, Any] = {"type": FRAME_QUERY_RESULT, "request_id": str(request_id)}
    if error:
        frame["error"] = error
    else:
        frame["files"] = dict(files or {})
    return frame


def file_fingerprint(path: str, size: int, digest: str) -> dict:
    return {"path": path, "size": int(size), "sha256": digest}


def sha256_file(path: str | os.PathLike[str], chunk: int = 1 << 20) -> str:
    """给 ``on_query`` 实现用：只算指纹，不读全文进内存。"""
    hasher = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            hasher.update(block)
    return hasher.hexdigest()


# ------------------------------------------------------------------ 本地队列

@dataclass
class QueueItem:
    envelope: dict
    attempts: int = 0
    queued_at: float = field(default_factory=time.time)

    @property
    def event_uid(self) -> str:
        return str(self.envelope.get("event_uid") or "")


class SpoolQueue:
    """JSONL 追加队列：进程重启后未确认的事件还在。"""

    def __init__(self, path: str | os.PathLike[str], limit: int = DEFAULT_QUEUE_LIMIT) -> None:
        self.path = Path(path)
        self.limit = max(1, int(limit))
        self._items: list[QueueItem] = []
        self._uids: set[str] = set()
        self._load()

    # -- 读写
    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    envelope = record.get("envelope") or {}
                    if isinstance(envelope, dict):
                        self._push(
                            QueueItem(envelope=envelope, attempts=int(record.get("attempts") or 0)),
                            persist=False,
                        )
        except OSError as exc:  # 磁盘坏了也不该炸掉探针
            LOGGER.warning("读取离线队列失败：%s", exc)

    def _rewrite(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            with tmp.open("w", encoding="utf-8") as handle:
                for item in self._items:
                    handle.write(json.dumps(
                        {"attempts": item.attempts, "queued_at": item.queued_at,
                         "envelope": item.envelope},
                        ensure_ascii=False,
                    ) + "\n")
            os.replace(tmp, self.path)
        except OSError as exc:
            LOGGER.warning("写入离线队列失败：%s", exc)

    def _push(self, item: QueueItem, *, persist: bool = True) -> bool:
        uid = item.event_uid
        if not uid or uid in self._uids:
            return False
        self._items.append(item)
        self._uids.add(uid)
        while len(self._items) > self.limit:
            dropped = self._items.pop(0)
            self._uids.discard(dropped.event_uid)
            LOGGER.warning("离线队列已满，丢弃最早的事件 %s", dropped.event_uid)
        if persist:
            self._rewrite()
        return True

    # -- 公共 API
    def __len__(self) -> int:
        return len(self._items)

    def pending(self) -> list[QueueItem]:
        return list(self._items)

    def push(self, envelope: dict) -> bool:
        return self._push(QueueItem(envelope=envelope))

    def requeue(self, item: QueueItem) -> bool:
        """把发出去但没等到 ack 的事件放回队首（保留 attempts，便于限次丢弃）。"""
        if not item.event_uid or item.event_uid in self._uids:
            return False
        item.queued_at = time.time()
        self._items.insert(0, item)
        self._uids.add(item.event_uid)
        while len(self._items) > self.limit:
            dropped = self._items.pop()
            self._uids.discard(dropped.event_uid)
            LOGGER.warning("离线队列已满，丢弃最旧的事件 %s", dropped.event_uid)
        self._rewrite()
        return True

    def remove(self, event_uid: str) -> bool:
        for index, item in enumerate(self._items):
            if item.event_uid == event_uid:
                self._items.pop(index)
                self._uids.discard(event_uid)
                self._rewrite()
                return True
        return False

    def mark_attempt(self, event_uid: str) -> None:
        for item in self._items:
            if item.event_uid == event_uid:
                item.attempts += 1
                return

    def pop_head(self) -> QueueItem | None:
        if not self._items:
            return None
        item = self._items.pop(0)
        self._uids.discard(item.event_uid)
        self._rewrite()
        return item

    def clear(self) -> None:
        self._items.clear()
        self._uids.clear()
        self._rewrite()


# ------------------------------------------------------------------ 连接抽象

class SocketLike(Protocol):
    async def send(self, data: str) -> None: ...

    async def recv(self) -> str: ...

    async def close(self) -> None: ...


SocketFactory = Callable[[str, dict], Awaitable[SocketLike]]
QueryHandler = Callable[[list[str], str], "dict[str, dict] | Awaitable[dict[str, dict]]"]


async def _default_socket_factory(url: str, headers: dict) -> SocketLike:
    """默认用 ``websockets`` 客户端；未安装时给出可执行的提示。"""
    try:
        import websockets  # 延迟导入：SDK 的构造与队列逻辑不需要它
    except ModuleNotFoundError as exc:  # pragma: no cover - 取决于运行环境
        raise RuntimeError("发送事件需要 websockets，请先执行：pip install websockets") from exc
    return await websockets.connect(url, additional_headers=headers or None, ping_interval=None)


@dataclass
class EdgeSession:
    """一次连上之后的会话状态（重连会重建）。"""

    device_id: int | None = None
    team_id: int | None = None
    server_time: str = ""
    paused: bool = False


class EdgeClient:
    """端侧通信客户端。

    典型用法（配合自己的探针）::

        client = EdgeClient(ws_url, token, device={"name": "linwei-mbp"})
        client.on_query = lambda paths, reason: {p: {"sha256": sha256_file(p)} for p in paths}
        await client.run_forever()
    """

    def __init__(
        self,
        url: str,
        token: str,
        *,
        device: dict | None = None,
        agent_version: str = "",
        spool_path: str | os.PathLike[str] | None = None,
        queue_limit: int = DEFAULT_QUEUE_LIMIT,
        heartbeat: float = DEFAULT_HEARTBEAT,
        backoff: Iterable[float] = DEFAULT_BACKOFF,
        socket_factory: SocketFactory | None = None,
        on_query: QueryHandler | None = None,
        on_event: Callable[[dict], None] | None = None,
        headers: dict | None = None,
    ) -> None:
        if not url:
            raise ValueError("url 不能为空")
        if not token:
            raise ValueError("token 不能为空（用管理员生成的配对码换取）")
        self.url = url
        self.token = token
        self.device = dict(device or {})
        self.agent_version = agent_version
        self.queue = SpoolQueue(spool_path or self._default_spool_path(), limit=queue_limit)
        self.heartbeat = max(5.0, float(heartbeat))
        self.backoff = tuple(float(value) for value in backoff) or DEFAULT_BACKOFF
        self.socket_factory = socket_factory or _default_socket_factory
        self.on_query = on_query
        self.on_event = on_event
        self.headers = dict(headers or {})
        self.session = EdgeSession()
        self.sent = 0
        self.rejected = 0
        self.duplicates = 0
        self.dropped = 0
        self.last_error = ""
        self._acks: dict[str, tuple[str, str]] = {}
        self._stop = asyncio.Event()

    # -- 路径与状态
    @staticmethod
    def _default_spool_path() -> Path:
        base = os.environ.get("TEAMFLOW_EDGE_DIR")
        root = Path(base) if base else Path.home() / ".teamflow"
        return root / "events.jsonl"

    def stats(self) -> dict:
        return {
            "connected": self.session.device_id is not None,
            "device_id": self.session.device_id,
            "team_id": self.session.team_id,
            "paused": self.session.paused,
            "queued": len(self.queue),
            "sent": self.sent,
            "rejected": self.rejected,
            "duplicates": self.duplicates,
            "dropped": self.dropped,
            "last_error": self.last_error,
        }

    def enqueue(self, envelope: dict) -> bool:
        """把事件放进队列（离线也能入队）；返回 False 表示 uid 重复。"""
        accepted = self.queue.push(envelope)
        if accepted and callable(self.on_event):
            try:
                self.on_event(envelope)
            except Exception:  # noqa: BLE001 - 回调失败不影响入队
                LOGGER.debug("on_event 回调异常", exc_info=True)
        return accepted

    def stop(self) -> None:
        """请求退出 ``run_forever``。"""
        self._stop.set()

    # -- 连接流程
    async def run_forever(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            try:
                await self._run_once()
                attempt = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 离线是常态，不能退出
                self.last_error = f"{type(exc).__name__}: {exc}"
                LOGGER.warning("端侧连接中断：%s", self.last_error)
                delay = self.backoff[min(attempt, len(self.backoff) - 1)]
                attempt += 1
                await self._sleep(delay + random.uniform(0, 0.5))

    async def _run_once(self) -> None:
        socket = await self.socket_factory(self.url, self.headers)
        try:
            await socket.send(json.dumps(
                hello_frame(self.token, self.device, agent_version=self.agent_version),
                ensure_ascii=False,
            ))
            hello = _loads(await asyncio.wait_for(socket.recv(), timeout=15.0))
            if hello.get("type") == FRAME_ERROR:
                raise RuntimeError(str(hello.get("message") or "服务端拒绝了配对"))
            if hello.get("type") != FRAME_HELLO_OK:
                raise RuntimeError(f"握手响应异常：{hello.get('type')!r}")
            self.session = EdgeSession(
                device_id=hello.get("device_id"),
                team_id=hello.get("team_id"),
                server_time=str(hello.get("server_time") or ""),
                paused=bool(hello.get("paused")),
            )
            self.last_error = ""
            LOGGER.info("已连接推理网关（device=%s, team=%s）",
                        self.session.device_id, self.session.team_id)
            await self._pump(socket)
        finally:
            await _quiet_close(socket)
            self.session = EdgeSession()

    async def _pump(self, socket: SocketLike) -> None:
        reader = asyncio.create_task(self._read_loop(socket))
        heartbeat = asyncio.create_task(self._heartbeat_loop(socket))
        try:
            while not self._stop.is_set():
                if reader.done():
                    exc = reader.exception()
                    if exc:
                        raise exc
                    return  # 服务端正常关闭
                if self.session.paused:
                    await self._sleep(1.0)
                    continue
                item = self.queue.pop_head()
                if item is None:
                    await self._sleep(0.5)
                    continue
                await socket.send(json.dumps(event_frame(item.envelope), ensure_ascii=False))
                self.sent += 1
                # 等 ack：服务端收到就回；超时/断线要把事件放回队列，不能静默丢
                if not await self._await_ack(socket, item):
                    item.attempts += 1
                    if item.attempts > MAX_SEND_ATTEMPTS:
                        self.dropped += 1
                        self.last_error = f"事件 {item.event_uid} 连续 {item.attempts} 次未确认，已丢弃"
                        LOGGER.warning(self.last_error)
                    else:
                        self.queue.requeue(item)
                        self.last_error = (
                            f"事件 {item.event_uid} 未收到 ack（第 {item.attempts} 次），已放回队列"
                        )
                        return  # 让外层重连，避免在坏连接上空转
        finally:
            for task in (reader, heartbeat):
                task.cancel()
            await asyncio.gather(reader, heartbeat, return_exceptions=True)

    async def _await_ack(self, socket: SocketLike, item: QueueItem, timeout: float = 20.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if item.event_uid in self._acks:
                status, message = self._acks.pop(item.event_uid)
                if status == ACK_REJECTED:
                    self.rejected += 1
                    self.last_error = message or "事件被服务端拒收"
                    LOGGER.warning("事件 %s 被拒收：%s", item.event_uid, self.last_error)
                elif status == ACK_DUPLICATE:
                    self.duplicates += 1
                return True
            await self._sleep(0.05)
        return False

    async def _read_loop(self, socket: SocketLike) -> None:
        while True:
            frame = _loads(await socket.recv())
            kind = frame.get("type")
            if kind == FRAME_ACK:
                uid = str(frame.get("event_uid") or "")
                self._acks[uid] = (
                    str(frame.get("status") or ACK_STORED),
                    str(frame.get("message") or ""),
                )
            elif kind == FRAME_QUERY:
                await self._answer_query(socket, frame)
            elif kind == FRAME_PAUSE:
                self.session.paused = True
                LOGGER.info("服务端要求暂停上报：%s", frame.get("reason") or "")
            elif kind == FRAME_RESUME:
                self.session.paused = False
                LOGGER.info("服务端恢复上报")
            elif kind == FRAME_PING:
                await socket.send(json.dumps(heartbeat_frame(self.sent, len(self.queue))))

    async def _answer_query(self, socket: SocketLike, frame: dict) -> None:
        request_id = str(frame.get("request_id") or "")
        paths = [str(path) for path in (frame.get("paths") or []) if str(path).strip()]
        reason = str(frame.get("reason") or "")
        if not callable(self.on_query):
            await socket.send(json.dumps(
                query_result_frame(request_id, error="端侧未实现反向查询"), ensure_ascii=False))
            return
        try:
            result = self.on_query(paths, reason)
            if asyncio.iscoroutine(result):
                result = await result
            await socket.send(json.dumps(
                query_result_frame(request_id, files=result or {}), ensure_ascii=False))
        except Exception as exc:  # noqa: BLE001 - 查询失败也要回包，否则服务端干等
            await socket.send(json.dumps(
                query_result_frame(request_id, error=str(exc)), ensure_ascii=False))

    async def _heartbeat_loop(self, socket: SocketLike) -> None:
        while True:
            await self._sleep(self.heartbeat)
            await socket.send(json.dumps(heartbeat_frame(self.sent, len(self.queue))))

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=max(0.01, float(seconds)))
        except asyncio.TimeoutError:
            pass


def _loads(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "replace")
    try:
        value = json.loads(str(raw))
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


async def _quiet_close(socket: SocketLike) -> None:
    close = getattr(socket, "close", None)
    if not callable(close):
        return
    try:
        await close()
    except Exception:  # noqa: BLE001
        LOGGER.debug("关闭连接时异常", exc_info=True)


__all__ = [
    "ACK_DUPLICATE",
    "ACK_REJECTED",
    "ACK_STORED",
    "DEFAULT_BACKOFF",
    "DEFAULT_HEARTBEAT",
    "DEFAULT_QUEUE_LIMIT",
    "EdgeClient",
    "EdgeSession",
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
    "QueueItem",
    "SCHEMA_VERSION",
    "SocketFactory",
    "SocketLike",
    "SpoolQueue",
    "build_event",
    "event_frame",
    "file_fingerprint",
    "heartbeat_frame",
    "hello_frame",
    "new_event_uid",
    "now_iso",
    "query_result_frame",
    "sha256_file",
]
