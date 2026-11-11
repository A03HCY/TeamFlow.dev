"""端侧「别重复提交」的回归测试。

两个层次：

* ``FolderWatcher``：只被 touch、或另存为同样内容的文件不算改动；结算完的窗口不会
  在下一轮被重新比对出来（``flush`` 必须推进基线）；
* ``WatcherService``：内容与上次成功上报完全一样的一批改动直接跳过，不再打服务端。
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from client import config as client_config  # noqa: E402
from client import gui as client_gui  # noqa: E402
from client.core.watcher import FolderWatcher  # noqa: E402


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def test_touch_without_content_change_is_not_a_change(tmp_path):
    target = tmp_path / "a.py"
    _write(target, "print(1)\n")
    watcher = FolderWatcher(tmp_path, interval=0.5)
    watcher.rebase(keep_pending=False)

    stamp = time.time() + 30
    os.utime(target, (stamp, stamp))          # mtime 变了，内容一模一样

    assert watcher.poll() is None
    assert watcher.pending_count == 0


def test_real_edit_is_still_detected(tmp_path):
    target = tmp_path / "a.py"
    _write(target, "print(1)\n")
    watcher = FolderWatcher(tmp_path, interval=0.5)
    watcher.rebase(keep_pending=False)

    _write(target, "print(2)\nprint(3)\n")

    assert watcher.poll() is None
    assert watcher.pending_count == 1
    report = watcher.flush()
    assert report is not None
    assert report.file_count == 1
    assert report.additions == 1


def test_flush_advances_baseline_so_nothing_is_resubmitted(tmp_path):
    target = tmp_path / "a.py"
    _write(target, "one\n")
    watcher = FolderWatcher(tmp_path, interval=0.5)
    watcher.rebase(keep_pending=False)

    _write(target, "one\ntwo\n")
    watcher.poll()
    assert watcher.pending_count == 1

    first = watcher.flush()
    assert first is not None and first.file_count == 1

    # 关键：结算之后基线要推进，下一轮不再把同一批改动算出来
    assert watcher.poll() is None
    assert watcher.pending_count == 0
    assert watcher.flush() is None


def test_idle_window_reports_only_once(tmp_path):
    target = tmp_path / "a.py"
    _write(target, "one\n")
    watcher = FolderWatcher(tmp_path, idle_minutes=30, interval=0.5)
    watcher.rebase(keep_pending=False)

    _write(target, "one\ntwo\n")
    watcher.poll()
    watcher._last_change_ts -= 31 * 60          # 假装已经静了 31 分钟

    report = watcher.poll()
    assert report is not None and report.file_count == 1
    assert watcher.poll() is None               # 同一个窗口不会被上报第二次


def test_report_signature_is_stable():
    from client.core.watcher import Change, WindowReport

    def build(added: int = 3) -> WindowReport:
        return WindowReport("a", "b", 30, [
            Change("x.py", "modified", added, 1, size=10, digest="d1"),
            Change("y.py", "added", 5, 0, size=20, digest="d2"),
        ])

    assert build().signature() == build().signature()
    assert build(added=4).signature() != build().signature()


class _FakeRest:
    """只记录被调用次数的假通道。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def fetch_policy(self) -> dict:
        return {
            "policy": {"idle_minutes": 30, "allow_view": True},
            "team_name": "测试团队",
            "device": {"name": "test-device"},
        }

    def post_log(self, payload, envelope=None) -> dict:  # noqa: ANN001 - 与真实通道同签名
        self.calls.append(payload)
        return {"status": "received", "analysis": "分析结果", "event": None}


def _service(tmp_path, monkeypatch):  # noqa: ANN001, ANN202 - 测试夹具
    rest = _FakeRest()
    monkeypatch.setattr(client_gui, "fallback_from_config", lambda cfg: rest)
    cfg = client_config.EdgeConfig(
        url="ws://127.0.0.1:8000/api/edge/ws", token="tfd_test", team="default"
    )
    service = client_gui.WatcherService(cfg, tmp_path, interval=0.5)
    service.bound = True
    service.watcher.rebase(keep_pending=False)
    return service, rest


def test_service_skips_identical_report(tmp_path, monkeypatch):
    target = tmp_path / "a.py"
    _write(target, "one\n")
    service, rest = _service(tmp_path, monkeypatch)

    _write(target, "one\ntwo\n")
    service.watcher.poll()
    report = service.watcher.flush()
    assert report is not None

    assert service._deliver(report) == "ok"
    assert service._deliver(report) == "duplicate"     # 一模一样的一批：跳过
    assert len(rest.calls) == 1
    assert service.skipped == 1
    assert service.uploaded == 1


