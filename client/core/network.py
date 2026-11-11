"""TeamFlow 端侧 SDK：与服务端推理网关的通信层（`client/` 里唯一由服务端团队维护的模块）。

探针（watchdog、diff 提取、脱敏、桌面 UI）由另一侧实现；本模块只负责三件事：

1. **组包**：``build_event`` / ``validate_event`` 在本地按 schema v1 自检，
   错误在入队前就暴露，不必等服务端 ack rejected；
2. **可靠上行**：``EdgeClient`` 维护 WSS 长连接（重连退避、心跳、ack 确认、
   窗口流水线、暂停/恢复、离线 JSONL 队列、可选的 REST 兜底）；
3. **反向查询**：服务端 ``query`` 帧到来时调用 ``on_query`` 并回传文件指纹。

典型用法（配合自己的探针）::

    from client.core.network import EdgeClient, build_event, new_event_uid

    client = EdgeClient("wss://teamflow.example.com/api/edge/ws", token="tfd_...",
                        device={"name": "linwei-mbp", "platform": "darwin"})
    client.on_query = lambda paths, reason: {
        path: {"sha256": sha256_file(path)} for path in paths
    }
    client.enqueue(build_event(
        event_uid=new_event_uid(),
        repo={"label": "teamflow", "path_hash": repo_path_hash("/code/teamflow"),
              "branch": "feat/12-login"},
        files=[{"path": "server/app/routers/auth.py", "status": "modified",
                "added": 40, "removed": 3}],
    ))
    client.run_blocking()          # 或在 asyncio 里 await client.run_forever()

设计要点（对应方案 §4、§5.1）：

* **离线优先**：未连上或发送失败的事件写进本地 JSONL 队列，重连后按序补传；
  队列有上限（默认 500 条），超出丢最旧的。
* **幂等**：``event_uid`` 当主键；同一个 uid 只入队一次，服务端也有唯一索引。
* **窗口流水线**：默认同时在途 4 条，不必等上一条 ack 才发下一条；某条 ack 超时就把
  在途事件放回队首并重连，避免在坏连接上空转。
* **致命关闭**：服务端以 4401（配对码无效/已吊销）或 4409（同一设备在别处重连）
  关闭连接时不再无限重试，``client.fatal`` 会记录原因。
* **零重依赖**：``websockets`` 只在真正连接时导入，测试可注入 ``socket_factory``。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import random
import re
import sys
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
# 同时在途的事件数：服务端逐条 ack，窗口 >1 才能吃满带宽
DEFAULT_WINDOW = 4
# 单条事件最多重发几次；超过就丢弃并计数（避免一条坏事件卡住整个队列）
MAX_SEND_ATTEMPTS = 5
# 等 ack 的上限；超时说明连接已经不可用
ACK_TIMEOUT = 20.0

# 与服务端 app/config.py 保持一致（只用于本地自检，服务端仍会全量校验）
MAX_EVENT_BYTES = 512 * 1024
MAX_FILES = 400
MAX_NOTES = 4000
MAX_PATH = 400
MAX_UID = 80

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

# 服务端约定：event kind 与文件状态
EVENT_KINDS = ("save", "snapshot", "commit", "branch-switch", "manual")
FILE_STATUSES = ("added", "modified", "deleted", "renamed", "untracked")

# 这些关闭码重试没有意义：配对码坏了或同设备被别的连接顶掉
FATAL_CLOSE_CODES = {
    4401: "配对码无效、设备已被吊销或已过期，请让管理员重新生成配对码",
    4409: "同一设备在别处重新连接，本连接已让位",
    1008: "服务端拒绝该连接",
}
# 服务端 error 帧里的 code，命中就停止重连
FATAL_ERROR_CODES = {"bad_device_token", "device_revoked", "pair_expired"}

_UID_RE = re.compile(r"^[A-Za-z0-9._:-]{8,80}$")
_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_HEX_RE = re.compile(r"^[0-9a-fA-F]{8,64}$")
_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")

_TEST_PATTERNS = (
    re.compile(r"(^|/)(tests?|__tests__|spec|specs)/"),
    re.compile(r"(^|/)tests?_[^/]+$"),
    re.compile(r"_test\.(py|go|rs|ts|js|mjs|cjs)$"),
    re.compile(r"\.(test|spec)\.(ts|tsx|js|jsx|mjs|cjs|vue)$"),
    re.compile(r"(^|/)conftest\.py$"),
)

_LANG_BY_SUFFIX = {
    ".py": "python", ".pyi": "python", ".ts": "typescript", ".tsx": "typescript",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".vue": "vue", ".go": "go", ".rs": "rust", ".java": "java", ".kt": "kotlin",
    ".rb": "ruby", ".php": "php", ".cs": "csharp", ".c": "c", ".h": "c",
    ".cpp": "cpp", ".hpp": "cpp", ".swift": "swift", ".sql": "sql",
    ".md": "markdown", ".json": "json", ".yaml": "yaml", ".yml": "yaml",
    ".toml": "toml", ".css": "css", ".html": "html", ".sh": "shell",
}


# ------------------------------------------------------------------ 异常

class EdgeError(Exception):
    """SDK 抛出的异常基类。"""


class EventInvalid(EdgeError, ValueError):
    """事件包在本地自检就不合法（字段名放在 ``field`` 上）。"""

    def __init__(self, message: str, *, field: str = "") -> None:
        super().__init__(message)
        self.field = field


class FatalClose(EdgeError):
    """服务端关闭了连接，且这个关闭码不该重试。"""

    def __init__(self, code: int, reason: str = "") -> None:
        self.code = int(code)
        detail = FATAL_CLOSE_CODES.get(self.code, "")
        message = f"服务端关闭连接（{code}）：{reason or detail}"
        super().__init__(message)


# ------------------------------------------------------------------ 小工具

def new_event_uid(prefix: str = "evt") -> str:
    """端侧事件幂等键：时间戳 + 随机串，满足服务端 ``[A-Za-z0-9._:-]{8,80}``。"""
    return f"{prefix}-{int(time.time() * 1000)}-{uuid.uuid4().hex[:12]}"


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def is_test_path(path: str) -> bool:
    text = str(path or "").replace("\\", "/")
    return any(pattern.search(text) for pattern in _TEST_PATTERNS)


def lang_for(path: str) -> str:
    suffix = Path(str(path or "")).suffix.lower()
    return _LANG_BY_SUFFIX.get(suffix, "")


def repo_path_hash(path: str | os.PathLike[str]) -> str:
    """仓库路径的稳定指纹（16 位小写 hex）：端侧只上报这个，不上报真实路径。"""
    resolved = os.path.normcase(os.path.abspath(os.path.expanduser(str(path))))
    return hashlib.sha256(resolved.encode("utf-8", "replace")).hexdigest()[:16]


def platform_name() -> str:
    """给 ``device.platform`` 用的短名（服务端上限 40 字符）。"""
    if sys.platform.startswith("darwin"):
        return "darwin"
    if sys.platform.startswith("win"):
        return "windows"
    return "linux"


def summarize_files(files: Iterable[dict]) -> dict:
    """从 files 里推导 ``stats``，省得调用方自己数测试文件。"""
    counts = {
        "files": 0, "added": 0, "removed": 0,
        "tests_added": 0, "tests_changed": 0, "tests_removed": 0, "binary": 0,
    }
    for item in files:
        counts["files"] += 1
        counts["added"] += int(item.get("added") or 0)
        counts["removed"] += int(item.get("removed") or 0)
        if not is_test_path(str(item.get("path") or "")):
            continue
        status = str(item.get("status") or "")
        if status == "added":
            counts["tests_added"] += 1
        elif status == "deleted":
            counts["tests_removed"] += 1
        else:
            counts["tests_changed"] += 1
    return counts


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


# ------------------------------------------------------------------ 组包与自检

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
    digest: dict | None = None,
) -> dict:
    """组装一个 schema v1 事件包。

    ``stats`` 省略时按 ``files`` 自动统计（含测试文件计数），``files`` 里的
    ``lang`` 省略时按扩展名补。只做本地最基本的自检，服务端仍会全量校验。

    ``digest`` 是本轮的符号级摘要（新增/移除了哪些函数、类、文档小节），
    不含路径也不含代码原文——静默模式下也会上行，让推断能说出「改了什么」。
    """
    repo_payload = dict(repo or {})
    if not repo_payload.get("path_hash"):
        raise EventInvalid("repo 缺少必填字段：path_hash", field="repo.path_hash")
    file_items: list[dict] = []
    for item in files:
        entry = dict(item)
        if not entry.get("lang"):
            entry["lang"] = lang_for(str(entry.get("path") or ""))
        file_items.append(entry)
    content_digest = dict(digest or {})
    return {
        "schema_version": SCHEMA_VERSION,
        "event_uid": event_uid,
        "kind": kind,
        "ts": ts or now_iso(),
        "trigger": trigger,
        "device": dict(device or {}),
        "repo": repo_payload,
        "stats": dict(stats) if stats is not None else summarize_files(file_items),
        "files": file_items,
        "digest": {
            "added": [str(name)[:60] for name in (content_digest.get("added") or [])][:16],
            "removed": [str(name)[:60] for name in (content_digest.get("removed") or [])][:16],
            "notes": [str(note)[:70] for note in (content_digest.get("notes") or [])][:8],
            # 新增的内容行：文档是正文，代码是骨架行。没有它，洞察只能说「多了六行」。
            "lines": [str(line)[:70] for line in (content_digest.get("lines") or [])][:10],
        },
        "redactions": int(redactions),
        "redacted": True,
        "notes": notes or "",
    }


def _check_path(path: str) -> str:
    text = str(path or "")
    if not text:
        raise EventInvalid("files[].path 不能为空", field="files.path")
    if len(text) > MAX_PATH:
        raise EventInvalid(f"files[].path 不能超过 {MAX_PATH} 个字符", field="files.path")
    if "\x00" in text:
        raise EventInvalid("files[].path 不能包含 NUL 字符", field="files.path")
    if _DRIVE_RE.match(text):
        raise EventInvalid(f"files[].path 必须是仓库内相对路径：{text}", field="files.path")
    if text.startswith("/") or text.startswith("~"):
        raise EventInvalid(f"files[].path 不能是绝对路径：{text}", field="files.path")
    if ".." in text.split("/") or ".." in text.split("\\"):
        raise EventInvalid(f"files[].path 不能包含上级目录：{text}", field="files.path")
    return text


def validate_event(
    envelope: dict,
    *,
    max_bytes: int = MAX_EVENT_BYTES,
    max_files: int = MAX_FILES,
) -> dict:
    """按服务端 schema v1 在本地自检，返回规整后的副本（不修改入参）。

    与服务端 ``parse_envelope`` 对齐：schema 版本、uid 字符集、kind 白名单、
    时间戳格式、仓库指纹、相对路径、文件状态、notes 长度、整包体积。
    """
    if not isinstance(envelope, dict):
        raise EventInvalid("事件包必须是 JSON 对象", field="envelope")
    data = dict(envelope)
    if int(data.get("schema_version") or 0) != SCHEMA_VERSION:
        raise EventInvalid(f"schema_version 必须是 {SCHEMA_VERSION}", field="schema_version")
    uid = str(data.get("event_uid") or "")
    if not _UID_RE.match(uid):
        raise EventInvalid(
            "event_uid 只能由字母、数字、. _ : - 组成，长度 8–80", field="event_uid")
    kind = str(data.get("kind") or "save")
    if kind not in EVENT_KINDS:
        raise EventInvalid(f"kind 必须是 {'/'.join(EVENT_KINDS)} 之一", field="kind")
    ts = str(data.get("ts") or "")
    if not _ISO_RE.match(ts):
        raise EventInvalid("ts 必须是 UTC ISO 时间，例如 2026-11-10T06:12:33Z", field="ts")
    repo = data.get("repo")
    if not isinstance(repo, dict):
        raise EventInvalid("repo 必须是对象", field="repo")
    path_hash = str(repo.get("path_hash") or "")
    if not _HEX_RE.match(path_hash):
        raise EventInvalid("repo.path_hash 必须是 8–64 位十六进制", field="repo.path_hash")
    files = data.get("files")
    if files is None:
        files = []
    if not isinstance(files, list):
        raise EventInvalid("files 必须是数组", field="files")
    if len(files) > max_files:
        raise EventInvalid(f"单次事件的文件数不能超过 {max_files}", field="files")
    cleaned: list[dict] = []
    for index, item in enumerate(files):
        if not isinstance(item, dict):
            raise EventInvalid(f"files[{index}] 必须是对象", field=f"files[{index}]")
        entry = dict(item)
        entry["path"] = _check_path(entry.get("path"))
        status = str(entry.get("status") or "modified")
        if status not in FILE_STATUSES:
            raise EventInvalid(
                f"files[{index}].status 必须是 {'/'.join(FILE_STATUSES)} 之一",
                field=f"files[{index}].status")
        entry["status"] = status
        cleaned.append(entry)
    notes = str(data.get("notes") or "")
    if len(notes) > MAX_NOTES:
        raise EventInvalid(f"notes 不能超过 {MAX_NOTES} 个字符", field="notes")
    data.update({
        "schema_version": SCHEMA_VERSION,
        "event_uid": uid,
        "kind": kind,
        "ts": ts,
        "trigger": str(data.get("trigger") or "save")[:40],
        "device": dict(data.get("device") or {}),
        "repo": {**repo, "path_hash": path_hash.lower()},
        "stats": dict(data.get("stats") or {}),
        "files": cleaned,
        "redactions": int(data.get("redactions") or 0),
        "redacted": bool(data.get("redacted", True)),
        "notes": notes,
    })
    payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
    if len(payload) > max_bytes:
        raise EventInvalid(
            f"事件包过大：{len(payload)} 字节，上限 {max_bytes} 字节", field="envelope")
    return data


# ------------------------------------------------------------------ 帧构造

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

    def __contains__(self, event_uid: object) -> bool:
        return str(event_uid) in self._uids

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
QueryRequestHandler = Callable[
    [dict], "dict | Awaitable[dict]"
]
"""新式反向查询回调：收到 ``{request_id, files, paths, mode, max_bytes, reason}``。

