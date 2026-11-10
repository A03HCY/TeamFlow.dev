"""端侧 SDK（``client`` 包）的测试：组包自检、离线队列、窗口发送、致命关闭、配置与 REST 兜底。"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from client import api as client_api  # noqa: E402
from client import config as client_config  # noqa: E402
from client.core import network as sdk  # noqa: E402
from client.core.query import QuerySandbox, QueryRequest, redact_text  # noqa: E402

HELLO_OK = {
    "type": "hello_ok",
    "schema": 1,
    "device_id": 7,
    "team_id": 1,
    "device_name": "test-mac",
    "server_time": "2026-11-10T08:00:00Z",
    "heartbeat": 20,
    "paused": False,
    "limits": {"event_bytes": 524288, "files": 400, "events_per_minute": 120},
}


class FakeSocket:
    """按脚本回帧的假连接；``send`` 只记录，测试自己决定何时回 ack。"""

    def __init__(self, script=None, *, raise_after=None):
        self.incoming: asyncio.Queue = asyncio.Queue()
        for frame in script or []:
            self.incoming.put_nowait(json.dumps(frame))
        self.sent: list[dict] = []
        self.closed = False
        self.raise_after = raise_after

    async def send(self, data: str) -> None:
        frame = json.loads(data)
        self.sent.append(frame)
        if self.raise_after is not None and len(self.sent) == self.raise_after:
            raise FakeClose(4401, "配对码已吊销")

    async def recv(self) -> str:
        return await self.incoming.get()

    async def close(self) -> None:
        self.closed = True

    def frames(self, kind: str) -> list[dict]:
        return [frame for frame in self.sent if frame.get("type") == kind]


class FakeClose(Exception):
    def __init__(self, code: int, reason: str = ""):
        super().__init__(reason)
        self.close_code = code


def make_client(socket, tmp_path, **kwargs) -> sdk.EdgeClient:
    async def factory(url, headers):
        return socket

    return sdk.EdgeClient(
        "ws://127.0.0.1:8000/api/edge/ws",
        "tfd_test_token",
        device={"name": "test-mac", "platform": "darwin"},
        spool_path=tmp_path / "events.jsonl",
        socket_factory=factory,
        heartbeat=3600,
        **kwargs,
    )


def envelope(uid="evt-0001-abcd", **overrides) -> dict:
    payload = sdk.build_event(
        event_uid=uid,
        repo={"label": "teamflow", "path_hash": "ab" * 8, "branch": "feat/1-x"},
        files=[{"path": "server/app/main.py", "status": "modified", "added": 3, "removed": 1}],
    )
    payload.update(overrides)
    return payload


# ------------------------------------------------------------------ 组包


def test_build_event_autofills_stats_and_lang():
    event = sdk.build_event(
        event_uid="evt-0001-abcd",
        repo={"path_hash": "ab" * 8},
        files=[
            {"path": "server/app/main.py", "status": "modified", "added": 10, "removed": 2},
            {"path": "server/tests/test_api.py", "status": "added", "added": 30},
            {"path": "README.md", "status": "deleted", "removed": 5},
        ],
    )
    stats = event["stats"]
    assert stats["files"] == 3
    assert stats["added"] == 40 and stats["removed"] == 7
    assert stats["tests_added"] == 1 and stats["tests_changed"] == 0
    assert event["files"][0]["lang"] == "python"
    assert event["schema_version"] == 1
    assert event["redacted"] is True
    assert event["ts"].endswith("Z")


def test_build_event_requires_repo_hash():
    with pytest.raises(sdk.EventInvalid) as excinfo:
        sdk.build_event(event_uid="evt-0001-abcd", repo={"label": "x"})
    assert excinfo.value.field == "repo.path_hash"


@pytest.mark.parametrize(
    "overrides, expected",
    [
        ({"schema_version": 2}, "schema_version"),
        ({"event_uid": "short"}, "event_uid"),
        ({"kind": "deploy"}, "kind"),
        ({"ts": "2026-11-10 08:00:00"}, "ts"),
        ({"repo": {"path_hash": "zz"}}, "repo.path_hash"),
        ({"notes": "x" * 4001}, "notes"),
    ],
)
def test_validate_event_rejects_bad_fields(overrides, expected):
    payload = envelope()
    payload.update(overrides)
    with pytest.raises(sdk.EventInvalid) as excinfo:
        sdk.validate_event(payload)
    assert expected in str(excinfo.value.field) or expected in str(excinfo.value)


@pytest.mark.parametrize("path", ["/etc/passwd", "C:/Windows/win.ini", "../secrets/.env", "a\x00b"])
def test_validate_event_rejects_unsafe_paths(path):
    payload = envelope()
    payload["files"] = [{"path": path, "status": "modified"}]
    with pytest.raises(sdk.EventInvalid) as excinfo:
        sdk.validate_event(payload)
    assert "path" in excinfo.value.field


def test_validate_event_normalizes_hash_case():
    payload = envelope()
    payload["repo"] = dict(payload["repo"], path_hash="AB" * 8)
    cleaned = sdk.validate_event(payload)
    assert cleaned["repo"]["path_hash"] == "ab" * 8
    assert cleaned is not payload


def test_repo_path_hash_is_stable_hex():
    first = sdk.repo_path_hash("D:/Projects/TeamFlow.dev")
    second = sdk.repo_path_hash("D:/Projects/TeamFlow.dev")
    assert first == second
    assert len(first) == 16 and int(first, 16) >= 0


def test_new_event_uid_matches_server_pattern():
    uid = sdk.new_event_uid()
    assert 8 <= len(uid) <= 80
    assert sdk.validate_event(envelope(uid))["event_uid"] == uid


# ------------------------------------------------------------------ 离线队列


def test_spool_queue_requeue_keeps_attempts(tmp_path):
    queue = sdk.SpoolQueue(tmp_path / "q.jsonl", limit=3)
    assert queue.push(envelope("evt-0001-aaaa")) is True
    assert queue.push(envelope("evt-0001-aaaa")) is False  # 同 uid 去重
    item = queue.pop_head()
    assert item is not None and item.event_uid == "evt-0001-aaaa"
    item.attempts = 2
    assert queue.requeue(item) is True
    again = queue.pop_head()
    assert again is not None and again.attempts == 2
    assert "evt-0001-aaaa" not in queue


def test_spool_queue_drops_oldest_when_full(tmp_path):
    queue = sdk.SpoolQueue(tmp_path / "q.jsonl", limit=2)
    for index in range(3):
        queue.push(envelope(f"evt-000{index}-abcd"))
    uids = [item.event_uid for item in queue.pending()]
    assert uids == ["evt-0001-abcd", "evt-0002-abcd"]
    assert len(queue) == 2


def test_client_enqueue_validates_and_dedups(tmp_path):
    client = make_client(FakeSocket(), tmp_path)
    assert client.enqueue(envelope()) is True
    assert client.enqueue(envelope()) is False
    assert len(client.queue) == 1
    broken = envelope("evt-0002-abcd")
    broken["kind"] = "deploy"
    with pytest.raises(sdk.EventInvalid):
        client.enqueue(broken)
    assert client.enqueue(broken, validate=False) is True


# ------------------------------------------------------------------ 连接与发送


def test_handshake_then_window_sends_both_events(tmp_path):
    socket = FakeSocket([HELLO_OK])
    client = make_client(socket, tmp_path, window=2)
    client.enqueue(envelope("evt-0001-abcd"))
    client.enqueue(envelope("evt-0002-abcd"))

    async def scenario():
        task = asyncio.create_task(client._run_once())
        for _ in range(100):
            if len(socket.frames("event")) >= 2:
                break
            await asyncio.sleep(0.02)
        sent = socket.frames("event")
        assert [frame["envelope"]["event_uid"] for frame in sent] == [
            "evt-0001-abcd", "evt-0002-abcd"]
        assert client.inflight == 2
        assert client.connects == 1
        # 两条 ack 回来之后队列与在途都清空
        for uid in ("evt-0001-abcd", "evt-0002-abcd"):
            socket.incoming.put_nowait(json.dumps(
                {"type": "ack", "event_uid": uid, "status": "stored", "message": ""}))
        for _ in range(100):
            if not client.inflight and not len(client.queue):
                break
            await asyncio.sleep(0.02)
        assert client.inflight == 0
        assert len(client.queue) == 0
        assert client.sent == 2
        client.stop()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_rejected_ack_calls_on_reject(tmp_path):
    seen: list[tuple] = []
    socket = FakeSocket([HELLO_OK])
    client = make_client(socket, tmp_path, on_reject=lambda uid, msg, item: seen.append((uid, msg)))
    client.enqueue(envelope("evt-0001-abcd"))

    async def scenario():
        task = asyncio.create_task(client._run_once())
        for _ in range(100):
            if socket.frames("event"):
                break
            await asyncio.sleep(0.02)
        socket.incoming.put_nowait(json.dumps(
            {"type": "ack", "event_uid": "evt-0001-abcd", "status": "rejected",
             "message": "事件包校验失败：path"}))
        for _ in range(100):
            if seen:
                break
            await asyncio.sleep(0.02)
        client.stop()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
    assert seen and seen[0][0] == "evt-0001-abcd"
    assert "校验失败" in seen[0][1]
    assert client.rejected == 1


def test_pause_stops_sending_until_resume(tmp_path):
    socket = FakeSocket([HELLO_OK])
    client = make_client(socket, tmp_path)
    client.enqueue(envelope("evt-0001-abcd"))

    async def scenario():
        task = asyncio.create_task(client._run_once())
        for _ in range(100):
            if socket.frames("event"):
                break
            await asyncio.sleep(0.02)
        assert len(socket.frames("event")) == 1
        socket.incoming.put_nowait(json.dumps(
            {"type": "ack", "event_uid": "evt-0001-abcd", "status": "stored"}))
        socket.incoming.put_nowait(json.dumps({"type": "pause", "reason": "负载高"}))
        for _ in range(100):
            if client.paused:
                break
            await asyncio.sleep(0.02)
        assert client.paused is True
        client.enqueue(envelope("evt-0002-abcd"))
        await asyncio.sleep(0.2)
        assert len(socket.frames("event")) == 1  # 暂停期间不发
        socket.incoming.put_nowait(json.dumps({"type": "resume"}))
        for _ in range(100):
            if len(socket.frames("event")) == 2:
                break
            await asyncio.sleep(0.02)
        assert client.paused is False
        assert len(socket.frames("event")) == 2
        client.stop()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_query_frame_is_answered(tmp_path):
    socket = FakeSocket([HELLO_OK])
    client = make_client(socket, tmp_path)
    client.on_query = lambda paths, reason: {
        path: {"size": 12, "sha256": "f" * 64} for path in paths}

    async def scenario():
        task = asyncio.create_task(client._run_once())
        await asyncio.sleep(0.1)
        socket.incoming.put_nowait(json.dumps(
            {"type": "query", "request_id": "q1", "paths": ["src/a.py"], "reason": "核对"}))
        for _ in range(100):
            if socket.frames("query_result"):
                break
            await asyncio.sleep(0.02)
        client.stop()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
    results = socket.frames("query_result")
    assert results[0]["request_id"] == "q1"
    assert results[0]["files"]["src/a.py"]["size"] == 12


def test_fatal_close_stops_reconnect(tmp_path):
    socket = FakeSocket([HELLO_OK], raise_after=2)  # hello 之后的第一个 event 就抛 4401
    client = make_client(socket, tmp_path)
    client.enqueue(envelope("evt-0001-abcd"))

    async def scenario():
        await asyncio.wait_for(client.run_forever(), timeout=10)

    asyncio.run(scenario())
    assert client.fatal is not None
    assert client.fatal["code"] == 4401
    assert "4401" in client.fatal["reason"]


def test_hello_error_frame_is_fatal(tmp_path):
    socket = FakeSocket([{"type": "error", "code": "device_revoked", "message": "该设备已被吊销"}])
    client = make_client(socket, tmp_path)

    async def scenario():
        await asyncio.wait_for(client.run_forever(), timeout=10)

    asyncio.run(scenario())
    assert client.fatal is not None
    assert "吊销" in client.fatal["reason"]


# ------------------------------------------------------------------ 配置


def test_config_roundtrip_and_env_override(tmp_path, monkeypatch):
    target = tmp_path / "edge.json"
    cfg = client_config.EdgeConfig(
        url="wss://teamflow.example.com/api/edge/ws",
        token="tfd_abcdefghijklmnop",
        team="default",
        device_name="linwei-mbp",
        heartbeat=30,
    )
    client_config.save_config(cfg, target)
    loaded = client_config.load_config(target, env={})
    assert loaded.url == cfg.url
    assert loaded.token == cfg.token
    assert loaded.heartbeat == 30
    assert loaded.device()["name"] == "linwei-mbp"

    overridden = client_config.load_config(
        target, env={"TEAMFLOW_EDGE_URL": "wss://other/api/edge/ws", "TEAMFLOW_EDGE_WINDOW": "8"})
    assert overridden.url == "wss://other/api/edge/ws"
    assert overridden.window == 8
    assert overridden.token == cfg.token


def test_config_masked_and_describe(tmp_path):
    cfg = client_config.EdgeConfig(url="ws://127.0.0.1:8000/api/edge/ws", token="tfd_1234567890")
    masked = cfg.masked()
    assert masked["token"].startswith("tfd_")
    assert "1234567890" not in masked["token"]
    text = client_config.describe(cfg)
    assert "127.0.0.1" in text and "tfd_" in text


@pytest.mark.parametrize(
    "kwargs, expected",
    [
        ({}, "缺少"),
        ({"url": "http://x", "token": "t"}, "ws://"),
        ({"url": "ws://x", "token": "t", "heartbeat": 1}, "heartbeat"),
        ({"url": "ws://x", "token": "t", "queue_limit": 0}, "queue_limit"),
        ({"url": "ws://x", "token": "t", "window": 99}, "window"),
    ],
)
def test_config_ensure_ready_errors(kwargs, expected):
    cfg = client_config.EdgeConfig(**kwargs)
    with pytest.raises(client_config.ConfigError) as excinfo:
        cfg.ensure_ready()
    assert expected in str(excinfo.value)


def test_config_client_kwargs(tmp_path):
    cfg = client_config.EdgeConfig(
        url="ws://127.0.0.1:8000/api/edge/ws", token="tfd_x", team="default",
        device_name="box", platform="linux", spool_dir=str(tmp_path), heartbeat=25, window=2)
    kwargs = cfg.client_kwargs()
    assert kwargs["device"] == {"name": "box", "platform": "linux"}
    assert kwargs["spool_path"] == tmp_path / "events.jsonl"
    assert kwargs["heartbeat"] == 25 and kwargs["window"] == 2


# ------------------------------------------------------------------ REST 兜底


def test_ws_to_http():
    assert client_api.RestFallback.ws_to_http("wss://host/api/edge/ws") == "https://host"
    assert client_api.RestFallback.ws_to_http("ws://127.0.0.1:8000/api/edge/ws") == "http://127.0.0.1:8000"
    assert client_api.RestFallback.ws_to_http("http://host") == "http://host"


def test_rest_accepts_ws_base_url():
    rest = client_api.RestFallback("ws://127.0.0.1:8171/api/edge/ws", "tfd_x", "default")
    assert rest.base_url == "http://127.0.0.1:8171"
    assert rest.url == "http://127.0.0.1:8171/api/teams/default/engine/edge/events"
    assert rest.ws_url == "ws://127.0.0.1:8171/api/edge/ws"


def test_rest_duplicate_is_not_an_error(monkeypatch):
    def fake_request(url, payload, *, timeout=0, headers=None):
        return {"ok": False, "status": "duplicate", "message": "事件已存在", "event_id": 3}

    monkeypatch.setattr(client_api, "_json_request", fake_request)
    rest = client_api.RestFallback("https://host", "tfd_x", "default")
    data = rest.post_event(envelope())
    assert data["status"] == "duplicate" and data["event_id"] == 3


def test_rest_post_event(monkeypatch):
    seen: dict = {}

    def fake_request(url, payload, *, timeout=0, headers=None):
        seen.update({"url": url, "payload": payload})
        return {"ok": True, "status": "stored", "event_id": 3, "job_id": 1, "issue_number": 2}

    monkeypatch.setattr(client_api, "_json_request", fake_request)
    rest = client_api.RestFallback("https://host", "tfd_x", "default")
    data = rest.post_event(envelope())
    assert data["status"] == "stored" and data["event_id"] == 3
    assert seen["url"] == "https://host/api/teams/default/engine/edge/events"
    assert seen["payload"]["token"] == "tfd_x"
    assert seen["payload"]["envelope"]["event_uid"] == "evt-0001-abcd"


def test_rest_post_event_surfaces_rejection(monkeypatch):
    def fake_request(url, payload, *, timeout=0, headers=None):
        return {"ok": False, "status": "rejected", "message": "事件包过大：600000 字节，上限 524288 字节"}

    monkeypatch.setattr(client_api, "_json_request", fake_request)
    rest = client_api.RestFallback("https://host", "tfd_x", "default")
    data = rest.post_event(envelope())
    assert data["status"] == "rejected"
    assert "过大" in data["message"]


def test_fallback_from_config(tmp_path):
    cfg = client_config.EdgeConfig(url="wss://host/api/edge/ws", token="tfd_x", team="default")
    rest = client_api.fallback_from_config(cfg)
    assert rest is not None and rest.url == "https://host/api/teams/default/engine/edge/events"
    assert client_api.fallback_from_config(client_config.EdgeConfig()) is None
    no_team = client_config.EdgeConfig(url="wss://host/api/edge/ws", token="tfd_x")
    assert client_api.fallback_from_config(no_team) is None


def test_spool_summary(tmp_path):
    queue = sdk.SpoolQueue(tmp_path / "events.jsonl", limit=10)
    queue.push(envelope("evt-0001-abcd"))
    summary = client_api.spool_summary(tmp_path / "events.jsonl")
    assert summary["exists"] and summary["count"] == 1
    assert client_api.spool_summary(tmp_path / "missing.jsonl")["count"] == 0


# ------------------------------------------------------------------ CLI


def test_cli_version_and_help(capsys):
    from client import main as client_main

    assert client_main.main(["--version"]) == 0
    assert "teamflow-edge-sdk" in capsys.readouterr().out
    assert client_main.main([]) == 0
    assert "status" in capsys.readouterr().out


def test_cli_set_and_status(tmp_path, capsys):
    from client import main as client_main

    target = tmp_path / "edge.json"
    code = client_main.main([
        "--config", str(target), "set",
        "--url", "ws://127.0.0.1:8000/api/edge/ws",
        "--token", "tfd_cli_token",
        "--team", "default",
        "--spool-dir", str(tmp_path),
    ])
    assert code == 0
    capsys.readouterr()
    assert client_main.main(["--config", str(target), "status"]) == 0
    out = capsys.readouterr().out
    assert "127.0.0.1" in out and "离线队列" in out


def test_cli_send_offline_writes_spool(tmp_path, capsys):
    from client import main as client_main

    target = tmp_path / "edge.json"
    client_main.main([
        "--config", str(target), "set",
        "--url", "ws://127.0.0.1:8000/api/edge/ws",
        "--token", "tfd_cli_token",
        "--team", "default",
        "--spool-dir", str(tmp_path),
    ])
    capsys.readouterr()
    code = client_main.main([
        "--config", str(target), "send",
        "--offline", "--branch", "feat/9-cli",
        "--file", "server/app/main.py:5:2",
        "--added-file", "server/tests/test_cli.py",
    ])
    assert code == 0
    queue = sdk.SpoolQueue(tmp_path / "events.jsonl")
    items = queue.pending()
    assert len(items) == 1
    payload = items[0].envelope
    assert payload["repo"]["branch"] == "feat/9-cli"
    assert payload["stats"]["files"] == 2
    assert payload["stats"]["tests_added"] == 1
    assert payload["files"][0]["added"] == 5


# ------------------------------------------------------------------ 反向查询：指定文件

def _sandbox_root(tmp_path) -> Path:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text(
        "import os\napi_key = 'abcdef123456'\nprint(os.name)\n", encoding="utf-8")
    (root / "src" / "blob.bin").write_bytes(b"\x00\x01\x02\x03")
    return root


def test_query_sandbox_stats_named_files(tmp_path):
    root = _sandbox_root(tmp_path)
    sandbox = QuerySandbox(root)
    result = sandbox.answer({"request_id": "q1", "files": ["src/app.py"], "mode": "stat"})
    info = result["files"]["src/app.py"]
    assert info["ok"] is True
    assert info["size"] > 0
    assert info["lines"] == 3
    assert info["lang"] == "python"
    assert len(info["sha256"]) == 64
    assert "content" not in info


def test_query_sandbox_rejects_escape_paths(tmp_path):
    root = _sandbox_root(tmp_path)
    sandbox = QuerySandbox(root)
    result = sandbox.answer({
        "files": ["/etc/passwd", "../outside.py", "C:/Windows/win.ini", "src/../../x.py"],
        "mode": "stat",
    })
    for path in ("/etc/passwd", "../outside.py", "C:/Windows/win.ini", "src/../../x.py"):
        assert result["files"][path]["ok"] is False
        assert result["files"][path]["error"]


def test_query_sandbox_read_needs_permission(tmp_path):
    root = _sandbox_root(tmp_path)
    denied = QuerySandbox(root, allow_content=False).answer(
        {"files": ["src/app.py"], "mode": "read"})
    assert denied["files"]["src/app.py"]["content_allowed"] is False
    assert "content" not in denied["files"]["src/app.py"]

    allowed = QuerySandbox(root, allow_content=True).answer(
        {"files": ["src/app.py"], "mode": "read"})
    info = allowed["files"]["src/app.py"]
    assert "print(os.name)" in info["content"]
    assert "abcdef123456" not in info["content"]  # 赋值式密钥被替换
    assert info["redactions"]["assign"] >= 1


def test_query_sandbox_read_refuses_binary(tmp_path):
    root = _sandbox_root(tmp_path)
    result = QuerySandbox(root, allow_content=True).answer(
        {"files": ["src/blob.bin"], "mode": "read"})
    info = result["files"]["src/blob.bin"]
    assert "content" not in info
    assert info["error"]


def test_query_sandbox_caps_file_count(tmp_path):
    root = _sandbox_root(tmp_path)
    sandbox = QuerySandbox(root, max_files=2)
    result = sandbox.answer({"files": ["src/app.py", "src/blob.bin", "src/none.py"]})
    assert len(result["files"]) == 2
    assert "最多" in result["error"]


def test_redact_text_masks_known_secrets():
    text, counts = redact_text("k=sk-abcdefghijklmnop1234 token=abcd1234 AKIAABCDEFGHIJKLMNOP")
    assert "sk-abcdefghijklmnop1234" not in text
    assert "abcd1234" not in text
    assert counts.get("openai_key") == 1
    assert counts.get("aws_key") == 1


def test_query_request_from_frame_normalizes():
    request = QueryRequest.from_frame(
        {"files": "src/a.py", "mode": "weird", "max_bytes": "2048", "reason": "核对"})
    assert request.files == ("src/a.py",)
    assert request.mode == "stat"
    assert request.max_bytes == 2048


def test_query_request_handler_answers_named_files(tmp_path):
    root = _sandbox_root(tmp_path)
    socket = FakeSocket([HELLO_OK])
    client = make_client(socket, tmp_path)
    client.on_query_request = QuerySandbox(root, allow_content=True)

    async def scenario():
        task = asyncio.create_task(client._run_once())
        await asyncio.sleep(0.1)
        socket.incoming.put_nowait(json.dumps({
            "type": "query",
            "request_id": "q9",
            "files": ["src/app.py", "src/missing.py"],
            "mode": "read",
            "max_bytes": 4096,
            "reason": "核对登录实现",
        }))
        for _ in range(150):
            if socket.frames("query_result"):
                break
            await asyncio.sleep(0.02)
        client.stop()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
    results = socket.frames("query_result")
    assert results and results[0]["request_id"] == "q9"
    files = results[0]["files"]
    assert "print(os.name)" in files["src/app.py"]["content"]
    assert files["src/missing.py"]["ok"] is False