def test_service_delivers_after_real_change(tmp_path, monkeypatch):
    target = tmp_path / "a.py"
    _write(target, "one\n")
    service, rest = _service(tmp_path, monkeypatch)

    _write(target, "one\ntwo\n")
    service.watcher.poll()
    assert service._deliver(service.watcher.flush()) == "ok"

    _write(target, "one\ntwo\nthree\n")
    service.watcher.poll()
    assert service._deliver(service.watcher.flush()) == "ok"
    assert len(rest.calls) == 2


def test_trigger_twice_does_not_double_post(tmp_path, monkeypatch):
    target = tmp_path / "a.py"
    _write(target, "one\n")
    service, rest = _service(tmp_path, monkeypatch)

    _write(target, "one\ntwo\n")
    service.watcher.poll()
    first = service.trigger()
    assert first["ok"] is True
    second = service.trigger()
    assert second["ok"] is False
    assert "没有待分析的改动" in second["message"]
    assert len(rest.calls) == 1


def test_snapshot_exposes_skipped_counter(tmp_path, monkeypatch):
    service, _rest = _service(tmp_path, monkeypatch)
    assert service.snapshot()["skipped"] == 0


# ---------------------------------------------------------------- 内容级摘要

def test_new_function_is_captured_as_symbol(tmp_path):
    """新增函数要能被识别出来：这是「AI 只看行数」和「AI 知道加了什么」的分界。"""
    target = tmp_path / "a.py"
    _write(target, "def one():\n    pass\n")
    watcher = FolderWatcher(tmp_path, interval=0.5)
    watcher.rebase(keep_pending=False)

    _write(target, "def one():\n    pass\n\ndef two():\n    return 2\n")
    watcher.poll()
    report = watcher.flush()

    assert report is not None
    change = report.changes[0]
    assert "two" in change.symbols_added, change.symbols_added
    assert "one" not in change.symbols_added, change.symbols_added
    assert "two" in report.symbol_digest()["added"]


def test_removed_function_shows_up_as_removed(tmp_path):
    target = tmp_path / "a.py"
    _write(target, "def keep():\n    pass\n\ndef gone():\n    pass\n")
    watcher = FolderWatcher(tmp_path, interval=0.5)
    watcher.rebase(keep_pending=False)

    _write(target, "def keep():\n    pass\n")
    watcher.poll()
    report = watcher.flush()

    assert report is not None
    assert "gone" in report.symbol_digest()["removed"]


def test_symbol_digest_carries_no_paths(tmp_path):
    """静默模式也要能上行：符号摘要里不能出现文件路径。"""
    (tmp_path / "pkg").mkdir()
    target = tmp_path / "pkg" / "secret_name.py"
    _write(target, "def alpha():\n    pass\n")
    watcher = FolderWatcher(tmp_path, interval=0.5)
    watcher.rebase(keep_pending=False)

    _write(target, "def alpha():\n    pass\n\ndef beta():\n    pass\n")
    watcher.poll()
    report = watcher.flush()

    digest = report.symbol_digest()
    assert digest["added"] == ["beta"]
    assert "secret_name" not in str(digest)


def test_default_idle_window_is_short(tmp_path):
    """默认窗口要短到演示时能看见结果；30 分钟等于「永远没反应」。"""
    watcher = FolderWatcher(tmp_path)
    assert watcher.idle_minutes == 3


def test_seconds_until_settle_counts_down(tmp_path):
    target = tmp_path / "a.py"
    _write(target, "one\n")
    watcher = FolderWatcher(tmp_path, idle_minutes=3, interval=0.5)
    watcher.rebase(keep_pending=False)

    assert watcher.seconds_until_settle() is None    # 没改动就没有倒计时

    _write(target, "one\ntwo\n")
    watcher.poll()
    left = watcher.seconds_until_settle()
    assert left is not None and 170 <= left <= 180, left

    watcher._last_change_ts -= 100                   # 静了 100 秒
    assert watcher.seconds_until_settle() <= 80


