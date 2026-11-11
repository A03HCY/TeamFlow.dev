"""洞察链路回归：静默模式的统计不能丢、洞察必须能落地。

两个曾经真实踩过的坑：

1. 客户端静默模式（不传路径）只上行 ``stats``，规则侧却用 ``len(envelope.files)``
   当文件数，洞察写成「0 个文件变化（+2/-0）」；
2. 端侧推断产出的洞察 ``suggestion`` 里没有 ``actions``，「预览并应用」直接报
   「这条提案没有可执行的动作」，于是洞察永远停在待确认。
"""

from __future__ import annotations

from app.services.inference import rules
from app.services.inference.events import parse_envelope

SILENT_ENVELOPE = {
    "schema_version": 1,
    "event_uid": "evt-silent-0001",
    "kind": "snapshot",
    "ts": "2026-11-10T15:00:00Z",
    "trigger": "idle-window",
    "device": {"name": "LAMNING", "platform": "windows", "agent_version": "0.1.0"},
    "repo": {
        "label": "TeamFlow.dev",
        "path_hash": "9f2c1a77b04e5d63",
        "branch": "main",
        "head": "abc1234def",
        "remote_hash": "9f2c1a77b04e5d63",
        "dirty": False,
        "unpushed": False,
    },
    # 静默模式：只有统计，没有任何 files
    "stats": {"files": 3, "added": 7, "removed": 0, "binary": 0},
    "files": [],
    "redactions": 0,
    "redacted": True,
    "notes": "30 分钟无操作，3 个文件改动（+7 / -0 行）",
}


