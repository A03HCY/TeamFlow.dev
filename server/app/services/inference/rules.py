"""规则推断（P1：不调 LLM 的确定性版本）。

为什么先做规则版：端侧探针刚接进来时，先把「事件 → 洞察」的链路、幂等、
审计、收件箱跑通，模型只在 P2 接到 Analyst 的位置上替换掉 ``analyze``。
因此这里的输出**故意与 Analyst 的 JSON 形状一致**（``issue_number`` /
``completion`` / ``status`` / ``confidence`` / ``evidence`` / ``risks`` /
``summary``），可以直接丢进 ``engine.check_analysis`` 过同一套防幻觉闸门。

打分规则（全部可解释，方便用户质问「凭什么是 0.72」）：

| 信号 | 影响 |
|---|---|
| 分支名里带 Issue 编号且议题存在 | +0.25 |
| 改了测试文件（新增/修改） | +0.20 |
| 改了代码文件（非文档、非测试） | +0.10 |
| 只动了文档 | -0.20 |
| 敏感文件被改动（.env 等，端侧已脱敏但路径可见） | -0.15 |
| 改动量 > 1500 行 | -0.10 |
| 本地提交未 push | ×0.85，且禁止建议 ``done`` |

基础分 0.30，最终收敛到 ``[0, 0.95]``。``status`` 只会给 ``in_progress`` 或
``done``：``done`` 必须同时满足「改了测试 + 已 push + 有 Issue + 置信度 ≥ 0.75」，
其余一律 ``in_progress``（人工确认后才真正确认完成）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

TEST_PATTERNS = (
    re.compile(r"(^|/)(tests?|__tests__|spec|specs)/"),
    re.compile(r"(^|/)tests?_[^/]+$"),
    re.compile(r"_test\.(py|go|rs|ts|js|mjs|cjs)$"),
    re.compile(r"\.(test|spec)\.(ts|tsx|js|jsx|mjs|cjs|vue)$"),
    re.compile(r"(^|/)conftest\.py$"),
)
DOC_SUFFIXES = (".md", ".mdx", ".rst", ".txt", ".adoc", ".pdf")
CODE_SUFFIXES = (
    ".py", ".pyi", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".vue",
    ".go", ".rs", ".java", ".kt", ".rb", ".php", ".cs", ".swift", ".c", ".h",
    ".cc", ".cpp", ".hpp", ".m", ".mm", ".scala", ".sh", ".ps1", ".sql",
)
SENSITIVE_HINTS = (
    ".env", ".pem", ".key", "id_rsa", "credentials", "secrets", ".p12", ".pfx",
)

# 分支名 → Issue 编号：feat/123-x、fix_45、#12、GH-12、issue-7
BRANCH_ISSUE_PATTERNS = (
    re.compile(r"#(\d{1,6})\b"),
    re.compile(r"(?i)\b(?:gh|issue|bug|feat|feature|fix|chore|task)[-_/](\d{1,6})\b"),
    re.compile(r"^(\d{1,6})[-_/]"),
)

CONFIDENCE_BASE = 0.30
CONFIDENCE_ISSUE = 0.25
CONFIDENCE_TESTS = 0.20
CONFIDENCE_CODE = 0.10
PENALTY_DOCS_ONLY = 0.20
PENALTY_SENSITIVE = 0.15
PENALTY_CHURN = 0.10
UNPUSHED_FACTOR = 0.85
DONE_MIN_CONFIDENCE = 0.75
CHURN_LIMIT = 1500
MAX_CONFIDENCE = 0.95


@dataclass
class Signals:
    """从一个事件包里抽出来的、可解释的观察值。"""

    files: int = 0
    added: int = 0
    removed: int = 0
    code_files: int = 0
    test_files: int = 0
    doc_files: int = 0
    sensitive_files: int = 0
    tests_added: int = 0
    tests_changed: int = 0
    tests_removed: int = 0
    unpushed: bool = False
    branch: str = ""
    head: str = ""
    kind: str = "save"
    ts: str = ""
    top_files: list[str] = field(default_factory=list)
    issue_number: int | None = None
    # 端侧抽出的符号差：让洞察说得出「这次加了什么」，而不是只有「3 个文件变化」
    symbols_added: list[str] = field(default_factory=list)
    symbols_removed: list[str] = field(default_factory=list)
    # 端侧抽出的注释线索：本地人写的单行说明，最能回答「在做什么」
    notes_added: list[str] = field(default_factory=list)
    # 本轮新增的内容行：文档是正文，代码是骨架（导入/路由/签名）
    content_lines: list[str] = field(default_factory=list)

    @property
    def churn(self) -> int:
        return int(self.added) + int(self.removed)

    @property
    def touched_tests(self) -> bool:
        return self.test_files > 0 or self.tests_added > 0 or self.tests_changed > 0

    @property
    def touched_code(self) -> bool:
        return self.code_files > 0

    @property
    def docs_only(self) -> bool:
        return self.files > 0 and self.code_files == 0 and self.test_files == 0


def is_test_path(path: str) -> bool:
    text = (path or "").lower()
    return any(pattern.search(text) for pattern in TEST_PATTERNS)


def is_doc_path(path: str) -> bool:
    return (path or "").lower().endswith(DOC_SUFFIXES)


def is_code_path(path: str) -> bool:
    text = (path or "").lower()
    if is_test_path(text) or is_doc_path(text):
        return False
    return text.endswith(CODE_SUFFIXES)


def is_sensitive_path(path: str) -> bool:
    text = (path or "").lower()
    return any(hint in text for hint in SENSITIVE_HINTS)


def branch_issue_number(branch: str | None) -> int | None:
    text = (branch or "").strip()
    if not text:
        return None
    for pattern in BRANCH_ISSUE_PATTERNS:
        found = pattern.search(text)
        if found:
            try:
                value = int(found.group(1))
            except (TypeError, ValueError):
                continue
            if value > 0:
                return value
    return None


def collect_signals(envelope) -> Signals:  # noqa: ANN001 - CodeEventEnvelope
    """把事件包折算成打分所需的信号（纯函数，便于单测）。"""
    signals = Signals(
        branch=envelope.repo.branch or "",
        head=envelope.repo.head or "",
        unpushed=bool(envelope.repo.unpushed),
        kind=envelope.kind,
        ts=envelope.ts,
        added=int(envelope.stats.added or 0),
        removed=int(envelope.stats.removed or 0),
        tests_added=int(envelope.stats.tests_added or 0),
        tests_changed=int(envelope.stats.tests_changed or 0),
        tests_removed=int(envelope.stats.tests_removed or 0),
        files=len(envelope.files) or int(envelope.stats.files or 0),
        issue_number=branch_issue_number(envelope.repo.branch),
    )
    for item in envelope.files:
        if is_test_path(item.path):
            signals.test_files += 1
        elif is_doc_path(item.path):
            signals.doc_files += 1
        elif is_code_path(item.path):
            signals.code_files += 1
        if item.sensitive or is_sensitive_path(item.path):
            signals.sensitive_files += 1
    if not envelope.files and signals.files:
        # 静默模式只上行统计、不上行路径：按测试变更数反推源码文件数，
        # 否则标题会误判成「以文档/配置变更为主」
        known = signals.tests_added + signals.tests_changed + signals.tests_removed
        signals.code_files = max(signals.files - known, 0)

    # 符号差：顶层 digest（静默模式也有）+ 文件级明细（可见模式更准）
    def _add(target: list[str], values) -> None:
        for name in values or []:
            text = str(name or "").strip()
            if text and text not in target:
                target.append(text)

    digest = getattr(envelope, "digest", None)
    if digest is not None:
        _add(signals.symbols_added, getattr(digest, "added", None))
        _add(signals.symbols_removed, getattr(digest, "removed", None))
        _add(signals.notes_added, getattr(digest, "notes", None))
        _add(signals.content_lines, getattr(digest, "lines", None))
    for item in envelope.files:
        _add(signals.symbols_added, getattr(item, "symbols_added", None))
        _add(signals.symbols_removed, getattr(item, "symbols_removed", None))
        _add(signals.notes_added, getattr(item, "notes_added", None))
        _add(signals.content_lines, getattr(item, "lines_added", None))
    # 同一批里既加又删同名（重命名）时，删除列表里去掉，避免读起来自相矛盾
    added_set = set(signals.symbols_added)
    signals.symbols_removed = [s for s in signals.symbols_removed if s not in added_set]
    signals.symbols_added = signals.symbols_added[:12]
    signals.symbols_removed = signals.symbols_removed[:12]
    signals.notes_added = signals.notes_added[:6]
    signals.content_lines = signals.content_lines[:10]
    # 证据里最值得展示的几条：先测试、再代码、再文档
    ordered = sorted(
        (item.path for item in envelope.files),
        key=lambda path: (0 if is_test_path(path) else 1 if is_code_path(path) else 2, path),
    )
    signals.top_files = ordered[:5]
    return signals


def iso_week(value: str | None) -> str:
    """``ts`` → ``2026-W45``（建议的 plan_week 用当前事件所在的周）。"""
    text = (value or "").replace("Z", "+00:00")
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        moment = datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    year, week, _ = moment.isocalendar()
    return f"{year}-W{week:02d}"


def score(signals: Signals) -> tuple[float, list[str]]:
    """返回 ``(置信度, 打分说明)``；说明会原样进洞察的证据，方便用户复核。"""
    value = CONFIDENCE_BASE
    notes = [f"基础分 {CONFIDENCE_BASE:.2f}"]
    if signals.issue_number:
        value += CONFIDENCE_ISSUE
        notes.append(f"分支名指向 #{signals.issue_number} +{CONFIDENCE_ISSUE:.2f}")
    if signals.touched_tests:
        value += CONFIDENCE_TESTS
        notes.append(f"改动了 {signals.test_files} 个测试文件 +{CONFIDENCE_TESTS:.2f}")
    if signals.touched_code:
        value += CONFIDENCE_CODE
        notes.append(f"改动了 {signals.code_files} 个代码文件 +{CONFIDENCE_CODE:.2f}")
    if signals.docs_only:
        value -= PENALTY_DOCS_ONLY
        notes.append(f"只动文档 -{PENALTY_DOCS_ONLY:.2f}")
    if signals.sensitive_files:
        value -= PENALTY_SENSITIVE
        notes.append(f"涉及 {signals.sensitive_files} 个敏感文件 -{PENALTY_SENSITIVE:.2f}")
    if signals.churn > CHURN_LIMIT:
        value -= PENALTY_CHURN
        notes.append(f"改动量 {signals.churn} 行过大 -{PENALTY_CHURN:.2f}")
    if signals.unpushed:
        value *= UNPUSHED_FACTOR
        notes.append(f"提交尚未 push ×{UNPUSHED_FACTOR:.2f}")
    return max(0.0, min(MAX_CONFIDENCE, round(value, 2))), notes


def _evidence(signals: Signals) -> list[dict]:
    items: list[dict] = []
    if signals.branch:
        items.append({
            "type": "branch",
            "ref": signals.branch,
            "note": "本地提交未 push" if signals.unpushed else "分支已同步远端",
        })
    if signals.head:
        items.append({"type": "commit", "ref": signals.head[:12], "note": f"触发事件：{signals.kind}"})
    for name in signals.symbols_added[:4]:
        items.append({"type": "symbol", "ref": f"{name}()", "note": "本轮新增的符号"})
    for name in signals.symbols_removed[:2]:
        items.append({"type": "symbol", "ref": f"{name}()", "note": "本轮移除的符号"})
    for note in signals.notes_added[:2]:
        items.append({"type": "note", "ref": note[:70], "note": "改动里的说明"})
    # 没有符号可引用时（文档/配置/纯文本），把新增的几行本身作为证据
    for line in (signals.content_lines[:3] if not signals.symbols_added else []):
        items.append({"type": "line", "ref": line[:70], "note": "新增的内容行"})
    for path in signals.top_files:
        kind = "test" if is_test_path(path) else "file"
        items.append({"type": kind, "ref": path, "note": "本次改动涉及"})
    return items[:10]


def _risks(signals: Signals) -> list[str]:
    risks: list[str] = []
    if signals.unpushed:
        risks.append("提交尚未 push，进度可能只是本地草稿")
    if signals.touched_code and not signals.touched_tests:
        risks.append("改了实现但没有测试变更，无法证明功能可用")
    if signals.sensitive_files:
        risks.append("触及敏感文件路径，请在确认前人工核对脱敏结果")
    if signals.docs_only:
        risks.append("只改了文档，不能据此认为功能有进展")
    if signals.churn > CHURN_LIMIT:
        risks.append(f"单次改动 {signals.churn} 行，可能包含批量重命名或自动生成代码")
    return risks


def completion_for(signals: Signals) -> int:
    """粗估完成度：只作为展示值，真正写回状态要人工确认。

    注意与 ``check_analysis`` 的硬规则对齐：判 ``done`` 时完成度必须 ≥ 90，
    否则洞察会被自己的闸门拦下来（那是个自相矛盾的结论）。
    """
    if signals.docs_only:
        return 10
    value = 25
    if signals.touched_code:
        value = 45
    if signals.touched_tests:
        value = 70
    if signals.touched_tests and not signals.unpushed and signals.issue_number:
        value = 92
    return max(0, min(100, value))


def suggested_status(signals: Signals, confidence: float) -> str:
    if (
        signals.touched_tests
        and not signals.unpushed
        and signals.issue_number
        and confidence >= DONE_MIN_CONFIDENCE
    ):
        return "done"
    return "in_progress"


_SKELETON_PREFIXES = (
    "def ", "async def ", "class ", "import ", "from ", "@",
    "export ", "const ", "let ", "var ", "function ", "pub fn ", "fn ",
)


def _prose_lines(signals: Signals, limit: int = 3) -> list[str]:
    """内容行里的「人话」：文档正文、注释、配置值，而不是 def/import 这类骨架。

    骨架已经由符号清单表达过了，重复列一遍只会把摘要挤满、把文档内容挤掉。
    """
    out: list[str] = []
    for line in signals.content_lines:
        text = line.strip()
        if not text or text.startswith(_SKELETON_PREFIXES):
            continue
        out.append(text)
        if len(out) >= limit:
            break
    return out


def title_for(signals: Signals, issue_title: str | None = None) -> str:
    where = f"#{signals.issue_number}" if signals.issue_number else "未关联议题的变更"
    if issue_title:
        return f"{where} {issue_title}"
    # 没有代码符号时（文档/配置/纯文本），标题直接用新增的第一行说话，
    # 否则建议出来的议题叫「以文档/配置变更为主」，等于什么都没说
    prose = _prose_lines(signals, 1)
    if not signals.touched_code and prose:
        return f"{where}：{prose[0][:48]}"
    if signals.symbols_added:
        return f"{where}：新增 " + "、".join(signals.symbols_added[:3])
    if signals.touched_tests:
        return f"{where}：代码与测试都有更新"
    if signals.touched_code:
        return f"{where}：代码有更新"
    return f"{where}：以文档/配置变更为主"


def summary_for(signals: Signals, confidence: float, status: str) -> str:
    """给洞察卡片的一句话：先说改了什么，再说规则怎么判。"""
    parts = [
        f"分支 {signals.branch or '（未命名）'} 上有 {signals.files} 个文件变化"
        f"（+{signals.added}/-{signals.removed}）",
    ]
    if signals.symbols_added:
        parts.append("新增 " + "、".join(f"{name}()" for name in signals.symbols_added[:5]))
    if signals.symbols_removed:
        parts.append("移除 " + "、".join(f"{name}()" for name in signals.symbols_removed[:3]))
    if signals.notes_added:
        parts.append("说明「" + "；".join(signals.notes_added[:2]) + "」")
    prose = _prose_lines(signals)
    if prose:
        # 直接把新增的几行读出来：没有这一步，洞察只能说「多了六行」
        parts.append("新增内容「" + "；".join(prose) + "」")
    if not signals.symbols_added and not signals.symbols_removed and signals.top_files:
        parts.append("涉及 " + "、".join(signals.top_files[:3]))
    if signals.touched_tests:
        parts.append(f"其中测试变更 {signals.test_files} 个")
    if signals.unpushed:
        parts.append("提交尚未 push")
    label = {"done": "看起来可以收尾", "in_progress": "仍在推进中", "todo": "尚未开始"}.get(
        status, "推进中"
    )
    parts.append(f"规则判断：{label}（置信度 {confidence:.2f}）")
    return "；".join(parts)


def digest_line(signals: Signals) -> str:
    """给议题的进度记录用：一行说明本轮到底做了什么。"""
    bits: list[str] = []
    if signals.symbols_added:
        bits.append("新增 " + "、".join(f"`{name}()`" for name in signals.symbols_added[:6]))
    if signals.symbols_removed:
        bits.append("移除 " + "、".join(f"`{name}()`" for name in signals.symbols_removed[:4]))
    if signals.top_files and not signals.symbols_added:
        bits.append("涉及 " + "、".join(f"`{path}`" for path in signals.top_files[:4]))
    if signals.notes_added:
        bits.append("说明 " + "；".join(f"「{note}」" for note in signals.notes_added[:2]))
    prose = _prose_lines(signals)
    if prose:
        bits.append("新增内容 " + "；".join(f"「{line}」" for line in prose))
    if not bits:
        bits.append(f"{signals.files} 个文件变更（+{signals.added}/-{signals.removed}）")
    return "；".join(bits)


def analyze(envelope, *, issue_title: str | None = None) -> dict:  # noqa: ANN001
    """规则版 Analyst：输出与 LLM 版一致的 analysis JSON。

    返回里额外带 ``signals``/``plan_week``/``notes``，供上层组装洞察与证据；
    ``check_analysis`` 只关心那几个标准字段，多余的键会被忽略。
    """
    signals = collect_signals(envelope)
    confidence, notes = score(signals)
    status = suggested_status(signals, confidence)
    payload = {
        "issue_number": int(signals.issue_number or 0),
        "completion": completion_for(signals),
        "status": status,
        "confidence": confidence,
        "evidence": _evidence(signals),
        "risks": _risks(signals),
        "summary": summary_for(signals, confidence, status),
    }
    return {
        "analysis": payload,
        "signals": signals,
        "notes": notes,
        "title": title_for(signals, issue_title),
        "plan_week": iso_week(signals.ts),
    }


__all__ = [
    "DONE_MIN_CONFIDENCE",
    "Signals",
    "analyze",
    "branch_issue_number",
    "collect_signals",
    "completion_for",
    "is_code_path",
    "is_doc_path",
    "is_sensitive_path",
    "is_test_path",
    "iso_week",
    "score",
    "suggested_status",
]