def test_new_comment_becomes_a_clue(tmp_path):
    """新增的单行注释要能作为「在做什么」的线索上行。"""
    target = tmp_path / "a.py"
    _write(target, "def one():\n    pass\n")
    watcher = FolderWatcher(tmp_path, interval=0.5)
    watcher.rebase(keep_pending=False)

    _write(target, "def one():\n    pass\n\n# 修复保存后列表不刷新的问题\n")
    watcher.poll()
    report = watcher.flush()

    assert report is not None
    notes = report.symbol_digest()["notes"]
    assert any("列表不刷新" in note for note in notes), notes


def test_secret_like_comment_is_dropped(tmp_path):
    """注释里出现「键 + 等号」一律不上行，宁可少一条线索。"""
    target = tmp_path / "a.py"
    _write(target, "def one():\n    pass\n")
    watcher = FolderWatcher(tmp_path, interval=0.5)
    watcher.rebase(keep_pending=False)

    _write(target, "def one():\n    pass\n\n# password=admin123 临时账号\n")
    watcher.poll()
    report = watcher.flush()

    notes = report.symbol_digest()["notes"]
    assert not any("admin123" in note for note in notes), notes


def test_python_comment_is_not_a_symbol(tmp_path):
    """Python 的 `# 说明` 是注释，不是「小节名」——曾经被当成函数名写进洞察。"""
    from client.core.watcher import _extract

    raw = "# 修复保存后列表不刷新\ndef real_fn():\n    pass\n".encode("utf-8")
    symbols, notes, lines = _extract(raw, ".py")
    assert symbols == ("real_fn",), symbols
    assert any("列表不刷新" in note for note in notes), notes


def test_markdown_heading_is_a_symbol(tmp_path):
    """Markdown 的标题才是小节名。"""
    from client.core.watcher import _extract

    raw = "# 方案总览\n\n## 数据流向\n".encode("utf-8")
    symbols, notes, lines = _extract(raw, ".md")
    assert "方案总览" in symbols and "数据流向" in symbols, symbols
    assert notes == (), notes


def test_plain_text_doc_shows_the_added_lines(tmp_path):
    """纯文本文档（没有 # 标题）也要能说出新增了哪几行。

    这是用户实际踩到的那条：read me.md 加了六行，分析只说「多了 6 行」。
    """
    target = tmp_path / "read me.md"
    _write(target, "这是测试文件 等待AI分析\n")
    watcher = FolderWatcher(tmp_path, interval=0.5)
    watcher.rebase(keep_pending=False)

    _write(target, "这是测试文件 等待AI分析\n看到此行请建议新增议题：测试\n前端已制作完毕\n")
    watcher.poll()
    report = watcher.flush()

    digest = report.symbol_digest()
    assert digest["added"] == [], digest
    assert any("新增议题" in line for line in digest["lines"]), digest["lines"]


def test_code_body_does_not_leak_into_lines(tmp_path):
    """代码文件只留骨架行：函数体里的语句不能上行。"""
    target = tmp_path / "svc.py"
    _write(target, "import os\n\ndef run():\n    pass\n")
    watcher = FolderWatcher(tmp_path, interval=0.5)
    watcher.rebase(keep_pending=False)

    _write(target, "import os\nimport json\n\ndef run():\n    secret_value = compute()\n    return 1\n")
    watcher.poll()
    report = watcher.flush()

    lines = report.symbol_digest()["lines"]
    assert any("import json" in line for line in lines), lines
    assert not any("secret_value" in line for line in lines), lines


def test_database_and_wal_are_not_scanned(tmp_path):
    """数据库、WAL、日志是运行产物，不能算作「用户改动」。

    真实踩过：被监听的仓库里放着 ``server/data.db``，服务端每 20 秒心跳会写一次
    WAL，文件大小随之增长；客户端把它当成改动，每次都把空闲窗口重置到当下，
    结果「改了半天代码，一条洞察都不出」，而且界面上完全看不出来。
    """
    _write(tmp_path / "app.py", "print(1)\n")
    (tmp_path / "data.db").write_bytes(b"x" * 10)
    (tmp_path / "data.db-wal").write_bytes(b"x" * 20)
    (tmp_path / "data.db-shm").write_bytes(b"x" * 5)
    (tmp_path / "server.log").write_text("line\n", encoding="utf-8")

    watcher = FolderWatcher(tmp_path, interval=0.5)
    names = {path.name for path in watcher._iter_files()}

    assert "app.py" in names
    assert not {"data.db", "data.db-wal", "data.db-shm", "server.log"} & names


