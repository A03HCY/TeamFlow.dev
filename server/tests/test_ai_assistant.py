"""AI 助手（常驻对话框）与「新创项目组」的接口测试。"""
from __future__ import annotations

import json

import pytest

from app.db import session as dbsession
from app.services import assistant
from app.services import settings as settings_service

DRAFT = {
    "name": "配色协作平台",
    "description": "让设计师共享配色方案",
    "milestones": [
        {
            "title": "配色库上线",
            "kind": "scheduled",
            "start_date": "2026-12-10",
            "due_date": "2026-12-01",
            "acceptance": ["- [ ] 支持创建配色方案", "支持导出 CSS 变量"],
        }
    ],
    "issues": [
        {"title": "实现配色数据模型", "body": "Palette 表", "priority": "urgent",
         "estimate_hours": 200, "milestone": 9, "labels": ["后端", "不存在的标签"]},
        {"title": "", "priority": "low"},
    ],
}


def _configure(session) -> None:
    settings_service.set_setting(session, "ai_base_url", "http://127.0.0.1:9/v1")
    settings_service.set_setting(session, "ai_model", "test-model")
    settings_service.set_setting(session, "ai_api_key", "sk-test")


@pytest.fixture()
def fake_ai(monkeypatch):
    calls: list[list[dict]] = []

    def fake_post(cfg, messages, *, temperature, max_tokens):
        calls.append(messages)
        if "JSON" in messages[0]["content"]:
            return "```json\n" + json.dumps(DRAFT, ensure_ascii=False) + "\n```"
        return "先关闭登录议题，再补测试。"

    monkeypatch.setattr(assistant, "_post", fake_post)
    return calls


def test_status_requires_config(client, team_slug):
    body = client.get(f"/api/teams/{team_slug}/ai").json()
    assert body["configured"] is False
    assert body["quick_prompts"]

    response = client.post(
        f"/api/teams/{team_slug}/ai/chat",
        json={"messages": [{"role": "user", "content": "进度如何"}]},
    )
    assert response.status_code == 400
    assert response.json().get("code") == "ai_unconfigured"