实现里通常直接放一个 :class:`client.core.query.QuerySandbox`（可调用对象），
它按请求里的 ``files`` 清单逐个答复，``mode="read"`` 才带内容。
"""
AckHandler = Callable[[str, str, "QueueItem | None"], None]
RestFallback = Callable[[dict], "dict | str | Awaitable[dict | str]"]


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
    heartbeat: float = DEFAULT_HEARTBEAT
    limits: dict = field(default_factory=dict)


class EdgeClient:
    """端侧通信客户端。

    典型用法（配合自己的探针）::

        client = EdgeClient(ws_url, token, device={"name": "linwei-mbp"})
        client.on_query = lambda paths, reason: {p: {"sha256": sha256_file(p)} for p in paths}
        await client.run_forever()

    值得知道的几个属性：

    * ``stats()``：连接状态、队列长度、在途数、发送/拒收/重复/丢弃计数、最近错误。
    * ``fatal``：命中致命关闭码（4401/4409）时记录 ``{"code", "reason"}``，此时不再重连。
    * ``on_ack(uid, status, item)``：每条 ack 的回调；``on_reject(uid, message)``：被拒收时回调。
    * ``rest_fallback(envelope)``：ack 反复超时时尝试走 REST 兜底（见 ``client/api.py``）。
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
        on_query_request: QueryRequestHandler | None = None,
        on_event: Callable[[dict], None] | None = None,
        on_ack: AckHandler | None = None,
        on_reject: Callable[[str, str, "QueueItem | None"], None] | None = None,
        on_error: Callable[[str, str], None] | None = None,
        headers: dict | None = None,
        window: int = DEFAULT_WINDOW,
        ack_timeout: float = ACK_TIMEOUT,
        validate: bool = True,
        rest_fallback: RestFallback | None = None,
    ) -> None:
        if not url:
            raise ValueError("url 不能为空")
        if not token:
            raise ValueError("token 不能为空（用管理员生成的配对码）")
        self.url = url
        self.token = token
        self.device = dict(device or {})
        self.agent_version = agent_version
        self.queue = SpoolQueue(spool_path or self._default_spool_path(), limit=queue_limit)
        self.heartbeat = max(5.0, float(heartbeat))
        self.backoff = tuple(float(value) for value in backoff) or DEFAULT_BACKOFF
        self.socket_factory = socket_factory or _default_socket_factory
        self.on_query = on_query
        self.on_query_request = on_query_request
        self.on_event = on_event
        self.on_ack = on_ack
        self.on_reject = on_reject
        self.on_error = on_error
        self.headers = dict(headers or {})
        self.window = max(1, int(window))
        self.ack_timeout = max(1.0, float(ack_timeout))
        self.validate = bool(validate)
        self.rest_fallback = rest_fallback

        self.session = EdgeSession()
        self.sent = 0
        self.rejected = 0
        self.duplicates = 0
        self.dropped = 0
        self.connects = 0
        self.reconnects = 0
        self.last_error = ""
        self.last_ack_at = ""
        self.fatal: dict | None = None
        self._acks: dict[str, tuple[str, str]] = {}
        self._inflight: dict[str, QueueItem] = {}
        self._waiters: dict[str, asyncio.Future] = {}
        self._stop = asyncio.Event()

    # -- 路径与状态
    @staticmethod
    def _default_spool_path() -> Path:
        base = os.environ.get("TEAMFLOW_EDGE_DIR")
        root = Path(base) if base else Path.home() / ".teamflow"
        return root / "events.jsonl"

    @property
    def inflight(self) -> int:
        return len(self._inflight)

    @property
    def connected(self) -> bool:
        return self.session.device_id is not None

    @property
    def paused(self) -> bool:
        return self.session.paused

    def stats(self) -> dict:
        return {
            "connected": self.connected,
            "device_id": self.session.device_id,
            "team_id": self.session.team_id,
            "paused": self.session.paused,
            "queued": len(self.queue),
            "inflight": self.inflight,
            "sent": self.sent,
            "rejected": self.rejected,
            "duplicates": self.duplicates,
            "dropped": self.dropped,
            "connects": self.connects,
            "reconnects": self.reconnects,
            "last_error": self.last_error,
            "last_ack_at": self.last_ack_at,
            "fatal": dict(self.fatal) if self.fatal else None,
            "spool": str(self.queue.path),
            "limits": dict(self.session.limits),
        }

    def enqueue(self, envelope: dict, *, validate: bool | None = None) -> bool:
        """把事件放进队列（离线也能入队）；返回 False 表示 uid 重复。

        默认会在入队前做本地自检（``validate_event``），不合法抛 ``EventInvalid``；
        探针自己已经校验过时传 ``validate=False`` 跳过。
        """
        should_validate = self.validate if validate is None else bool(validate)
        payload = validate_event(envelope) if should_validate else envelope
        accepted = self.queue.push(payload)
        if accepted and callable(self.on_event):
            try:
                self.on_event(payload)
            except Exception:  # noqa: BLE001 - 回调失败不影响入队
                LOGGER.debug("on_event 回调异常", exc_info=True)
        return accepted

    def stop(self) -> None:
        """请求退出 ``run_forever``。"""
        self._stop.set()

    def run_blocking(self) -> None:
        """给非 asyncio 的调用方用：阻塞跑 ``run_forever`` 直到 ``stop()``/Ctrl+C。"""
        try:
            asyncio.run(self.run_forever())
        except KeyboardInterrupt:  # pragma: no cover - 交互式退出
            self.stop()

    async def wait_idle(self, timeout: float | None = None) -> bool:
        """等队列清空且没有在途事件；返回是否在超时前空闲。"""
        deadline = None if timeout is None else time.monotonic() + float(timeout)
        while len(self.queue) or self.inflight:
            if deadline is not None and time.monotonic() > deadline:
                return False
            await asyncio.sleep(0.1)
        return True

    # -- 连接流程
    async def run_forever(self) -> None:
        """连上就补传，断开就退避重连；致命关闭码会让它直接返回。"""
        attempt = 0
        while not self._stop.is_set():
            try:
                await self._run_once()
                attempt = 0
            except asyncio.CancelledError:
                raise
            except FatalClose as exc:
                self.fatal = {"code": exc.code, "reason": str(exc)}
                self.last_error = str(exc)
                LOGGER.error("端侧连接被服务端终止：%s", exc)
                if callable(self.on_error):
                    _safe_call(self.on_error, "fatal", str(exc))
                return
            except Exception as exc:  # noqa: BLE001 - 离线是常态，不能退出
                self.last_error = f"{type(exc).__name__}: {exc}"
                LOGGER.warning("端侧连接中断：%s", self.last_error)
                if callable(self.on_error):
                    _safe_call(self.on_error, "disconnect", self.last_error)
                delay = self.backoff[min(attempt, len(self.backoff) - 1)]
                attempt += 1
                if self.reconnects or self.connects:
                    self.reconnects += 1
                await self._sleep(delay + random.uniform(0, 0.5))
            else:
                if self._stop.is_set():
                    return
                # 服务端正常关闭：短暂停一下再连，避免空转
                await self._sleep(0.5)

    async def _run_once(self) -> None:
        socket = await self.socket_factory(self.url, self.headers)
        try:
            await socket.send(json.dumps(
                hello_frame(self.token, self.device, agent_version=self.agent_version),
                ensure_ascii=False,
            ))
            hello = _loads(await asyncio.wait_for(socket.recv(), timeout=15.0))
            if hello.get("type") == FRAME_ERROR:
                code = str(hello.get("code") or "")
                message = str(hello.get("message") or "服务端拒绝了配对")
                if code in FATAL_ERROR_CODES:
                    raise FatalClose(4401, message)
                raise RuntimeError(message)
            if hello.get("type") != FRAME_HELLO_OK:
                raise RuntimeError(f"握手响应异常：{hello.get('type')!r}")
            limits = hello.get("limits") or {}
            self.session = EdgeSession(
                device_id=hello.get("device_id"),
                team_id=hello.get("team_id"),
                server_time=str(hello.get("server_time") or ""),
                paused=bool(hello.get("paused")),
                heartbeat=float(hello.get("heartbeat") or self.heartbeat),
                limits=dict(limits) if isinstance(limits, dict) else {},
            )
            self.connects += 1
            self.last_error = ""
            LOGGER.info("已连接推理网关（device=%s, team=%s）",
                        self.session.device_id, self.session.team_id)
            await self._pump(socket)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            code = _close_code(exc)
            if code in FATAL_CLOSE_CODES:
                raise FatalClose(code, str(exc)) from exc
            raise
        finally:
            await _quiet_close(socket)
            self._drop_waiters()
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
                await self._fill_window(socket)
                if not self._inflight:
                    await self._sleep(0.3)
                    continue
                await self._wait_acks()
        finally:
            for task in (reader, heartbeat):
                task.cancel()
            await asyncio.gather(reader, heartbeat, return_exceptions=True)
            # 连接断了就把在途事件放回队首，不能静默丢
            self._requeue_inflight(count_attempt=False)

    async def _fill_window(self, socket: SocketLike) -> None:
        """把队列里的下一批事件发出去，直到在途数达到窗口上限。"""
        loop = asyncio.get_running_loop()
        while len(self._inflight) < self.window and not self.session.paused:
            item = self.queue.pop_head()
            if item is None:
                return
            uid = item.event_uid
            self._inflight[uid] = item
            self._waiters[uid] = loop.create_future()
            await socket.send(json.dumps(event_frame(item.envelope), ensure_ascii=False))
            self.sent += 1

    async def _wait_acks(self) -> None:
        """等任意一条 ack；超时说明连接已经不可用。"""
        waiters = [future for future in self._waiters.values() if not future.done()]
        if not waiters:
            return
        done, _ = await asyncio.wait(
            waiters, timeout=self.ack_timeout, return_when=asyncio.FIRST_COMPLETED)
        for future in done:
            uid = self._uid_of(future)
            if uid is None:
                continue
            status, message = future.result()
            item = self._inflight.pop(uid, None)
            self._waiters.pop(uid, None)
            self._handle_ack(uid, status, message, item)
        if done:
            return
        # 一条都没回来：把在途的放回队列，交给 run_forever 重连
        for uid in list(self._inflight):
            future = self._waiters.pop(uid, None)
            if future and not future.done():
                future.cancel()
        self._requeue_inflight(count_attempt=True)
        self.last_error = "等待 ack 超时，已把在途事件放回队列并重连"
        LOGGER.warning(self.last_error)
        raise TimeoutError(self.last_error)

    def _uid_of(self, future: asyncio.Future) -> str | None:
        for uid, candidate in self._waiters.items():
            if candidate is future:
                return uid
        return None

    def _handle_ack(self, uid: str, status: str, message: str, item: QueueItem | None) -> None:
        self.last_ack_at = now_iso()
        if status == ACK_REJECTED:
            self.rejected += 1
            self.last_error = message or "事件被服务端拒收"
            LOGGER.warning("事件 %s 被拒收：%s", uid, self.last_error)
            if item is not None:
                self.dropped += 1
            if callable(self.on_reject):
                _safe_call(self.on_reject, uid, self.last_error, item)
        elif status == ACK_DUPLICATE:
            self.duplicates += 1
        if callable(self.on_ack):
            _safe_call(self.on_ack, uid, status, item)

    def _requeue_inflight(self, *, count_attempt: bool) -> None:
        items = list(self._inflight.values())
        self._inflight.clear()
        for item in items:
            if count_attempt:
                item.attempts += 1
            if count_attempt and item.attempts > MAX_SEND_ATTEMPTS:
                self.dropped += 1
                self.last_error = f"事件 {item.event_uid} 连续 {item.attempts} 次未确认，已丢弃"
                LOGGER.warning(self.last_error)
                if callable(self.on_reject):
                    _safe_call(self.on_reject, item.event_uid, self.last_error, item)
                continue
            self.queue.requeue(item)

    def _resolve_ack(self, uid: str, status: str, message: str) -> None:
        future = self._waiters.get(uid)
        if future is not None and not future.done():
            future.set_result((status, message))
            return
        self._acks[uid] = (status, message)

    def _drop_waiters(self) -> None:
        for future in self._waiters.values():
            if not future.done():
                future.cancel()
        self._waiters.clear()

    async def _read_loop(self, socket: SocketLike) -> None:
        while True:
            frame = _loads(await socket.recv())
            kind = frame.get("type")
            if kind == FRAME_ACK:
                uid = str(frame.get("event_uid") or "")
                status = str(frame.get("status") or ACK_STORED)
                message = str(frame.get("message") or "")
                if uid in self._acks:
                    status, message = self._acks.pop(uid)
                self._resolve_ack(uid, status, message)
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
            elif kind == FRAME_ERROR:
                code = str(frame.get("code") or "")
                message = str(frame.get("message") or "服务端返回错误")
                self.last_error = message
                LOGGER.warning("服务端错误帧（%s）：%s", code or "-", message)
                if code in FATAL_ERROR_CODES:
                    raise FatalClose(4401, message)
                raise RuntimeError(message)
            elif kind:
                LOGGER.debug("忽略未知帧：%s", kind)

    async def _answer_query(self, socket: SocketLike, frame: dict) -> None:
        request_id = str(frame.get("request_id") or "")
        files = [str(item) for item in (frame.get("files") or frame.get("paths") or [])
                 if str(item).strip()]
        reason = str(frame.get("reason") or "")
        mode = str(frame.get("mode") or "stat")
        handler = self.on_query_request or self.on_query
        if not callable(handler):
            await socket.send(json.dumps(
                query_result_frame(request_id, error="端侧未实现反向查询"), ensure_ascii=False))
            return
        try:
            if self.on_query_request is not None:
                # 新式：把整帧交给实现（能拿到 mode / max_bytes / files 清单）
                request = {
                    "request_id": request_id,
                    "files": files,
                    "paths": list(files),
                    "mode": mode,
                    "max_bytes": frame.get("max_bytes"),
                    "reason": reason,
                }
                result = handler(request)
            else:
                result = handler(files, reason)
            if asyncio.iscoroutine(result):
                result = await result
            payload = dict(result or {})
            # 兼容旧回调：直接返回 {path: info} 的实现自动包一层
            if "files" not in payload:
                payload = {"files": payload}
            await socket.send(json.dumps(
                query_result_frame(request_id, files=payload.get("files") or {},
                                   error=str(payload.get("error") or "")),
                ensure_ascii=False))
        except Exception as exc:  # noqa: BLE001 - 查询失败也要回包，否则服务端干等
            await socket.send(json.dumps(
                query_result_frame(request_id, error=str(exc)), ensure_ascii=False))

    async def _heartbeat_loop(self, socket: SocketLike) -> None:
        interval = self.session.heartbeat or self.heartbeat
        while not self._stop.is_set():
            await self._sleep(interval)
            if self._stop.is_set():
                return
            await socket.send(json.dumps(heartbeat_frame(self.sent, len(self.queue))))

    async def _sleep(self, seconds: float) -> None:
        """可被 ``stop()`` 打断的 sleep。

        ``stop()`` 之后 ``Event.wait()`` 会立刻返回，所以这里必须显式让出一次控制权，
        否则心跳/发送循环会变成忙等（不 yield 的无限循环，连取消都送不进去）。
        """
        delay = max(0.01, float(seconds))
        if self._stop.is_set():
            await asyncio.sleep(0)
            return
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass
        await asyncio.sleep(0)


