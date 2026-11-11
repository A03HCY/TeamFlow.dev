"""P1 端侧接入测试：配对 → WS 上报 → 规则推断 → 收件箱 → 审计。

注意：worker 在测试里是关掉的（conftest 设 ``TEAMFLOW_INFERENCE_WORKER=0``），
每个用例显式调 ``POST /engine/jobs/drain`` 来驱动管线，这样断言与后台线程无关。
"""
from __future__ import annotations

import json

import pytest

from app import config as app_config
from app.services.edge import devices as device_service
from app.services.inference import rules


def _envelope(
    uid: str = "evt-test-0001",
    *,
    branch: str = "feat/1-demo",
    files: list[dict] | None = None,
    unpushed: bool = False,
    stats: dict | None = None,
) -> dict:
    payload_files = files if files is not None else [
        {"path": "src/auth.py", "status": "modified", "added": 120, "removed": 8},
        {"path": "tests/test_auth.py", "status": "added", "added": 40, "removed": 0},
    ]
    return {
        "schema_version": 1,
        "event_uid": uid,
        "kind": "save",
        "ts": "2026-11-10T10:20:00Z",
        "trigger": "save",
        "device": {"name": "mbp", "platform": "darwin", "agent_version": "0.1.0"},
        "repo": {
            "label": "teamflow",
            "path_hash": "9f2c1a77b04e5d63",
            "branch": branch,
            "head": "abc1234def",
            "remote_hash": "9f2c1a77b04e5d63",
            "dirty": True,
            "unpushed": unpushed,
        },
        "stats": stats or {
            "files": len(payload_files),
            "added": 160,
            "removed": 8,
            "tests_added": 1,
            "tests_changed": 0,
            "tests_removed": 0,
            "binary": 0,
        },
        "files": payload_files,
        "redactions": 1,
        "redacted": True,
        "notes": "",
    }


def _pair(client, slug: str, name: str = "测试机") -> dict:
    response = client.post(
        f"/api/teams/{slug}/engine/edge/pair", json={"name": name, "platform": "darwin"}
    )
    assert response.status_code == 200, response.text
    return response.json()


# ------------------------------------------------------------------ 设备配对

def test_pair_creates_pending_device(client, team_slug):
    data = _pair(client, team_slug)
    assert data["token"].startswith("tfd_")
    assert data["device"]["state"] == "pending"
    assert data["device"]["online"] is False

    overview = client.get(f"/api/teams/{team_slug}/engine/edge").json()
    assert len(overview["devices"]) == 1
    assert overview["devices"][0]["name"] == "测试机"
    assert overview["devices"][0]["event_count"] == 0


def test_revoke_device_blocks_handshake(client, team_slug):
    paired = _pair(client, team_slug)
    device_id = paired["device"]["id"]
    revoked = client.delete(f"/api/teams/{team_slug}/engine/edge/devices/{device_id}")
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["device"]["state"] == "revoked"

    with client.websocket_connect("/api/edge/ws") as ws:
        ws.send_json({"type": "hello", "schema": 1, "token": paired["token"]})
        frame = ws.receive_json()
        assert frame["type"] == "error"
        assert "吊销" in frame["message"]


def test_unknown_token_is_rejected(client, team_slug):
    with client.websocket_connect("/api/edge/ws") as ws:
        ws.send_json({"type": "hello", "schema": 1, "token": "tfd_nope"})
        frame = ws.receive_json()
        assert frame["type"] == "error"
        assert "配对码" in frame["message"]


# ------------------------------------------------------------------ WS 上报