def test_runtime_artifact_detection():
    """运行产物识别要覆盖数据库、WAL、日志，且不能误伤源码。"""
    from client.core.watcher import is_runtime_artifact

    assert is_runtime_artifact("data.db")
    assert is_runtime_artifact("data.db-wal")
    assert is_runtime_artifact("data.db-shm")
    assert is_runtime_artifact("server.log")
    assert is_runtime_artifact("events.jsonl.bak")
    assert not is_runtime_artifact("app.py")
    assert not is_runtime_artifact("README.md")


# ---------------------------------------------------------------- 漏检新文件的三种真实成因


def test_more_than_one_batch_is_not_dropped(tmp_path, monkeypatch):
    """一次新建的文件超过单批上限时，剩下的要排队发下一批，不能丢。

    真实库里有 8 条上报都恰好是 80 个文件——旧代码用 `[:80]` 切片后直接清空队列，
    多出来的文件永远不会再被上报，表现为「偶现没发现新文件」。
    """
    from client.core import watcher as watcher_mod

    monkeypatch.setattr(watcher_mod, "MAX_FILES_PER_REPORT", 2)

    watcher = FolderWatcher(tmp_path, interval=0.5)
    watcher.poll()
    for i in range(5):
        _write(tmp_path / f"f{i}.py", "def f():\n    pass\n")
    watcher.poll()

    first = watcher.flush()
    assert [change.path for change in first.changes] == ["f0.py", "f1.py"]
    assert watcher.pending_count == 3, "剩下的必须留在队列里，而不是被切片丢掉"

    rest: list[str] = []
    for _ in range(4):
        report = watcher.poll()
        if report is not None:
            rest += [change.path for change in report.changes]

    assert rest == ["f2.py", "f3.py", "f4.py"]
    assert watcher.pending_count == 0


def test_dot_github_and_common_dirs_are_scanned(tmp_path):
    """`.github` 与 dist/build/data 这类常见目录里的新文件必须能被发现。

    `.github` 之前被 `startswith('.git')` 顺手误杀；dist/build/data 则是被当成
    「构建产物」整目录忽略——用户把新模块放进去，就永远等不到洞察。
    """
    for rel in (".github/workflows/ci.yml", "dist/app.js", "build/out.py", "data/seed.json"):
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        _write(path, "x\n")

    watcher = FolderWatcher(tmp_path, interval=0.5)
    names = {path.relative_to(tmp_path).as_posix() for path in watcher._iter_files()}

    assert ".github/workflows/ci.yml" in names
    assert {"dist/app.js", "build/out.py", "data/seed.json"} <= names


def test_file_read_failure_is_retried(tmp_path, monkeypatch):
    """文件刚创建时被占用（读失败），下一轮必须重读，不能当成空文件。

    旧代码把「读失败」写成 lines=0 / digest=""，之后 mtime 与 size 都没变，
    diff 就再也不会看它一眼，内容永远进不了摘要。
    """
    target = tmp_path / "locked.py"
    _write(target, "def locked():\n    return 42\n")

    real = Path.read_bytes
    locked = {"on": True}

    def flaky(self):
        if self.name == "locked.py" and locked["on"]:
            raise OSError("file is locked by another process")
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", flaky)

    watcher = FolderWatcher(tmp_path, interval=0.5)
    watcher.poll()          # 基线：这一轮读不到，只能记成 unread
    assert watcher._baseline["locked.py"].unread is True
    assert watcher._baseline["locked.py"].digest == ""

    locked["on"] = False    # 占用解除
    watcher.poll()          # mtime/size 都没变，也必须补报内容
    report = watcher.flush()

    assert report is not None, "mtime/size 没变也要补报内容"
    assert [change.path for change in report.changes] == ["locked.py"]
    assert report.changes[0].added == 2
    assert report.changes[0].digest, "补齐后要带上内容指纹"
    assert report.changes[0].symbols_added == ("locked",)


def test_truncated_scan_does_not_report_deletions(tmp_path, monkeypatch):
    """扫描触顶时快照不完整：没扫到的文件不能被当成「删除了」。"""
    from client.core import watcher as watcher_mod

    for i in range(3):
        _write(tmp_path / f"f{i}.py", "def f():\n    pass\n")

    watcher = FolderWatcher(tmp_path, interval=0.5)
    watcher.poll()

    monkeypatch.setattr(watcher_mod, "MAX_FILES", 1)
    watcher.poll()

    assert watcher._truncated is True
    assert watcher.pending_count == 0, "触顶时不能凭空报删除"