def _ingest_silent(client, slug: str) -> dict:
    token = client.post(
        f"/api/teams/{slug}/engine/edge/pair",
        json={"name": "静默机", "platform": "windows"},
    ).json()["token"]
    response = client.post(
        f"/api/teams/{slug}/engine/edge/events",
        json={"token": token, "envelope": SILENT_ENVELOPE},
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "stored"
    return response.json()


def test_silent_mode_file_count_comes_from_stats():
    """没有 files 时，文件数与增删行要取 stats，而不是当成 0。"""
    envelope = parse_envelope(SILENT_ENVELOPE)
    result = rules.analyze(envelope)

    summary = result["analysis"]["summary"]
    assert "3 个文件变化" in summary, summary
    assert "+7/-0" in summary, summary

    signals = result["signals"]
    assert signals.files == 3
    assert signals.code_files == 3, "静默模式按测试变更反推，剩下的算源码"
    assert "文档" not in result["title"], result["title"]


def test_insight_has_actionable_actions(client, team_slug):
    """端侧推断出的洞察必须带 actions，否则界面上点不动。"""
    _ingest_silent(client, team_slug)
    drained = client.post(f"/api/teams/{team_slug}/engine/jobs/drain", params={"limit": 5})
    assert drained.status_code == 200, drained.text

    listing = client.get(f"/api/teams/{team_slug}/engine/insights").json()
    items = listing.get("insights") or []
    assert items, "作业跑完应该有一条洞察"
    insight_id = items[0]["id"]

    preview = client.post(
        f"/api/teams/{team_slug}/engine/insights/{insight_id}/apply",
        json={"dry_run": True},
    )
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["preview"], "预览里必须有可执行动作"
    assert body["preview"][0]["op"] in {"create_issue", "update_issue"}


def test_unscoped_insight_applies_and_creates_issue(client, team_slug):
    """未关联议题的洞察确认后要真的建出议题，而不是只改状态。"""
    _ingest_silent(client, team_slug)
    client.post(f"/api/teams/{team_slug}/engine/jobs/drain", params={"limit": 5})
    items = client.get(f"/api/teams/{team_slug}/engine/insights").json()["insights"]
    insight_id = items[0]["id"]

    applied = client.post(
        f"/api/teams/{team_slug}/engine/insights/{insight_id}/apply",
        json={"dry_run": False},
    )
    assert applied.status_code == 200, applied.text
    assert applied.json()["applied"] >= 1

    issues = client.get(f"/api/teams/{team_slug}/issues", params={"state": "all"}).json()["issues"]
    assert any(str(item["title"]).startswith("[端侧]") for item in issues), issues

    detail = client.get(f"/api/teams/{team_slug}/engine/insights/{insight_id}").json()
    state = detail.get("insight", detail).get("state")
    assert state == "confirmed", detail


# ---------------------------------------------------------------- 内容级洞察

DIGEST_ENVELOPE = {
    "schema_version": 1,
    "event_uid": "evt-digest-0001",
    "kind": "snapshot",
    "ts": "2026-11-10T15:30:00Z",
    "trigger": "idle-window",
    "device": {"name": "LAMNING", "platform": "windows", "agent_version": "0.1.0"},
    "repo": {
        "label": "TeamFlow.dev",
        "path_hash": "9f2c1a77b04e5d63",
        "branch": "main",
        "head": "abc1234def",
        "remote_hash": "9f2c1a77b04e5d63",
        "dirty": False,
        "unpushed": False,
    },
    "stats": {"files": 2, "added": 40, "removed": 5, "binary": 0},
    "files": [],
    # 静默模式也能带上来的符号级摘要：这是「AI 只看数量」和「AI 知道改了什么」的分界
    "digest": {"added": ["rebase", "seconds_until_settle"], "removed": ["_hash_cache"]},
    "redactions": 0,
    "redacted": True,
    "notes": "静默窗口上报",
}


def test_digest_symbols_reach_summary_and_evidence():
    """符号摘要要能进洞察正文，而不是只有「2 个文件变化」。"""
    envelope = parse_envelope(DIGEST_ENVELOPE)
    result = rules.analyze(envelope)

    summary = result["analysis"]["summary"]
    assert "rebase()" in summary, summary
    assert "seconds_until_settle()" in summary, summary
    assert "移除" in summary and "_hash_cache()" in summary, summary

    refs = [item.get("ref") for item in result["analysis"]["evidence"]]
    assert any("rebase()" in str(ref) for ref in refs), refs


def test_digest_line_is_human_readable():
    """进度记录那一行要能直接贴进议题评论里。"""
    signals = rules.collect_signals(parse_envelope(DIGEST_ENVELOPE))
    line = rules.digest_line(signals)
    assert "rebase()" in line
    assert "_hash_cache()" in line


def test_existing_issue_gets_progress_comment_and_body(client, team_slug, new_issue):
    """已有议题时，应用洞察必须追加进度记录 + 覆写正文，不能只改状态。"""
    number = int(new_issue["number"])
    envelope = dict(DIGEST_ENVELOPE)
    envelope["event_uid"] = f"evt-digest-issue-{number}"
    envelope["repo"] = {**DIGEST_ENVELOPE["repo"], "branch": f"feat/{number}-progress"}

    token = client.post(
        f"/api/teams/{team_slug}/engine/edge/pair",
        json={"name": f"进度机{number}", "platform": "windows"},
    ).json()["token"]
    stored = client.post(
        f"/api/teams/{team_slug}/engine/edge/events",
        json={"token": token, "envelope": envelope},
    )
    assert stored.status_code == 200, stored.text
    client.post(f"/api/teams/{team_slug}/engine/jobs/drain", params={"limit": 5})

    insights = client.get(f"/api/teams/{team_slug}/engine/insights").json()["insights"]
    target = next(item for item in insights if item.get("issue_number") == number)
    actions = (target.get("suggestion") or {}).get("actions") or []
    assert actions and actions[0]["op"] == "update_issue", actions
    assert actions[0].get("comment"), "动作里必须带进度记录"
    assert actions[0].get("body_append"), "动作里必须带正文追加"

    applied = client.post(
        f"/api/teams/{team_slug}/engine/insights/{target['id']}/apply",
        json={"dry_run": False},
    )
    assert applied.status_code == 200, applied.text

    detail = client.get(f"/api/teams/{team_slug}/issues/{number}").json()
    assert "自动进度" in (detail["issue"].get("body") or ""), detail["issue"]
    assert detail["comments"], "应用后议题下应该有进度记录"
    assert "rebase" in detail["comments"][-1]["body"]


def test_digest_notes_become_human_context():
    """注释线索要能进摘要与证据，让洞察读起来像「在做什么」而不是「几个文件」。"""
    envelope = dict(DIGEST_ENVELOPE)
    envelope["event_uid"] = "evt-digest-notes"
    envelope["digest"] = {
        "added": ["rebase"],
        "removed": [],
        "notes": ["修复保存后列表不刷新"],
    }
    result = rules.analyze(parse_envelope(envelope))
    summary = result["analysis"]["summary"]
    assert "修复保存后列表不刷新" in summary, summary
    assert any(item.get("type") == "note" for item in result["analysis"]["evidence"])


# ---------------------------------------------------------------- AI 自己提议的议题/里程碑


def test_plan_becomes_milestone_before_issue():
    """模型建议「新开里程碑 + 新建议题」时，动作顺序必须是先建里程碑、再挂议题。

    顺序反了的话，新建的议题拿不到里程碑编号（那个编号此刻还不存在）。
    """
    from app.services.inference import admission
    from app.services.inference.pipeline import _actions_for

    decision = admission.Decision(
        verdict=admission.VERDICT_CREATE,
        reason="新增深色主题切换",
        source="model",
        new_issue={"title": "支持深色主题切换", "body": "新增 toggleTheme()", "priority": "medium"},
        milestone={"action": "create", "title": "体验优化", "acceptance": "- [ ] 三个页面生效"},
    )
    actions = _actions_for(
        None,
        {"status": "in_progress"},
        rules.collect_signals(parse_envelope(DIGEST_ENVELOPE)),
        "兜底标题",
        "摘要",
        {},
        decision,
    )

    assert [item["op"] for item in actions] == ["create_milestone", "create_issue"]
    assert actions[1]["title"] == "支持深色主题切换"
    assert actions[1]["milestone"] == "new", "新议题要挂到本次新建的里程碑上"


def test_plan_updates_existing_issue_with_model_status():
    """merge 时模型可以改状态、改优先级，并把一句人话进度写进评论。"""
    from app.services.inference import admission
    from app.services.inference.pipeline import _actions_for

    class _Issue:
        number = 18

    decision = admission.Decision(
        verdict=admission.VERDICT_MERGE,
        reason="推进 #18",
        source="model",
        issue_update={"status": "done", "priority": "high", "progress": "准入闸门已接入管线"},
    )
    actions = _actions_for(
        _Issue(),
        {"status": "in_progress"},
        rules.collect_signals(parse_envelope(DIGEST_ENVELOPE)),
        "标题",
        "摘要",
        {},
        decision,
    )

    assert actions[0]["op"] == "update_issue"
    assert actions[0]["status"] == "done", "模型确认的完成状态要能覆盖规则版"
    assert actions[0]["priority"] == "high"
    assert "准入闸门已接入管线" in actions[0]["comment"]


def test_apply_links_new_issue_to_new_milestone(client, team_slug):
    """执行「先建里程碑、再建议题」时，议题要真的落到新里程碑下。"""
    import json

    from sqlalchemy import select

    from app.db import session as dbsession
    from app.db.base import utcnow_iso
    from app.db.engine_models import Insight
    from app.db.models import Issue, Milestone, Team

    with dbsession.session_scope() as session:
        team = session.scalar(select(Team).where(Team.slug == team_slug))
        insight = Insight(
            team_id=team.id,
            kind="unscoped",
            title="体验优化",
            summary="新增深色主题切换",
            confidence=0.5,
            state="draft",
            evidence="[]",
            suggestion=json.dumps({
                "dry_run": True,
                "actions": [
                    {"op": "create_milestone", "title": "体验优化", "acceptance": "- [ ] 三页生效"},
                    {"op": "create_issue", "title": "支持深色主题切换", "status": "in_progress",
                     "milestone": "new"},
                ],
            }, ensure_ascii=False),
            applied="{}",
            model="test",
            dedupe_key=f"test:milestone-link:{utcnow_iso()}",
            created_at=utcnow_iso(),
        )
        session.add(insight)
        session.flush()
        insight_id = insight.id

    applied = client.post(
        f"/api/teams/{team_slug}/engine/insights/{insight_id}/apply",
        json={"dry_run": False},
    )
    assert applied.status_code == 200, applied.text
    assert applied.json()["applied"] == 2

    with dbsession.session_scope() as session:
        milestone = session.scalar(
            select(Milestone).where(Milestone.team_id == team.id, Milestone.title == "体验优化")
        )
        assert milestone is not None, "里程碑应该被真的建出来"
        issue = session.scalar(
            select(Issue).where(Issue.team_id == team.id, Issue.title == "支持深色主题切换")
        )
        assert issue is not None, "议题应该被真的建出来"
        assert issue.milestone_id == milestone.id, "新议题要挂在同一个提案里新建的里程碑下"