def test_ws_event_ingest_is_idempotent(client, team_slug):
    paired = _pair(client, team_slug)
    envelope = _envelope()
    with client.websocket_connect("/api/edge/ws") as ws:
        ws.send_json({"type": "hello", "schema": 1, "token": paired["token"]})
        hello = ws.receive_json()
        assert hello["type"] == "hello_ok"
        assert hello["team_id"] == paired["device"]["team_id"]

        ws.send_json({"type": "event", "envelope": envelope})
        ack = ws.receive_json()
        assert ack["status"] == "stored", ack

        # 同一条再发一次：幂等键命中，回 duplicate 且不重复入库
        ws.send_json({"type": "event", "envelope": envelope})
        again = ws.receive_json()
        assert again["status"] == "duplicate", again

        ws.send_json({"type": "heartbeat", "sent": 1, "queued": 0})

    events = client.get(f"/api/teams/{team_slug}/engine/edge/events").json()["events"]
    assert len(events) == 1
    assert events[0]["event_uid"] == envelope["event_uid"]
    assert events[0]["files"][0]["path"] in {"src/auth.py", "tests/test_auth.py"}

    overview = client.get(f"/api/teams/{team_slug}/engine/edge").json()
    assert overview["devices"][0]["state"] == "offline"  # 连过又断开
    assert overview["devices"][0]["event_count"] == 1


def test_invalid_envelope_is_acked_as_rejected(client, team_slug):
    paired = _pair(client, team_slug)
    broken = _envelope("evt-broken-01")
    broken["files"][0]["path"] = "/etc/passwd"
    with client.websocket_connect("/api/edge/ws") as ws:
        ws.send_json({"type": "hello", "schema": 1, "token": paired["token"]})
        ws.receive_json()
        ws.send_json({"type": "event", "envelope": broken})
        ack = ws.receive_json()
    assert ack["status"] == "rejected"
    assert "相对路径" in ack["message"]


# ------------------------------------------------------------------ 规则推断

def test_drain_creates_insight_with_reasoning(client, team_slug):
    paired = _pair(client, team_slug)
    with client.websocket_connect("/api/edge/ws") as ws:
        ws.send_json({"type": "hello", "schema": 1, "token": paired["token"]})
        ws.receive_json()
        ws.send_json({"type": "event", "envelope": _envelope("evt-insight-01")})
        assert ws.receive_json()["status"] == "stored"

    drained = client.post(f"/api/teams/{team_slug}/engine/jobs/drain?limit=5").json()
    assert drained["processed"] == 1

    data = client.get(f"/api/teams/{team_slug}/engine/insights?state=draft").json()
    assert len(data["insights"]) == 1
    insight = data["insights"][0]
    assert insight["kind"] == "progress"
    assert insight["state"] == "draft"
    assert insight["confidence"] >= 0.75
    assert insight["suggestion"]["status"] == "done"
    assert insight["suggestion"]["dry_run"] is True
    refs = {item["ref"] for item in insight["evidence"]}
    assert "tests/test_auth.py" in refs
    assert insight["events"][0]["type"] == "created"
    # 分支名里的 #1 应该被解析成议题 1
    assert insight["issue_id"] is not None


def test_second_batch_merges_into_same_draft(client, team_slug):
    paired = _pair(client, team_slug)
    for uid in ("evt-merge-001", "evt-merge-002"):
        with client.websocket_connect("/api/edge/ws") as ws:
            ws.send_json({"type": "hello", "schema": 1, "token": paired["token"]})
            ws.receive_json()
            ws.send_json({"type": "event", "envelope": _envelope(uid)})
            ws.receive_json()
        client.post(f"/api/teams/{team_slug}/engine/jobs/drain?limit=5")

    data = client.get(f"/api/teams/{team_slug}/engine/insights?state=draft").json()
    assert len(data["insights"]) == 1, data
    types = [item["type"] for item in data["insights"][0]["events"]]
    assert types == ["created", "merged"]


def test_unpushed_and_no_test_change_do_not_mark_done(client, team_slug):
    paired = _pair(client, team_slug)
    envelope = _envelope(
        "evt-weak-0001",
        files=[{"path": "src/auth.py", "status": "modified", "added": 30, "removed": 2}],
        unpushed=True,
        stats={"files": 1, "added": 30, "removed": 2, "tests_added": 0,
               "tests_changed": 0, "tests_removed": 0, "binary": 0},
    )
    with client.websocket_connect("/api/edge/ws") as ws:
        ws.send_json({"type": "hello", "schema": 1, "token": paired["token"]})
        ws.receive_json()
        ws.send_json({"type": "event", "envelope": envelope})
        ws.receive_json()
    client.post(f"/api/teams/{team_slug}/engine/jobs/drain?limit=5")

    insight = client.get(f"/api/teams/{team_slug}/engine/insights?state=draft").json()["insights"][0]
    assert insight["suggestion"]["status"] == "in_progress"
    assert insight["confidence"] < 0.75


