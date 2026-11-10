"""P0 地基测试：建表、审计哈希链、事件包校验、引擎配置、hooks、端侧 SDK。"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest
from sqlalchemy import inspect, text

from app.db import session as dbsession
from app.services import engine
from app.services.engine import agents as engine_agents
from app.services.engine import structured as engine_structured
from app.services.engine.errors import (
    EngineDisabled,
    EngineNotConfigured,
    EngineUnavailable,
    EventRejected,
)
from app.services.inference import audit, events

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from client.core import network as sdk  # noqa: E402

ENGINE_TABLES = (
    "devices",
    "repo_bindings",
    "code_events",
    "inference_jobs",
    "insights",
    "insight_events",
    "team_engine",
    "integration_credentials",
    "inference_audit",
)


def _team_id(client) -> int:
    return int(client.get("/api/teams").json()["teams"][0]["id"])


def _valid_envelope(**overrides) -> dict:
    payload = {
        "schema_version": 1,
        "event_uid": "evt-20261110-abcdef12",
        "kind": "save",
        "ts": "2026-11-10T14:20:31Z",
        "trigger": "save",
        "device": {"name": "linwei-mbp", "platform": "darwin", "agent_version": "0.1.0"},
        "repo": {"label": "teamflow", "path_hash": "a" * 16, "branch": "feat/engine", "head": "b" * 40},
        "stats": {"files": 2, "added": 42, "removed": 7, "tests_added": 1},
        "files": [
            {"path": "server/app/main.py", "status": "modified", "added": 30, "removed": 5},
            {"path": "server/tests/test_api.py", "status": "modified", "added": 12, "removed": 2},
        ],
        "redactions": 1,
        "redacted": True,
        "notes": "本地保存触发",
    }
    payload.update(overrides)
    return payload


# ------------------------------------------------------------------ 建表

def test_engine_tables_created(client):
    names = set(inspect(dbsession.get_engine()).get_table_names())
    missing = [name for name in ENGINE_TABLES if name not in names]
    assert not missing, f"缺少引擎表：{missing}"


# ------------------------------------------------------------------ 审计链

def test_audit_chain_append_and_verify(client):
    team_id = _team_id(client)
    with dbsession.session_scope() as session:
        audit.append(session, team_id=team_id, action="insight.created",
                     subject_type="insight", subject_id="1", payload={"n": 1},
                     actor=(None, "访客"))
        audit.append(session, team_id=team_id, action="insight.confirmed",
                     subject_type="insight", subject_id="1", payload={"n": 2},
                     actor=(None, "访客"))
        audit.append(session, team_id=team_id, action="engine.configured",
                     subject_type="team", subject_id=str(team_id), payload={"model": "m"})
    with dbsession.session_scope() as session:
        report = audit.verify(session)
    assert report["ok"] is True
    assert report["checked"] == 3
    assert report["teams"] == 1


def test_audit_chain_detects_tamper(client):
    team_id = _team_id(client)
    with dbsession.session_scope() as session:
        audit.append(session, team_id=team_id, action="insight.created",
                     subject_type="insight", subject_id="1", payload={"confidence": 0.2})
        audit.append(session, team_id=team_id, action="insight.confirmed",
                     subject_type="insight", subject_id="1", payload={"confidence": 0.2})
    with dbsession.session_scope() as session:
        session.execute(text("UPDATE inference_audit SET payload='{\"confidence\": 0.99}' "
                             "WHERE id = (SELECT MIN(id) FROM inference_audit)"))
    with dbsession.session_scope() as session:
        report = audit.verify(session, team_id=team_id)
    assert report["ok"] is False
    assert report["broken_at"]["team_id"] == team_id
    assert "hash" in report["reason"]


def test_audit_chain_isolated_per_team(client):
    teams = client.get("/api/teams").json()["teams"]
    first, second = int(teams[0]["id"]), int(teams[1]["id"])
    with dbsession.session_scope() as session:
        audit.append(session, team_id=first, action="insight.created", payload={"t": 1})
        audit.append(session, team_id=second, action="insight.created", payload={"t": 2})
    with dbsession.session_scope() as session:
        rows = session.execute(
            text("SELECT team_id, prev_hash FROM inference_audit ORDER BY id")
        ).fetchall()
    assert [row[0] for row in rows] == [first, second]
    assert all(row[1] == audit.GENESIS_HASH for row in rows), "两个团队应各有独立的链首"


# ------------------------------------------------------------------ 事件包

def test_envelope_accepts_valid_payload():
    envelope = events.parse_envelope(_valid_envelope())
    assert envelope.event_uid == "evt-20261110-abcdef12"
    assert envelope.repo.path_hash == "a" * 16
    summary = events.summary(envelope)
    assert summary["stats"]["added"] == 42
    assert len(summary["files"]) == 2


@pytest.mark.parametrize(
    "patch, reason",
    [
        ({"schema_version": 2}, "schema_version"),
        ({"event_uid": "short"}, "event_uid"),
        ({"kind": "deploy"}, "kind"),
        ({"ts": "昨天"}, "ts"),
        ({"files": [{"path": "/etc/passwd"}]}, "相对路径"),
        ({"files": [{"path": "../secrets/.env"}]}, "上级目录"),
        ({"files": [{"path": "C:/Windows/win.ini"}]}, "绝对路径"),
        ({"files": [{"path": "src/a.py", "status": "weird"}]}, "文件状态"),
        ({"repo": {"path_hash": "zz"}}, "path_hash"),
    ],
)
def test_envelope_rejects_invalid_payload(patch, reason):
    payload = _valid_envelope()
    payload.update(patch)
    with pytest.raises(EventRejected) as excinfo:
        events.parse_envelope(payload)
    assert reason in str(excinfo.value)


def test_envelope_rejects_secret_leak():
    payload = _valid_envelope(notes="顺手把 key 贴进来了 sk-abcdefghijklmnopqrstuvwxyz")
    with pytest.raises(EventRejected) as excinfo:
        events.parse_envelope(payload)
    assert "密钥" in str(excinfo.value)


def test_envelope_rejects_oversize():
    payload = _valid_envelope(notes="x" * 5000)
    with pytest.raises(EventRejected):
        events.parse_envelope(json.dumps(payload))
    huge = _valid_envelope()
    huge["files"] = [{"path": f"src/f{index}.py"} for index in range(401)]
    with pytest.raises(EventRejected) as excinfo:
        events.parse_envelope(huge)
    assert "文件数" in str(excinfo.value)


def test_sdk_event_roundtrips_through_server_schema():
    """端侧 SDK 造的事件包必须能被服务端直接受理（协议一致性）。"""
    envelope = sdk.build_event(
        event_uid=sdk.new_event_uid("probe"),
        repo={"label": "teamflow", "path_hash": "c" * 16, "branch": "main"},
        files=[{"path": "server/app/main.py", "status": "modified", "added": 3, "removed": 1}],
        stats={"files": 1, "added": 3, "removed": 1},
        device={"name": "测试机", "platform": "linux"},
    )
    parsed = events.parse_envelope(envelope)
    assert parsed.event_uid == envelope["event_uid"]
    assert parsed.files[0].path == "server/app/main.py"


# ------------------------------------------------------------------ 引擎配置

def test_engine_config_roundtrip(client):
    team_id = _team_id(client)
    with dbsession.session_scope() as session:
        saved = engine.save_engine_config(session, team_id, {
            "enabled": True,
            "base_url": "https://api.example.com/v1/",
            "model": "deepseek-chat",
            "api_key": "sk-test-123456",
            "confidence_threshold": 0.75,
        })
    assert saved.base_url == "https://api.example.com/v1"
    assert saved.enabled is True
    assert saved.api_key == "sk-test-123456"
    assert saved.configured is True
    assert saved.source == "team"
    assert saved.confidence_threshold == pytest.approx(0.75)

    described = saved.describe()
    assert "sk-test-123456" not in json.dumps(described, ensure_ascii=False)
    assert described["configured"] is True

    with dbsession.session_scope() as session:
        reloaded = engine.load_engine_config(session, team_id)
    assert reloaded.model == "deepseek-chat"
    assert reloaded.api_key == "sk-test-123456"


def test_engine_config_env_beats_table(client, monkeypatch):
    team_id = _team_id(client)
    with dbsession.session_scope() as session:
        engine.save_engine_config(session, team_id, {
            "base_url": "https://table.example.com",
            "model": "table-model",
            "api_key": "table-key",
        })
    monkeypatch.setenv("TEAMFLOW_ENGINE_BASE_URL", "https://env.example.com")
    monkeypatch.setenv("TEAMFLOW_ENGINE_MODEL", "env-model")
    with dbsession.session_scope() as session:
        cfg = engine.load_engine_config(session, team_id)
    assert cfg.base_url == "https://env.example.com"
    assert cfg.model == "env-model"
    assert cfg.source == "env"


def test_engine_config_validates_input(client):
    from app.errors import ApiError

    team_id = _team_id(client)
    with pytest.raises(ApiError):
        with dbsession.session_scope() as session:
            engine.save_engine_config(session, team_id, {"base_url": "ftp://nope"})
    with pytest.raises(ApiError):
        with dbsession.session_scope() as session:
            engine.save_engine_config(session, team_id, {"confidence_threshold": 1.5})


def test_engine_config_guards(client):
    cfg = engine.EngineConfig(
        team_id=1, enabled=False, base_url="", api_key="", model="",
        embedding_model="hashing", confidence_threshold=0.6, approval_timeout=3600,
        budget_usd=0.05, budget_tokens=200_000, budget_seconds=120.0,
        dry_run=True, auto_writeback=False,
    )
    with pytest.raises(EngineNotConfigured):
        cfg.require_configured()
    configured = engine.EngineConfig(**{**cfg.__dict__, "base_url": "https://x", "api_key": "k",
                                        "model": "m"})
    with pytest.raises(EngineDisabled):
        configured.require_enabled()


# ------------------------------------------------------------------ orchify 适配

def _fake_cfg(client, **overrides) -> engine.EngineConfig:
    base = dict(
        team_id=_team_id(client), enabled=True, base_url="https://api.example.com/v1",
        api_key="sk-test", model="fake-model", embedding_model="hashing",
        confidence_threshold=0.6, approval_timeout=3600, budget_usd=0.05,
        budget_tokens=200_000, budget_seconds=120.0, dry_run=True, auto_writeback=False,
    )
    base.update(overrides)
    return engine.EngineConfig(**base)


def test_agents_are_built_with_budget_and_json_flags(client):
    from orchify.testing import FakeLLM

    llm = FakeLLM([[{"final": {"content": "{}", "reasoning": ""}}]])
    built = engine.build_agents(_fake_cfg(client), llm=llm, tag="unit")
    assert set(built.as_dict()) >= {"preprocessor", "analyst", "coordinator", "reporter"}
    assert built.analyst.name.startswith("tf-")
    assert built.analyst.llm_kwargs["json_format"] is True
    assert built.preprocessor.llm_kwargs["json_format"] is False
    budget = built.budget.as_dict()
    assert budget["tokens"] > 0 and budget["usd"] > 0


def test_structured_output_with_fake_llm(client):
    from orchify.testing import FakeLLM, final

    answer = json.dumps({
        "completion": 60,
        "status": "in_progress",
        "confidence": 0.72,
        "summary": "接口层已完成",
        "evidence": [{"type": "file", "ref": "server/app/main.py", "note": "新增路由"}],
        "risks": [],
    }, ensure_ascii=False)
    llm = FakeLLM([[final(answer)]])
    agent = engine.build_agent(_fake_cfg(client), "analyst", llm=llm, tag="unit")
    result = asyncio.run(engine.aask_structured(agent, "分析这次提交", engine.ANALYSIS_SCHEMA))
    assert result["completion"] == 60
    assert result["confidence"] == pytest.approx(0.72)
    assert engine.check_analysis(result, threshold=0.6) == []


def test_analysis_gate_flags_low_confidence_and_done_without_tests():
    weak = {
        "completion": 100, "status": "done", "confidence": 0.8,
        "summary": "看起来写完了", "evidence": [],
        "risks": [],
    }
    problems = engine.check_analysis(weak, threshold=0.6)
    assert any("置信度" in item for item in problems)
    assert any("测试" in item for item in problems)


def test_hooks_install_and_uninstall(client):
    seen: list[str] = []

    def listener(event):
        seen.append(event.type)

    before = len(engine.describe_hooks())
    binding = engine.install_hook("run:start", listener)
    assert len(engine.describe_hooks()) == before + 1
    assert binding is not None
    assert engine.uninstall_hook(binding) is True
    assert len(engine.describe_hooks()) == before
    assert seen == []


def test_hook_event_normalization():
    from orchify.event import BaseEvent

    raw = BaseEvent("tf-1-analyst", "abc", "turn-1", "run:start",
                    payload={"status": "running"}, run_id="tf-1-analyst#abc")
    hook_event = engine.hooks.normalize(raw)
    assert hook_event.type == "run:start"
    assert hook_event.run_id == "tf-1-analyst#abc"
    assert hook_event.detail == {"status": "running"}


def test_engine_bootstrap_reports_state(client):
    info = engine.bootstrap()
    assert info["available"] is True
    assert "paths" in info and info["paths"]["root"]
    ok, reason = engine.available()
    assert ok is True and reason == ""


def test_engine_disabled_has_no_side_effects(client, monkeypatch, tmp_path):
    """TEAMFLOW_ENGINE=0 时不建目录、不加载 orchify（P0 验收项）。"""
    target = tmp_path / "engine-off"
    monkeypatch.setenv("TEAMFLOW_ENGINE", "0")
    monkeypatch.setenv("TEAMFLOW_ENGINE_DIR", str(target))
    ok, reason = engine.available()
    assert ok is False
    assert "关闭" in reason
    info = engine.bootstrap()
    assert info == {"available": False, "reason": reason}
    assert not target.exists(), "关闭引擎时不应该创建运行目录"


def test_engine_unavailable_when_extra_missing(monkeypatch):
    monkeypatch.setattr(engine, "orchify_module", _raise_missing)

    def _available():
        try:
            engine.orchify_module()
        except EngineUnavailable as exc:
            return False, str(exc)
        return True, ""

    ok, reason = _available()
    assert ok is False
    assert "orchify" in reason


def _raise_missing():
    raise EngineUnavailable("缺少依赖 orchify，请执行 pip install orchify")


# ------------------------------------------------------------------ 端侧 SDK

def test_spool_queue_dedupes_and_caps(tmp_path):
    queue = sdk.SpoolQueue(tmp_path / "events.jsonl", limit=3)
    assert queue.push(sdk.build_event(event_uid="evt-1", repo={"path_hash": "a" * 8})) is True
    assert queue.push(sdk.build_event(event_uid="evt-1", repo={"path_hash": "a" * 8})) is False
    for index in range(2, 6):
        queue.push(sdk.build_event(event_uid=f"evt-{index}", repo={"path_hash": "a" * 8}))
    assert len(queue) == 3
    assert [item.event_uid for item in queue.pending()] == ["evt-3", "evt-4", "evt-5"]
    # 重启后仍能读回
    reloaded = sdk.SpoolQueue(tmp_path / "events.jsonl", limit=3)
    assert [item.event_uid for item in reloaded.pending()] == ["evt-3", "evt-4", "evt-5"]


class _Closed(Exception):
    pass


_CLOSE = object()


class FakeSocket:
    def __init__(self, replies):
        self.replies = replies
        self.sent: list[dict] = []
        self._inbox: asyncio.Queue = asyncio.Queue()

    async def send(self, data):
        frame = json.loads(data)
        self.sent.append(frame)
        for reply in self.replies(frame):
            item = _CLOSE if reply is _CLOSE else json.dumps(reply, ensure_ascii=False)
            await self._inbox.put(item)

    async def recv(self):
        item = await self._inbox.get()
        if item is _CLOSE:
            raise _Closed()
        return item

    async def close(self):
        await self._inbox.put(_CLOSE)


def test_edge_client_handshake_send_and_ack(tmp_path):
    def replies(frame):
        if frame["type"] == sdk.FRAME_HELLO:
            return [{"type": sdk.FRAME_HELLO_OK, "device_id": 7, "team_id": 1, "server_time": "now"}]
        if frame["type"] == sdk.FRAME_EVENT:
            uid = frame["envelope"]["event_uid"]
            return [{"type": sdk.FRAME_ACK, "event_uid": uid, "status": sdk.ACK_STORED},
                    _CLOSE]
        return []

    socket = FakeSocket(replies)

    async def factory(url, headers):
        return socket

    client = sdk.EdgeClient("ws://127.0.0.1:8123/ws/edge", "pair-token",
                            device={"name": "单元测试"}, spool_path=tmp_path / "q.jsonl",
                            socket_factory=factory, heartbeat=3600)
    client.enqueue(sdk.build_event(event_uid="evt-send-1", repo={"path_hash": "d" * 8},
                                   files=[{"path": "src/a.py", "status": "modified"}]))

    async def scenario():
        with pytest.raises(_Closed):
            await asyncio.wait_for(client._run_once(), timeout=10)

    asyncio.run(scenario())
    assert [frame["type"] for frame in socket.sent] == [sdk.FRAME_HELLO, sdk.FRAME_EVENT]
    assert client.sent == 1
    assert len(client.queue) == 0
    assert client.rejected == 0


def test_edge_client_answers_reverse_query(tmp_path):
    seen: list[tuple[list[str], str]] = []

    def on_query(paths, reason):
        seen.append((paths, reason))
        return {paths[0]: {"sha256": "f" * 64, "size": 12}}

    def replies(frame):
        if frame["type"] == sdk.FRAME_HELLO:
            return [{"type": sdk.FRAME_HELLO_OK, "device_id": 1, "team_id": 1},
                    {"type": sdk.FRAME_QUERY, "request_id": "req-1",
                     "paths": ["src/a.py"], "reason": "需要指纹"},
                    _CLOSE]
        return []

    socket = FakeSocket(replies)

    async def factory(url, headers):
        return socket

    client = sdk.EdgeClient("ws://127.0.0.1:8123/ws/edge", "pair-token",
                            spool_path=tmp_path / "q.jsonl", socket_factory=factory,
                            heartbeat=3600, on_query=on_query)

    async def scenario():
        with pytest.raises(_Closed):
            await asyncio.wait_for(client._run_once(), timeout=10)

    asyncio.run(scenario())
    assert seen == [(["src/a.py"], "需要指纹")]
    result = [frame for frame in socket.sent if frame["type"] == sdk.FRAME_QUERY_RESULT]
    assert result and result[0]["files"]["src/a.py"]["size"] == 12