def test_chat_roundtrip(client, team_slug, fake_ai):
    with dbsession.session_scope() as session:
        _configure(session)

    body = client.get(f"/api/teams/{team_slug}/ai").json()
    assert body["configured"] is True
    assert body["model"] == "test-model"

    response = client.post(
        f"/api/teams/{team_slug}/ai/chat",
        json={"messages": [{"role": "user", "content": "进度如何"}]},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["reply"] == "先关闭登录议题，再补测试。"
    # 系统提示里带上了团队快照
    assert "团队数据快照" in fake_ai[0][0]["content"]
    assert "议题" in fake_ai[0][0]["content"]


def test_chat_rejects_empty_messages(client, team_slug, fake_ai):
    with dbsession.session_scope() as session:
        _configure(session)
    response = client.post(f"/api/teams/{team_slug}/ai/chat", json={"messages": []})
    assert response.status_code == 400
    assert response.json()["error"]


def test_draft_is_normalized(client, fake_ai):
    with dbsession.session_scope() as session:
        _configure(session)

    response = client.post("/api/ai/project/draft", json={"goal": "做一个配色协作平台"})
    assert response.status_code == 200
    draft = response.json()["draft"]
    assert draft["name"] == "配色协作平台"
    milestone = draft["milestones"][0]
    # 日期颠倒会被交换，列表型验收标准统一成复选框
    assert milestone["start_date"] == "2026-12-01"
    assert milestone["due_date"] == "2026-12-10"
    assert milestone["acceptance"] == "- [ ] 支持创建配色方案\n- [ ] 支持导出 CSS 变量"
    assert any("颠倒" in item for item in draft["warnings"])
    issue = draft["issues"][0]
    assert issue["priority"] == "medium"          # 非法优先级回落到 medium
    assert issue["estimate_hours"] == 80          # 工时夹在 0–80
    assert issue["milestone"] == 0                # 越界的里程碑序号归 0
    assert len(draft["issues"]) == 1              # 空标题被丢弃


def test_draft_requires_goal(client, fake_ai):
    with dbsession.session_scope() as session:
        _configure(session)
    response = client.post("/api/ai/project/draft", json={"goal": "x"})
    assert response.status_code == 400


def test_project_create_needs_login_mode(client, team_slug, fake_ai):
    with dbsession.session_scope() as session:
        _configure(session)
    draft = client.post("/api/ai/project/draft", json={"goal": "做一个配色协作平台"}).json()["draft"]
    response = client.post("/api/ai/project/create", json={"draft": draft})
    assert response.status_code == 403


def test_create_project_service(client, team_slug):
    """直接调服务层：团队、里程碑、议题与标签一次建好，编号从 1 开始。"""
    import copy

    raw = copy.deepcopy(DRAFT)
    raw["issues"][0]["milestone"] = 1
    draft = assistant.normalize_draft(raw)
    with dbsession.session_scope() as session:
        result = assistant.create_project(session, draft)
        team = result["team"]
        assert team.slug
        assert result["milestones"] == [1]
        assert result["issues"] == [1]

    from app.db.models import Issue, Label, Milestone
    from sqlalchemy import select

    with dbsession.session_scope() as session:
        milestones = list(session.scalars(
            select(Milestone).where(Milestone.team_id == team.id)
        ))
        assert len(milestones) == 1
        assert "CSS 变量" in milestones[0].acceptance
        issue = session.scalar(select(Issue).where(Issue.team_id == team.id))
        assert issue is not None
        assert issue.number == 1
        assert issue.milestone_id == milestones[0].id
        assert issue.estimate_hours == 80
        names = {row.name for row in session.scalars(
            select(Label).where(Label.team_id == team.id)
        )}
        assert "后端" in names and "不存在的标签" in names


def test_team_brief_mentions_members_and_counts(client, team_slug):
    with dbsession.session_scope() as session:
        from app.services import teams as team_service

        team = team_service.find_by_slug(session, team_slug)
        brief = assistant.team_brief(session, team)
    assert "团队：" in brief
    assert "议题（只统计叶子议题）" in brief
    assert "成员：" in brief


# ---------------------------------------------------------------- AI 工作台会话


def test_workspace_chat_persists_session(client, team_slug, fake_ai):
    with dbsession.session_scope() as session:
        _configure(session)

    first = client.post(
        f"/api/teams/{team_slug}/ai/chat",
        json={"message": "里程碑怎么排？", "mode": "plan", "context": [1, "#2", "x"]},
    )
    assert first.status_code == 200
    payload = first.json()
    assert payload["session_id"]
    assert payload["title"] == "里程碑怎么排？"
    assert payload["mode"] == "plan"
    assert payload["context"] == [1, 2]
    system = fake_ai[0][0]["content"]
    assert "偏重规划" in system
    assert "用户点名要看的议题" in system

    second = client.post(
        f"/api/teams/{team_slug}/ai/chat",
        json={"session_id": payload["session_id"], "message": "本周先做什么？"},
    )
    assert second.status_code == 200
    assert second.json()["session_id"] == payload["session_id"]
    # 第二轮带上了历史：system + 上一轮 user/assistant + 新 user
    assert len(fake_ai[1]) == 4
    assert fake_ai[1][1]["content"] == "里程碑怎么排？"

    listing = client.get(f"/api/teams/{team_slug}/ai/sessions").json()
    assert len(listing["sessions"]) == 1
    row = listing["sessions"][0]
    assert row["message_count"] == 4
    assert row["mode_label"] == "规划"
    assert [item["value"] for item in listing["modes"]] == ["ask", "plan", "review"]

    detail = client.get(f"/api/teams/{team_slug}/ai/sessions/{row['id']}").json()
    assert [m["role"] for m in detail["messages"]] == [
        "user", "assistant", "user", "assistant",
    ]
    assert detail["messages"][0]["context"] == [1, 2]

    assert client.delete(f"/api/teams/{team_slug}/ai/sessions/{row['id']}").json()["ok"] is True
    assert client.get(f"/api/teams/{team_slug}/ai/sessions").json()["sessions"] == []
    missing = client.get(f"/api/teams/{team_slug}/ai/sessions/{row['id']}")
    assert missing.status_code == 404
    assert missing.json()["code"] == "ai_session_missing"


def test_workspace_chat_requires_message(client, team_slug, fake_ai):
    with dbsession.session_scope() as session:
        _configure(session)
    response = client.post(f"/api/teams/{team_slug}/ai/chat", json={})
    assert response.status_code == 400
    assert response.json()["code"] == "ai_bad_request"


def test_workspace_session_can_be_created_empty(client, team_slug):
    created = client.post(f"/api/teams/{team_slug}/ai/sessions", json={"title": "新对话"})
    assert created.status_code == 200
    row = created.json()["session"]
    assert row["title"] == "新对话"
    assert row["message_count"] == 0
    detail = client.get(f"/api/teams/{team_slug}/ai/sessions/{row['id']}").json()
    assert detail["messages"] == []