def test_docs_only_event_is_rejected(client, team_slug):
    paired = _pair(client, team_slug)
    envelope = _envelope(
        "evt-docs-0001",
        branch="chore/docs",
        files=[{"path": "README.md", "status": "modified", "added": 20, "removed": 3}],
        stats={"files": 1, "added": 20, "removed": 3, "tests_added": 0,
               "tests_changed": 0, "tests_removed": 0, "binary": 0},
    )
    with client.websocket_connect("/api/edge/ws") as ws:
        ws.send_json({"type": "hello", "schema": 1, "token": paired["token"]})
        ws.receive_json()
        ws.send_json({"type": "event", "envelope": envelope})
        ws.receive_json()

    results = client.post(f"/api/teams/{team_slug}/engine/jobs/drain?limit=5").json()["results"]
    assert results[0]["created"] is False
    # 拒绝理由来自准入闸门：纯文档改动不再靠「置信度不够」这种数字兜住，
    # 而是直接说清「不构成功能进展」，并把结论留在 admission 里便于复盘
    assert "文档" in results[0]["skipped"]
    assert results[0]["admission"]["verdict"] == "skip"
    assert results[0]["admission"]["firm"] is True

    insights = client.get(f"/api/teams/{team_slug}/engine/insights?state=all").json()["insights"]
    assert insights == []


# ------------------------------------------------------------------ 收件箱与审计

def test_reject_insight_and_audit_chain(client, team_slug):
    paired = _pair(client, team_slug)
    with client.websocket_connect("/api/edge/ws") as ws:
        ws.send_json({"type": "hello", "schema": 1, "token": paired["token"]})
        ws.receive_json()
        ws.send_json({"type": "event", "envelope": _envelope("evt-reject-01")})
        ws.receive_json()
    client.post(f"/api/teams/{team_slug}/engine/jobs/drain?limit=5")

    insight = client.get(f"/api/teams/{team_slug}/engine/insights?state=draft").json()["insights"][0]
    rejected = client.post(
        f"/api/teams/{team_slug}/engine/insights/{insight['id']}/reject",
        json={"reason": "证据不足"},
    )
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["insight"]["state"] == "rejected"

    again = client.post(f"/api/teams/{team_slug}/engine/insights/{insight['id']}/reject", json={})
    assert again.status_code == 409

    entries = client.get(f"/api/teams/{team_slug}/engine/audit").json()["entries"]
    actions = [item["action"] for item in entries]
    assert "device.paired" in actions
    assert "repo.bound" in actions
    assert "insight.created" in actions
    assert "insight.rejected" in actions

    verify = client.get(f"/api/teams/{team_slug}/engine/audit/verify").json()
    assert verify["ok"] is True
    assert verify["checked"] == len(entries)


