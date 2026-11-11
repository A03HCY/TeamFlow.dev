"""准入闸门：决定「一条事件什么时候该变成洞察」的那一层。

回归的起点是一个真实缺陷：改一行注释的 .py 能建卡，写满一页新需求的 .md
反而被丢掉——因为过去唯一的判断依据是文件后缀加出来的置信度。
这些用例锁定三条底线：

1. 噪声（锁文件/构建产物）与纯措辞修订不建卡；
2. 分支号能确定的归属不问模型，省钱也更快；
3. 模型可以改判、可以指定议题，但必须落在真实候选里，失败要回退规则。
"""
from __future__ import annotations

import pytest

from app.services.inference import admission, rules


def _signals(**kw) -> rules.Signals:
    base = dict(
        files=1,
        added=10,
        removed=1,
        code_files=1,
        branch="main",
        top_files=["server/app/services/inference/pipeline.py"],
    )
    base.update(kw)
    return rules.Signals(**base)


# ---------------------------------------------------------------- 硬性噪声


def test_lock_and_bundle_files_are_rejected():
    signals = _signals(
        files=2,
        code_files=0,
        top_files=["package-lock.json", "web/app.min.js"],
    )
    decision = admission.rule_decision(signals, confidence=0.4, has_issue=False)

    assert decision.verdict == admission.VERDICT_SKIP
    assert decision.firm is True
    assert "依赖锁" in decision.reason


def test_build_output_is_noise():
    assert admission.is_noise_path("dist/assets/index-9f2.js")
    assert admission.is_noise_path("coverage/lcov.info")
    assert not admission.is_noise_path("server/app/main.py")


def test_empty_report_is_rejected():
    decision = admission.rule_decision(
        _signals(files=0, code_files=0, top_files=[]), confidence=0.0, has_issue=False
    )
    assert decision.verdict == admission.VERDICT_SKIP
    assert decision.firm is True


# ---------------------------------------------------------------- 文档改动


def test_docs_only_without_requirement_is_rejected():
    signals = _signals(
        files=1,
        code_files=0,
        doc_files=1,
        top_files=["README.md"],
        content_lines=["修正了几处错别字和标点"],
    )
    decision = admission.rule_decision(signals, confidence=0.1, has_issue=False)

    assert decision.verdict == admission.VERDICT_SKIP
    assert decision.firm is True
    assert "文档" in decision.reason


def test_docs_with_new_requirement_goes_to_model():
    """文档里写下了新的待办/验收条件：不能因为后缀是 .md 就直接丢掉。"""
    signals = _signals(
        files=1,
        code_files=0,
        doc_files=1,
        top_files=["docs/plan.md"],
        content_lines=["待办：下一版接入语义归属，验收标准是误挂率低于 5%"],
    )
    decision = admission.rule_decision(signals, confidence=0.1, has_issue=False)

    # 规则不直接判死，留 firm=False 让模型再看一眼
    assert decision.firm is False


# ---------------------------------------------------------------- 归属与噪声的其它边界


def test_branch_number_wins_without_asking_model():
    signals = _signals(issue_number=18, branch="feat/18-admission-gate", added=200)
    decision = admission.rule_decision(signals, confidence=0.55, has_issue=True)

    assert decision.verdict == admission.VERDICT_MERGE
    assert decision.issue_number == 18
    assert decision.firm is True


def test_sensitive_only_is_rejected():
    signals = _signals(
        files=1, code_files=0, sensitive_files=1, top_files=["server/.env"]
    )
    decision = admission.rule_decision(signals, confidence=0.3, has_issue=False)

    assert decision.verdict == admission.VERDICT_SKIP
    assert "敏感" in decision.reason


def test_huge_deletion_is_rejected():
    """以删除为主的大改动更像回滚或误删，不能当成进展。"""
    signals = _signals(added=100, removed=5000)
    decision = admission.rule_decision(signals, confidence=0.5, has_issue=False)

    assert decision.verdict == admission.VERDICT_SKIP
    assert decision.firm is True
    assert "回滚" in decision.reason