# ------------------------------------------------------------------ 内部工具

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


def _close_code(exc: BaseException) -> int | None:
    """从 websockets 的关闭异常里取 close code（不依赖具体异常类型）。"""
    for attr in ("close_code", "code"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    rcvd = getattr(exc, "rcvd", None)
    value = getattr(rcvd, "code", None)
    return value if isinstance(value, int) else None


def _safe_call(fn: Callable[..., Any], *args: Any) -> None:
    try:
        fn(*args)
    except Exception:  # noqa: BLE001 - 用户回调不能影响主循环
        LOGGER.debug("回调异常", exc_info=True)


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
    "ACK_TIMEOUT",
    "FATAL_CLOSE_CODES",
    "FATAL_ERROR_CODES",
    "DEFAULT_BACKOFF",
    "DEFAULT_HEARTBEAT",
    "DEFAULT_QUEUE_LIMIT",
    "DEFAULT_WINDOW",
    "EVENT_KINDS",
    "EdgeClient",
    "EdgeError",
    "EdgeSession",
    "EventInvalid",
    "FatalClose",
    "FILE_STATUSES",
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
    "MAX_EVENT_BYTES",
    "MAX_FILES",
    "MAX_NOTES",
    "MAX_PATH",
    "MAX_SEND_ATTEMPTS",
    "QueueItem",
    "QueryHandler",
    "QueryRequestHandler",
    "SCHEMA_VERSION",
    "SocketFactory",
    "SocketLike",
    "SpoolQueue",
    "build_event",
    "event_frame",
    "file_fingerprint",
    "heartbeat_frame",
    "hello_frame",
    "is_test_path",
    "lang_for",
    "new_event_uid",
    "now_iso",
    "platform_name",
    "query_result_frame",
    "repo_path_hash",
    "sha256_file",
    "summarize_files",
    "validate_event",
]
