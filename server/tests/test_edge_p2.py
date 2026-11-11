"""P2.5 测试：议题层级（父/子、环、深度、叶子口径）与规划提案（拆分/验收/负责人）。"""

from __future__ import annotations

from app.db import session as dbsession
from app.db.models import Issue, Team
from app.services.inference import proposals


# ------------------------------------------------------------------ 工具

def _create(client, slug: str, **body) -> dict:
    response = client.post(f"/api/teams/{slug}/issues", json=body)
    assert response.status_code == 200, response.text
    return response.json()["issue"]


def _envelope(uid: str = "evt-p2-0001", *, branch: str = "feat/1-split", files: list[dict] | None = None) -> dict:
    payload_files = files if files is not None else [
        {"path": "client/core/watcher.py", "status": "modified", "added": 40, "removed": 5},
        {"path": "client/core/redact.py", "status": "modified", "added": 30, "removed": 2},
        {"path": "server/app/routers/edge.py", "status": "modified", "added": 40, "removed": 2},
        {"path": "server/tests/test_edge_p1.py", "status": "modified", "added": 10, "removed": 1},
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
            "dirty": False,
            "unpushed": False,
        },
        "stats": {
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


def _ingest(client, slug: str, envelope: dict) -> dict:
    token = client.post(
        f"/api/teams/{slug}/engine/edge/pair", json={"name": "拆分机", "platform": "darwin"}
    ).json()["token"]
    response = client.post(
        f"/api/teams/{slug}/engine/edge/events",
        json={"token": token, "envelope": envelope},
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "stored"
    return response.json()


# ------------------------------------------------------------------ 层级

def test_children_count_only_leaves(client, team_slug):
    parent = _create(client, team_slug, title="父议题", body="拆成两块")
    child = _create(client, team_slug, title="子议题 A", parent=parent["number"])
    assert child["parent_id"] == parent["id"]

    detail = client.get(f"/api/teams/{team_slug}/issues/{parent['number']}").json()
    assert [item["number"] for item in detail["children"]] == [child["number"]]
    assert detail["progress"] == {"total": 1, "closed": 0, "percent": 0}

    listing = client.get(f"/api/teams/{team_slug}/issues", params={"state": "all"}).json()
    parents = [item for item in listing["issues"] if item["parent_id"] is None]
    assert parents, "列表应包含顶层议题"
    assert listing["counts"]["parents"] == 1

    only_children = client.get(
        f"/api/teams/{team_slug}/issues", params={"parent": parent["number"]}
    ).json()
    assert [item["number"] for item in only_children["issues"]] == [child["number"]]


def test_child_inherits_parent_milestone(client, team_slug):
    milestones = client.get(f"/api/teams/{team_slug}/milestones").json()["milestones"]
    milestone_id = milestones[0]["id"]
    parent = _create(client, team_slug, title="带里程碑的父议题", milestone_id=milestone_id)
    child = _create(client, team_slug, title="继承里程碑的子议题", parent=parent["number"])
    assert child["milestone_id"] == milestone_id


def test_cycle_is_rejected(client, team_slug):
    parent = _create(client, team_slug, title="环测试父")
    child = _create(client, team_slug, title="环测试子", parent=parent["number"])

    same = client.patch(
        f"/api/teams/{team_slug}/issues/{parent['number']}",
        json={"parent": parent["number"]},
    )
    assert same.status_code == 400
    assert same.json()["code"] == "cycle_detected"

    reverse = client.patch(
        f"/api/teams/{team_slug}/issues/{parent['number']}",
        json={"parent": child["number"]},
    )
    assert reverse.status_code == 400
    assert reverse.json()["code"] == "cycle_detected"


def test_depth_limit_is_rejected(client, team_slug):
    first = _create(client, team_slug, title="第 1 层")
    second = _create(client, team_slug, title="第 2 层", parent=first["number"])
    third = _create(client, team_slug, title="第 3 层", parent=second["number"])

    # 把第 1 层挂到第 3 层下 → 形成一个 3 层链的第 4 层
    response = client.patch(
        f"/api/teams/{team_slug}/issues/{first['number']}",
        json={"parent": third["number"]},
    )
    assert response.status_code == 400
    assert response.json()["code"] in {"depth_exceeded", "cycle_detected"}

    leaves = client.get(f"/api/teams/{team_slug}/issues", params={"parent": "none"}).json()
    assert len(leaves["issues"]) >= 1


def test_parent_guards_close_and_delete(client, team_slug):
    parent = _create(client, team_slug, title="守卫父议题")
    child = _create(client, team_slug, title="守卫子议题", parent=parent["number"])

    blocked = client.patch(
        f"/api/teams/{team_slug}/issues/{parent['number']}", json={"status": "done"}
    )
    assert blocked.status_code == 409
    assert blocked.json()["code"] == "has_children"

    deleted = client.delete(f"/api/teams/{team_slug}/issues/{parent['number']}")
    assert deleted.status_code == 409

    closed_child = client.patch(
        f"/api/teams/{team_slug}/issues/{child['number']}", json={"state": "closed"}
    )
    assert closed_child.status_code == 200
    detail = client.get(f"/api/teams/{team_slug}/issues/{parent['number']}").json()
    assert detail["can_close"] is True

    now_ok = client.patch(
        f"/api/teams/{team_slug}/issues/{parent['number']}", json={"status": "done"}
    )
    assert now_ok.status_code == 200


def test_parent_can_be_promoted_to_top_level(client, team_slug):
    parent = _create(client, team_slug, title="待提升父议题")
    child = _create(client, team_slug, title="待提升子议题", parent=parent["number"])

    response = client.patch(
        f"/api/teams/{team_slug}/issues/{child['number']}", json={"parent": ""}
    )
    assert response.status_code == 200
    assert response.json()["issue"]["parent_id"] is None


def test_member_skills_roundtrip(client, team_slug):
    detail = client.get(f"/api/teams/{team_slug}").json()["team"]
    member = detail["members"][0]
    response = client.patch(
        f"/api/teams/{team_slug}/members/{member['username']}",
        json={"skills": [{"name": "前端", "level": 3}, {"name": "数据库", "level": 2}]},
    )
    assert response.status_code == 200, response.text
    assert [item["name"] for item in response.json()["member"]["skills"]] == ["前端", "数据库"]

    fresh = client.get(f"/api/teams/{team_slug}").json()["team"]
    saved = next(item for item in fresh["members"] if item["username"] == member["username"])
    assert saved["skills"][0]["level"] == 3


# ------------------------------------------------------------------ 提案

def test_split_proposal_apply_and_revert(client, team_slug):
    _ingest(client, team_slug, _envelope())

    response = client.post(
        f"/api/teams/{team_slug}/engine/proposals",
        json={"kind": "split", "issue": 1},
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["created"] is True
    insight = data["insight"]
    assert insight["kind"] == "split"
    assert insight["state"] == "draft"
    assert insight["issue_number"] == 1
    assert 0 < insight["confidence"] <= 0.85
    actions = insight["suggestion"]["actions"]
    assert len(actions) >= 2
    assert all(action["op"] == "create_issue" and action["parent"] == 1 for action in actions)
    assert data["preview"]

    dry = client.post(
        f"/api/teams/{team_slug}/engine/insights/{insight['id']}/apply",
        json={"dry_run": True},
    )
    assert dry.status_code == 200
    assert dry.json()["dry_run"] is True
    assert dry.json()["applied"] == 0
    assert client.get(f"/api/teams/{team_slug}/issues", params={"parent": 1}).json()["issues"] == []

    real = client.post(
        f"/api/teams/{team_slug}/engine/insights/{insight['id']}/apply",
        json={"dry_run": False},
    )
    assert real.status_code == 200, real.text
    body = real.json()
    assert body["applied"] == len(actions)
    created = body["created"]["issues"]
    assert len(created) == len(actions)
    assert body["insight"]["state"] == "confirmed"

    children = client.get(f"/api/teams/{team_slug}/issues", params={"parent": 1}).json()["issues"]
    assert len(children) == len(actions)
    assert all(item["parent_id"] == 1 for item in children)

    repeat = client.post(
        f"/api/teams/{team_slug}/engine/insights/{insight['id']}/apply",
        json={"dry_run": False},
    )
    assert repeat.status_code == 409
    assert repeat.json()["code"] == "bad_state"

    revert = client.post(f"/api/teams/{team_slug}/engine/insights/{insight['id']}/revert", json={})
    assert revert.status_code == 200, revert.text
    assert len(revert.json()["removed"]) == len(actions)
    assert revert.json()["insight"]["state"] == "reverted"
    assert client.get(f"/api/teams/{team_slug}/issues", params={"parent": 1}).json()["issues"] == []

    again = client.post(f"/api/teams/{team_slug}/engine/insights/{insight['id']}/revert", json={})
    assert again.status_code == 409


def test_apply_progress_insight_updates_issue(client, team_slug):
    """进度洞察必须能应用。

    这里曾经断言「进度洞察被 apply 接口拒绝」（400），但前端「预览并应用」按钮
    调的就是这个接口，而且没有第二条确认路径——结果是议题正文与进度永远写不回去，
    用户看到的就是「AI 不会改已有议题」。
    """
    _ingest(client, team_slug, _envelope())
    drain = client.post(f"/api/teams/{team_slug}/engine/jobs/drain", params={"limit": 5})
    assert drain.status_code == 200
    drafts = client.get(
        f"/api/teams/{team_slug}/engine/insights", params={"state": "draft"}
    ).json()["insights"]
    progress = [item for item in drafts if item["kind"] == "progress"]
    assert progress, drafts

    before = client.get(f"/api/teams/{team_slug}/issues/1").json()
    response = client.post(
        f"/api/teams/{team_slug}/engine/insights/{progress[0]['id']}/apply",
        json={"dry_run": False},
    )
    assert response.status_code == 200, response.text
    assert response.json()["applied"] >= 1

    after = client.get(f"/api/teams/{team_slug}/issues/1").json()
    assert "自动进度" in (after["issue"].get("body") or ""), after["issue"]
    assert len(after["comments"]) == len(before["comments"]) + 1, after["comments"]


def test_split_proposal_merges_duplicates(client, team_slug):
    _ingest(client, team_slug, _envelope())
    first = client.post(
        f"/api/teams/{team_slug}/engine/proposals", json={"kind": "split", "issue": 1}
    ).json()
    second = client.post(
        f"/api/teams/{team_slug}/engine/proposals", json={"kind": "split", "issue": 1}
    ).json()
    assert first["insight"]["id"] == second["insight"]["id"]
    events = [item["type"] for item in second["insight"]["events"]]
    assert "created" in events and "merged" in events

    drafts = client.get(
        f"/api/teams/{team_slug}/engine/insights", params={"state": "draft"}
    ).json()["insights"]
    assert len([item for item in drafts if item["kind"] == "split"]) == 1


def test_split_proposal_without_evidence(client, team_slug):
    issue = _create(client, team_slug, title="没有代码事件的议题", body="- [ ] 第一步\n- [ ] 第二步")
    data = client.post(
        f"/api/teams/{team_slug}/engine/proposals",
        json={"kind": "split", "issue": issue["number"]},
    ).json()
    assert data["created"] is True
    assert data["insight"]["confidence"] <= 0.35
    assert data["insight"]["suggestion"]["warnings"]


def test_criteria_proposal_writes_acceptance(client, team_slug):
    milestones = client.get(f"/api/teams/{team_slug}/milestones").json()["milestones"]
    milestone = milestones[0]
    client.patch(
        f"/api/teams/{team_slug}/milestones/{milestone['number']}",
        json={"description": "完成登录改造\n- [ ] 支持邮箱登录\n- [ ] 补集成测试"},
    )

    data = client.post(
        f"/api/teams/{team_slug}/engine/proposals",
        json={"kind": "criteria", "milestone": milestone["number"]},
    ).json()
    assert data["created"] is True
    action = data["insight"]["suggestion"]["actions"][0]
    assert action["op"] == "update_milestone"
    assert "邮箱登录" in action["acceptance"]

    applied = client.post(
        f"/api/teams/{team_slug}/engine/insights/{data['insight']['id']}/apply",
        json={"dry_run": False},
    )
    assert applied.status_code == 200
    fresh = client.get(f"/api/teams/{team_slug}/milestones").json()["milestones"]
    saved = next(item for item in fresh if item["number"] == milestone["number"])
    assert "邮箱登录" in saved["acceptance"]


def test_assignment_scores_prefer_skills(client, team_slug):
    detail = client.get(f"/api/teams/{team_slug}").json()["team"]
    members = detail["members"]
    assert len(members) >= 2
    client.patch(
        f"/api/teams/{team_slug}/members/{members[0]['username']}",
        json={"skills": [{"name": "登录", "level": 3}]},
    )
    client.patch(
        f"/api/teams/{team_slug}/members/{members[1]['username']}",
        json={"skills": []},
    )
    label = client.post(
        f"/api/teams/{team_slug}/labels", json={"name": "登录", "color": "#0969da"}
    ).json()["label"]
    issue = _create(client, team_slug, title="登录态修复", label_ids=[label["id"]])

    with dbsession.session_scope() as session:
        team = session.query(Team).filter(Team.slug == team_slug).one()
        row = session.get(Issue, issue["id"])
        scores = proposals.assignment_scores(session, team, row)
    assert scores
    assert scores[0]["score"] > 0
    assert scores[0]["id"] == members[0]["id"]


def test_proposal_kind_is_validated(client, team_slug):
    response = client.post(
        f"/api/teams/{team_slug}/engine/proposals", json={"kind": "nope", "issue": 1}
    )
    assert response.status_code == 400