def test_huge_addition_is_not_rejected():
    """新增为主的大改动（新模块一次性落地）不能一刀切丢掉。

    真实踩过：把项目整体复制进被监听目录，80 个文件 +18096 行，
    旧规则只看 churn 就拒绝，结果「刚接入项目」永远出不了洞察。
    """
    signals = _signals(added=18096, removed=0, symbols_added=["decide", "rule_decision"])
    decision = admission.rule_decision(signals, confidence=0.3, has_issue=False)

    assert decision.verdict == admission.VERDICT_CREATE
    assert decision.firm is False


def test_code_change_without_issue_is_created():
    signals = _signals(symbols_added=["decide", "rule_decision"], added=120)
    decision = admission.rule_decision(signals, confidence=0.4, has_issue=False)

    assert decision.verdict == admission.VERDICT_CREATE
    assert decision.firm is False
    assert "decide" in decision.reason


# ---------------------------------------------------------------- 模型参与


def test_model_cannot_invent_issue_number(monkeypatch):
    monkeypatch.setattr(
        admission,
        "_chat_json",
        lambda *a, **kw: {"verdict": "merge", "issue": 999, "reason": "看起来像"},
    )
    with pytest.raises(ValueError):
        admission.model_decision(
            {"base_url": "http://x", "api_key": "k", "model": "m"},
            _signals(),
            [{"number": 18, "title": "准入闸门", "body": ""}],
            confidence=0.4,
        )


def test_model_can_assign_real_issue(monkeypatch):
    monkeypatch.setattr(
        admission,
        "_chat_json",
        lambda *a, **kw: {
            "verdict": "merge",
            "issue": 18,
            "reason": "新增的 decide() 属于准入闸门议题",
        },
    )
    decision = admission.model_decision(
        {"base_url": "http://x", "api_key": "k", "model": "m"},
        _signals(symbols_added=["decide"]),
        [{"number": 18, "title": "准入闸门", "body": ""}],
        confidence=0.4,
    )

    assert decision.verdict == admission.VERDICT_MERGE
    assert decision.issue_number == 18
    assert decision.source == "model"


def test_model_failure_falls_back_to_rules(monkeypatch):
    """模型挂了不能把事件也弄丢：退回规则结论，并注明原因。"""
    monkeypatch.setattr(
        admission.ai_service,
        "get_config",
        lambda session: {"base_url": "http://x", "api_key": "k", "model": "m"},
    )
    monkeypatch.setattr(
        admission, "_chat_json", lambda *a, **kw: (_ for _ in ()).throw(TimeoutError("超时"))
    )

    decision = admission.decide(
        None,
        team_id=1,
        signals=_signals(symbols_added=["decide"]),
        confidence=0.4,
        has_issue=False,
        candidates=[{"number": 18, "title": "准入闸门", "body": ""}],
    )

    assert decision.verdict == admission.VERDICT_CREATE
    assert decision.source == "rules"
    assert "模型判断不可用" in decision.reason


def test_firm_decision_never_calls_model(monkeypatch):
    called = {"n": 0}

    def boom(session):
        called["n"] += 1
        raise AssertionError("硬性结论不该再问模型")

    monkeypatch.setattr(admission.ai_service, "get_config", boom)
    decision = admission.decide(
        None,
        team_id=1,
        signals=_signals(top_files=["package-lock.json"], code_files=0),
        confidence=0.4,
        has_issue=False,
    )

    assert decision.verdict == admission.VERDICT_SKIP
    assert called["n"] == 0


# ---------------------------------------------------------------- 规划建议：议题/里程碑


