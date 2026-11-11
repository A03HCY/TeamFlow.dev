"""「最近一次分析」这条链路：必须说得出改了什么，而不是只数文件。

这条链路和 engine 的洞察是两套实现（``log_analysis.py`` vs ``inference/rules.py``），
之前只有后者接了符号摘要，于是页面上一直显示「2 个文件改动（+12/-6 行）」。
"""
from __future__ import annotations

from types import SimpleNamespace

from app.services.inference import rules
from app.services.inference.events import parse_envelope
from app.services.log_analysis import _content_hint, _files_hint, _rules_text


def _envelope(files, digest=None):
    return {
        "schema_version": 1,
        "event_uid": "evt-log-analysis-1",
        "kind": "snapshot",
        "ts": "2026-11-11T00:10:00Z",
        "trigger": "idle-window",
        "device": {"name": "LAMNING", "platform": "windows", "agent_version": "0.1.0"},
        "repo": {
            "label": "TeamFlow.dev",
            "path_hash": "9f2c1a77b04e5d63",
            "branch": "main",
            "head": "abc1234def",
            "dirty": True,
            "unpushed": False,
        },
        "stats": {"files": len(files), "added": 12, "removed": 6, "binary": 0},
        "files": files,
        "digest": digest or {"added": [], "removed": [], "notes": [], "lines": []},
        "redactions": 0,
        "redacted": True,
        "notes": "",
    }


def _row(files=2, added=12, removed=6):
    return SimpleNamespace(
        file_count=files, additions=added, deletions=removed,
        repo_label="TeamFlow.dev", branch="main", idle_minutes=1, summary="",
    )


def test_file_buckets_never_go_negative():
    """曾经出现「-1 个源码文件」：tests 和 docs 重复命中同一个路径。"""
    files = [
        {"path": "server/tests/test_foo.md", "status": "modified"},
        {"path": "read me.md", "status": "modified"},
    ]
    hint = _files_hint(files)
    assert "-1" not in hint, hint
    assert "1 个测试文件" in hint, hint


def test_digest_lines_answer_what_the_six_lines_are():
    envelope = _envelope(
        [
            {"path": "server/tests/test_foo.md", "status": "modified"},
            {"path": "read me.md", "status": "modified"},
        ],
        digest={
            "added": [],
            "removed": [],
            "notes": [],
            "lines": ["看到此行请建议新增议题：测试", "前端已制作完毕"],
        },
    )
    parsed = parse_envelope(envelope)
    verdict = rules.analyze(parsed)

    text = _rules_text(verdict, _row(), parsed.files)
    assert "看到此行请建议新增议题" in text, text
    assert text.index("看到此行") < text.index("改动包含"), "内容线索要排在文件统计前面"


def test_silent_mode_still_reports_classification():
    """静默模式没有路径，也要说出「1 个源码文件 + 1 个测试文件」。"""
    envelope = _envelope(
        [],
        digest={"added": [], "removed": [], "notes": [], "lines": ["新增接口 /api/health"]},
    )
    parsed = parse_envelope(envelope)
    verdict = rules.analyze(parsed)

    text = _rules_text(verdict, _row(files=2), [])
    assert "新增接口 /api/health" in text, text


def test_model_prompt_carries_content_hint():
    envelope = _envelope(
        [{"path": "read me.md", "status": "modified"}],
        digest={"added": [], "removed": [], "notes": [], "lines": ["前端已制作完毕"]},
    )
    verdict = rules.analyze(parse_envelope(envelope))
    hint = _content_hint(verdict)
    assert "前端已制作完毕" in hint, hint


def test_unscoped_doc_change_titles_from_content():
    """没有代码符号时，洞察标题要读得出新增内容，而不是「以文档变更为主」。"""
    envelope = _envelope(
        [{"path": "read me.md", "status": "modified"}],
        digest={
            "added": [], "removed": [], "notes": [],
            "lines": ["看到此行请在网页建议新增新的议题：测试"],
        },
    )
    verdict = rules.analyze(parse_envelope(envelope))
    assert "议题" in verdict["title"], verdict["title"]
    assert "以文档" not in verdict["title"], verdict["title"]


# ------------------------------------------------------- 模型返回正文为空

class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        import json
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _model_reply(content: str, finish_reason: str = "stop"):
    return {
        "choices": [
            {
                "finish_reason": finish_reason,
                "message": {"role": "assistant", "content": content,
                            "reasoning_content": "想了很多" * 50},
            }
        ],
        "usage": {"completion_tokens": 320, "completion_tokens_details": {"reasoning_tokens": 320}},
    }


def test_empty_model_content_is_a_failure(monkeypatch):
    """推理模型把预算吃光时 content 是空串；不能当成功，否则覆盖规则文案。"""
    from app.services import log_analysis

    monkeypatch.setattr(
        log_analysis.urllib.request, "urlopen",
        lambda request, timeout=12: _FakeResponse(_model_reply("", "length")),
    )
    cfg = {"base_url": "https://api.deepseek.com", "api_key": "k", "model": "deepseek-flash"}
    import pytest
    with pytest.raises(ValueError):
        log_analysis._call_model(cfg, "prompt")


def test_truncated_model_reply_falls_back_to_rules(monkeypatch):
    """模型只回了半句时，宁可给规则文案，也不能给一条断掉的分析。"""
    from app.services import log_analysis

    monkeypatch.setattr(
        log_analysis.ai_service, "get_config",
        lambda session: {"base_url": "https://api.deepseek.com", "api_key": "k", "model": "m"},
    )
    monkeypatch.setattr(
        log_analysis, "_call_model",
        lambda cfg, prompt: "这轮改动实质上是在做一次端到端验证：在",
    )
    envelope = _envelope(
        [{"path": "read me.md", "status": "modified"}],
        digest={"added": [], "removed": [], "notes": [], "lines": ["前端已制作完毕"]},
    )
    parsed = parse_envelope(envelope)
    verdict = rules.analyze(parsed)

    text = log_analysis.analyze_log(None, _row(files=1, added=6, removed=0), verdict, parsed.files)
    assert "前端已制作完毕" in text, text
    assert not text.endswith("在"), text