def test_rest_fallback_accepts_event(client, team_slug):
    paired = _pair(client, team_slug)
    response = client.post(
        f"/api/teams/{team_slug}/engine/edge/events",
        json={"token": paired["token"], "envelope": _envelope("evt-rest-0001")},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "stored"
    assert body["job_id"]

    duplicate = client.post(
        f"/api/teams/{team_slug}/engine/edge/events",
        json={"token": paired["token"], "envelope": _envelope("evt-rest-0001")},
    ).json()
    assert duplicate["status"] == "duplicate"


def test_engine_disabled_returns_503(client, team_slug, monkeypatch):
    monkeypatch.setenv("TEAMFLOW_ENGINE", "0")
    response = client.post(f"/api/teams/{team_slug}/engine/edge/pair", json={"name": "x"})
    assert response.status_code == 503
    assert response.json()["code"] == "engine_unavailable"


# ------------------------------------------------------------------ 反向查询：指定文件


def test_query_offline_device_returns_409(client, team_slug):
    pair = client.post(f"/api/teams/{team_slug}/engine/edge/pair", json={"name": "q-mac"})
    device_id = pair.json()["device"]["id"]
    response = client.post(
        f"/api/teams/{team_slug}/engine/edge/devices/{device_id}/query",
        json={"files": ["src/app.py"], "mode": "stat"},
    )
    assert response.status_code == 409
    assert response.json()["code"] == "device_offline"


def test_query_validates_request(client, team_slug):
    pair = client.post(f"/api/teams/{team_slug}/engine/edge/pair", json={"name": "q-mac2"})
    device_id = pair.json()["device"]["id"]
    empty = client.post(
        f"/api/teams/{team_slug}/engine/edge/devices/{device_id}/query", json={"files": []})
    assert empty.status_code == 400 and empty.json()["code"] == "bad_query"

    bad_mode = client.post(
        f"/api/teams/{team_slug}/engine/edge/devices/{device_id}/query",
        json={"files": ["a.py"], "mode": "dump"},
    )
    assert bad_mode.status_code == 400 and bad_mode.json()["code"] == "bad_query"

    too_many = client.post(
        f"/api/teams/{team_slug}/engine/edge/devices/{device_id}/query",
        json={"files": [f"src/f{i}.py" for i in range(30)]},
    )
    assert too_many.status_code == 400

    unknown = client.post(
        f"/api/teams/{team_slug}/engine/edge/devices/9999/query",
        json={"files": ["a.py"]},
    )
    assert unknown.status_code == 404


def test_registry_query_frame_carries_files_and_mode():
    """网关侧：query 帧必须点名 files/mode/max_bytes，并把回包交回 future。"""
    import asyncio
    from app.services.edge.gateway import QUERY_MODE_READ, Connection, ConnectionRegistry

    class _FakeWS:
        def __init__(self):
            self.sent: list[dict] = []

        async def send_json(self, payload):
            self.sent.append(payload)

        async def close(self, code=1000, reason=""):
            return None

    async def scenario():
        registry = ConnectionRegistry()
        ws = _FakeWS()
        connection = Connection(device_id=42, team_id=1, websocket=ws)
        await registry.register(connection)
        task = asyncio.create_task(registry.query(
            42, ["src/a.py", "src/b.py"], mode=QUERY_MODE_READ, max_bytes=2048, reason="核对"))
        for _ in range(50):
            if ws.sent:
                break
            await asyncio.sleep(0.01)
        frame = ws.sent[0]
        assert frame["type"] == "query"
        assert frame["files"] == ["src/a.py", "src/b.py"]
        assert frame["paths"] == ["src/a.py", "src/b.py"]
        assert frame["mode"] == "read"
        assert frame["max_bytes"] == 2048
        registry.resolve_query(connection, frame["request_id"],
                               {"files": {"src/a.py": {"ok": True, "size": 10}}})
        result = await asyncio.wait_for(task, timeout=2)
        assert result["files"]["src/a.py"]["size"] == 10
        registry.unregister(connection)

    asyncio.run(scenario())



def test_branch_issue_number_variants():
    assert rules.branch_issue_number("feat/12-login") == 12
    assert rules.branch_issue_number("fix_45") == 45
    assert rules.branch_issue_number("#7") == 7
    assert rules.branch_issue_number("main") is None
    assert rules.branch_issue_number("") is None


def test_iso_week_and_path_classification():
    assert rules.iso_week("2026-11-10T10:20:00Z") == "2026-W46"
    assert rules.is_test_path("tests/test_auth.py")
    assert rules.is_test_path("src/app.spec.ts")
    assert not rules.is_test_path("src/testing_utils.py")
    assert rules.is_doc_path("docs/README.md")
    assert rules.is_code_path("src/auth.py")
    assert rules.is_sensitive_path(".env.local")


def test_token_hash_never_stores_plaintext():
    token = device_service.new_token()
    assert token.startswith(app_config.ENGINE_DEVICE_TOKEN_PREFIX)
    digest = device_service.hash_token(token)
    assert token not in digest
    assert len(digest) == 64


def test_insight_dict_roundtrip_keeps_json_fields():
    from app.services.inference.pipeline import dedupe_key_for

    key = dedupe_key_for(12, "feat/12-login", "progress")
    assert key.startswith("issue12:")
    assert len(key) <= 160
    assert json.dumps({"dedupe_key": key})  # 仅确认可序列化