def test_model_plan_fields_are_sanitized(monkeypatch):
    """模型给的建议要逐字段校验：非法状态丢弃、越权编号丢弃、结论不符的建议丢弃。

    这条用例守的是「AI 能提建议」和「AI 能乱写」之间的那条线。
    """
    monkeypatch.setattr(
        admission,
        "_chat_json",
        lambda *a, **kw: {
            "verdict": "merge",
            "issue": 18,
            "reason": "decide() 属于准入闸门",
            "issue_update": {
                "status": "完成",          # 不是白名单里的值
                "priority": "urgent",      # 合法
                "progress": "补上准入判断，跳过噪声改动",
                "body_append": "",         # 空值不该留下
            },
            "new_issue": {"title": "merge 时不该有新议题"},  # 结论不符
            "milestone": {"action": "update", "number": 99, "acceptance": "x"},  # 编造的编号
        },
    )
    decision = admission.model_decision(
        {"base_url": "http://x", "api_key": "k", "model": "m"},
        _signals(symbols_added=["decide"]),
        [{"number": 18, "title": "准入闸门", "body": ""}],
        confidence=0.4,
        milestones=[{"number": 3, "title": "v1"}],
    )

    assert decision.issue_update == {"priority": "urgent", "progress": "补上准入判断，跳过噪声改动"}
    assert decision.new_issue == {}
    assert decision.milestone == {}


def test_model_can_propose_new_issue_and_milestone(monkeypatch):
    """create 时模型可以自己起议题标题、正文，并顺手新开一个里程碑。"""
    monkeypatch.setattr(
        admission,
        "_chat_json",
        lambda *a, **kw: {
            "verdict": "create",
            "reason": "新增深色主题切换，候选议题都不涉及",
            "title": "深色主题切换",
            "new_issue": {
                "title": "支持深色主题切换",
                "body": "新增 theme.js 的 toggleTheme()，尚未覆盖设置页。",
                "priority": "medium",
                "milestone": 3,
            },
            "milestone": {
                "action": "create",
                "title": "体验优化",
                "acceptance": "- [ ] 主题切换在三个页面生效",
            },
        },
    )
    decision = admission.model_decision(
        {"base_url": "http://x", "api_key": "k", "model": "m"},
        _signals(symbols_added=["toggleTheme"]),
        [{"number": 18, "title": "准入闸门", "body": ""}],
        confidence=0.4,
        milestones=[{"number": 3, "title": "v1"}],
    )

    assert decision.verdict == admission.VERDICT_CREATE
    assert decision.new_issue["title"] == "支持深色主题切换"
    assert decision.new_issue["milestone"] == 3
    assert decision.milestone["action"] == "create"
    plan = decision.as_dict()["plan"]
    assert plan["milestone"]["title"] == "体验优化"


def test_milestone_update_accepts_candidate_number(monkeypatch):
    monkeypatch.setattr(
        admission,
        "_chat_json",
        lambda *a, **kw: {
            "verdict": "merge",
            "issue": 18,
            "reason": "验收标准已满足",
            "milestone": {"action": "update", "number": 3, "state": "done", "acceptance": "- [x] 全部通过"},
        },
    )
    decision = admission.model_decision(
        {"base_url": "http://x", "api_key": "k", "model": "m"},
        _signals(),
        [{"number": 18, "title": "准入闸门", "body": ""}],
        confidence=0.4,
        milestones=[{"number": 3, "title": "v1"}],
    )

    assert decision.milestone == {
        "action": "update", "number": 3, "state": "done", "acceptance": "- [x] 全部通过",
    }


def test_skip_drops_every_plan_field(monkeypatch):
    """判定不值得记录时，任何落库建议都不该被采信。"""
    monkeypatch.setattr(
        admission,
        "_chat_json",
        lambda *a, **kw: {
            "verdict": "skip",
            "reason": "只是改注释",
            "new_issue": {"title": "不该出现"},
            "milestone": {"action": "create", "title": "不该出现"},
        },
    )
    decision = admission.model_decision(
        {"base_url": "http://x", "api_key": "k", "model": "m"},
        _signals(),
        [{"number": 18, "title": "准入闸门", "body": ""}],
        confidence=0.4,
        milestones=[],
    )

    assert decision.as_dict().get("plan") is None
