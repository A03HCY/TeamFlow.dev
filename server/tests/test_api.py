"""核心 API 冒烟测试：健康检查、团队、议题、里程碑、标签、日程与周计划。"""

from __future__ import annotations


def test_health(client):
    data = client.get("/api/health").json()
    assert data["ok"] is True
    assert data["app"] == "TeamFlow"
    assert data["version"]


def test_bootstrap(client):
    data = client.get("/api/bootstrap").json()
    assert data["require_login"] in (True, False)
    assert isinstance(data["teams"], list) and data["teams"]
    assert "edit_protected" in data and "edit_locked" in data
    for team in data["teams"]:
        assert {"id", "name", "slug", "issues_public"} <= set(team)


def test_teams_and_detail(client, team_slug):
    listing = client.get("/api/teams").json()["teams"]
    assert any(team["slug"] == team_slug for team in listing)

    team = client.get(f"/api/teams/{team_slug}").json()["team"]
    assert team["slug"] == team_slug
    assert "can_edit" in team and "is_member" in team
    assert set(team["counts"]) == {"total", "open"}
    assert isinstance(team["members"], list)


def test_unknown_team(client):
    response = client.get("/api/teams/does-not-exist/issues")
    assert response.status_code == 404
    assert response.json()["error"]


def test_issue_lifecycle(client, team_slug, new_issue):
    number = new_issue["number"]
    assert new_issue["state"] == "open"

    listing = client.get(f"/api/teams/{team_slug}/issues", params={"state": "all"}).json()
    assert any(item["number"] == number for item in listing["issues"])
    assert set(listing["counts"]) == {"total", "open", "closed", "parents"}

    patched = client.patch(
        f"/api/teams/{team_slug}/issues/{number}",
        json={"status": "in_progress", "priority": "low"},
    ).json()["issue"]
    assert patched["status"] == "in_progress"
    assert patched["priority"] == "low"

    closed = client.patch(f"/api/teams/{team_slug}/issues/{number}", json={"state": "closed"}).json()["issue"]
    assert closed["state"] == "closed"
    assert closed["status"] == "done"
    assert closed["closed_at"]

    comment = client.post(
        f"/api/teams/{team_slug}/issues/{number}/comments",
        json={"body": "第一条评论"},
    ).json()["comment"]
    assert comment["body"] == "第一条评论"

    detail = client.get(f"/api/teams/{team_slug}/issues/{number}").json()
    assert detail["issue"]["number"] == number
    assert len(detail["comments"]) == 1

    assert client.delete(f"/api/teams/{team_slug}/issues/{number}").json()["ok"] is True
    assert client.get(f"/api/teams/{team_slug}/issues/{number}").status_code == 404


def test_issue_filters(client, team_slug):
    empty = client.patch(f"/api/teams/{team_slug}/issues/1", json={})
    assert empty.status_code == 400

    filtered = client.get(
        f"/api/teams/{team_slug}/issues",
        params={"status": "todo", "sort": "number", "state": "all"},
    ).json()
    assert all(item["status"] == "todo" for item in filtered["issues"])

    by_week = client.get(f"/api/teams/{team_slug}/issues", params={"week": "current"}).json()
    assert isinstance(by_week["issues"], list)


def test_milestones(client, team_slug):
    created = client.post(
        f"/api/teams/{team_slug}/milestones",
        json={
            "title": "pytest 里程碑",
            "description": "自动化测试创建",
            "kind": "scheduled",
            "start_date": "2026-01-05",
            "due_date": "2026-02-05",
        },
    ).json()["milestone"]
    number = created["number"]
    assert created["progress"] == 0

    updated = client.patch(
        f"/api/teams/{team_slug}/milestones/{number}",
        json={"title": "pytest 里程碑（改）"},
    ).json()["milestone"]
    assert updated["title"] == "pytest 里程碑（改）"

    listing = client.get(f"/api/teams/{team_slug}/milestones").json()
    assert any(item["number"] == number for item in listing["milestones"])

    assert client.delete(f"/api/teams/{team_slug}/milestones/{number}").json()["ok"] is True


def test_labels(client, team_slug):
    created = client.post(
        f"/api/teams/{team_slug}/labels",
        json={"name": "pytest-label", "color": "#123456"},
    ).json()["label"]
    assert created["color"] == "#123456"

    renamed = client.patch(
        f"/api/teams/{team_slug}/labels/pytest-label",
        json={"name": "pytest-label-2", "color": "#abcdef"},
    ).json()["label"]
    assert renamed["name"] == "pytest-label-2"

    listing = client.get(f"/api/teams/{team_slug}/labels").json()
    assert any(item["name"] == "pytest-label-2" for item in listing["labels"])

    assert client.delete(f"/api/teams/{team_slug}/labels/pytest-label-2").json()["ok"] is True


def test_schedules(client, team_slug):
    team = client.get(f"/api/teams/{team_slug}").json()["team"]
    owner_id = team["members"][0]["id"]
    created = client.post(
        f"/api/teams/{team_slug}/schedules",
        json={
            "title": "pytest 站会",
            "kind": "recurring",
            "weekday": 2,
            "start_time": "10:00",
            "end_time": "10:30",
            "user_id": owner_id,
        },
    ).json()["schedule"]
    assert created["weekday"] == 2
    assert created["user_id"] == owner_id

    listing = client.get(f"/api/teams/{team_slug}/schedules").json()
    assert any(item["id"] == created["id"] for item in listing["schedules"])

    assert client.delete(f"/api/teams/{team_slug}/schedules/{created['id']}").json()["ok"] is True


def test_week_plan(client, team_slug):
    data = client.get(f"/api/teams/{team_slug}/week").json()
    assert data["week"]
    assert isinstance(data["groups"], list)
    assert set(data["stats"]) >= {"planned", "open", "done", "unscheduled"}

    next_week = client.get(f"/api/teams/{team_slug}/week", params={"week": "next"}).json()
    assert next_week["week"] != data["week"]


def test_overview_and_roadmap(client, team_slug):
    overview = client.get(f"/api/teams/{team_slug}/overview").json()
    assert set(overview["stats"]) >= {"total", "open", "closed", "completion"}
    assert isinstance(overview["trend"], list) and len(overview["trend"]) == 14

    roadmap = client.get(f"/api/teams/{team_slug}/roadmap").json()
    assert isinstance(roadmap["milestones"], list)
    assert "can_edit" in roadmap


def test_removed_features_are_gone(client, team_slug):
    for path in ("drive", "drive/items", "meetings", "meetings/rooms", "cloud"):
        response = client.get(f"/api/teams/{team_slug}/{path}")
        assert response.status_code in (404, 405), path


def test_static_index_and_spa_fallback(client):
    index = client.get("/")
    assert index.status_code == 200
    assert "TeamFlow" in index.text

    fallback = client.get(f"/{client.get('/api/teams').json()['teams'][0]['slug']}/overview")
    assert fallback.status_code == 200
    assert "gh-ui" in fallback.text

    assert client.get("/vendor/gh-ui.js").status_code == 200
    assert client.get("/js/app.js").status_code == 200
