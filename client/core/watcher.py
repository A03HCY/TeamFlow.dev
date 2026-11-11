"""端侧文件变动捕捉：无人操作窗口 → 变动摘要 → 日志。

设计原则（对应改型方案 §2 的 ①②③）：

* **只取结构摘要，不传代码原文**：统计路径、大小、修改时间、行数增量，另外在本地
  抽出「符号清单」（函数/类/接口/文档小节名），用前后两轮的符号差来表示
  「这次到底加了什么」——符号名是结构信息，不是代码内容，静默模式下也能上行；
* **按窗口聚合**：文件夹连续 ``idle_minutes`` 分钟没有新的写入，才认为「这一轮干完了」，
  把这段时间攒下的改动打成一条日志，避免每保存一次就上报一条；
* **零第三方依赖**：用轮询快照而不是 watchdog，少一个安装步骤，跨平台行为一致。

只依赖标准库，可以单独跑::

    python -m client.main watch --root D:\\Projects\\TeamFlow.dev
"""
from __future__ import annotations

import hashlib
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

# 不看的目录：版本库、依赖、虚拟环境与工具缓存。
# 这里只放「确定不会放源码」的目录名。像 dist / build / data / out 这种
# 常见但可能放真实内容的目录不能一棍子打死——之前把它们整目录忽略，
# 结果「新建的文件怎么都不上报」，而且界面上完全看不出是被过滤了。
IGNORE_DIRS = {
    # 版本库
    ".git", ".svn", ".hg",
    # 依赖与虚拟环境：动辄几万个文件，会把 MAX_FILES 吃满，反而挤掉真实改动
    "node_modules", "bower_components", "jspm_packages",
    "__pycache__", ".venv", "venv", "env", ".eggs",
    # 工具缓存 / IDE
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", ".nox",
    ".idea", ".vscode", ".cache", ".parcel-cache", ".next", ".nuxt",
    "htmlcov", ".hypothesis",
    # 本项目自身产物：备份是二进制大文件，每生成一个都会被当成改动
    "backups",
}

#: 运行产物：数据库、WAL、日志每几秒就被服务端自己写一次。
#: 把它们算进「改动」会让空闲窗口永远无法结算——真实症状是「改了代码却
#: 一直不上报」，因为 ``data.db-wal`` 每 20 秒随心跳增长一次，每次都把
#: ``_last_change_ts`` 刷新到当下。这类文件不参与快照。
IGNORE_FILE_SUFFIXES = (".db", ".db-wal", ".db-shm", ".sqlite", ".sqlite3", ".log", ".bak")


def is_runtime_artifact(name: str) -> bool:
    """判断文件名是不是运行产物（数据库、WAL、日志、离线队列备份）。

    单独抽成函数是为了能直接单测：这类文件一旦漏进快照，空闲窗口就再也
    结算不了，而症状（一直不上报）和「没检测到改动」几乎一样，很难排查。
    """
    return name.lower().endswith(IGNORE_FILE_SUFFIXES)

# 不数行数的文件：二进制与体积大的产物（仍会被感知为「改动过」）
BINARY_SUFFIXES = {
    ".pyc", ".pyo", ".so", ".dll", ".dylib", ".exe", ".bin", ".zip", ".7z", ".rar",
    ".gz", ".tar", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".bmp", ".svgz",
    ".mp3", ".mp4", ".mov", ".avi", ".pdf", ".doc", ".docx", ".xls", ".xlsx",
    ".ppt", ".pptx", ".db", ".db-wal", ".db-shm", ".sqlite", ".sqlite3",
    ".woff", ".woff2", ".ttf", ".otf", ".map",
}

MAX_FILE_BYTES = 2 * 1024 * 1024   # 超过 2MB 的文件只记大小，不数行
MAX_FILES = 20000                  # 单次快照上限，防止误指到磁盘根目录
#: 单批上报的文件数上限。必须 ≤ 服务端 ``ENGINE_EVENT_MAX_FILES``（400）；
#: 超过上限的部分会留在待上报队列里下一批继续发，不会丢。
MAX_FILES_PER_REPORT = 400
MAX_SYMBOLS = 40                   # 单个文件最多保留多少个符号
MAX_NOTES_PER_FILE = 8             # 单个文件最多保留多少条注释线索
MAX_LINES_PER_FILE = 80            # 单个文件最多保留多少条「内容行」，够 diff 出新增片段
MAX_SCAN_LINES = 4000              # 只看前 4000 行，避免超大生成文件拖慢扫描
MAX_LINE_CHARS = 70                # 单条内容行的长度上限（超长直接丢，避免把代码整行带出去）

#: 代码文件里「能说明这段代码在干什么」的结构行：导入、装饰器/路由、定义语句。
#: 函数体内部语句不取——那才是代码原文；这里只要骨架。
STRUCTURE_LINE_PATTERNS = (
    re.compile(r"^\s*(?:from\s+\S+\s+import\s+.+|import\s+.+)$"),
    re.compile(r"^\s*@\w[\w.]*(?:\(.*\))?\s*$"),
    re.compile(r"^\s*(?:async\s+)?def\s+\w+\s*\(.*\)?\s*:?\s*$"),
    re.compile(r"^\s*class\s+\w+(?:\(.*\))?\s*:?\s*$"),
    re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+\w+\s*\(.*"),
    re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+[A-Za-z_$][\w$]*\s*="),
)

#: 各语言里「定义了一个新东西」的行。只取名字，不取函数体——
#: 这样洞察里能写「新增 rebase()、flush()」，而代码原文始终留在本机。
SYMBOL_PATTERNS = (
    re.compile(r"^\s*(?:async\s+)?def\s+([A-Za-z_]\w*)"),
    re.compile(r"^\s*func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)"),
    re.compile(r"^\s*(?:pub\s+)?(?:async\s+)?fn\s+([A-Za-z_]\w*)"),
    re.compile(r"^\s*class\s+([A-Za-z_]\w*)"),
    re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?(?:class|interface|enum|type|struct|trait)\s+([A-Za-z_]\w*)"),
    re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)"),
    re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*"
               r"(?:async\s*)?(?:function|\()"),
    re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*"
               r"(?:async\s*)?\([^)]*\)\s*=>"),
)

#: 纯文本类文件：内容行全量参与 diff（文档改了什么，本来就不是秘密）。
TEXT_SUFFIXES = {
    ".txt", ".text", ".log", ".csv", ".tsv", ".json", ".yaml", ".yml", ".toml",
    ".ini", ".cfg", ".conf", ".env.example", ".sql", ".html", ".htm", ".css",
}

#: 只有 Markdown 里的 ``# 标题`` 才是「小节名」；Python 里的 ``#`` 是注释，
#: 混在一起会把「# 修复列表不刷新」当成一个函数名（真踩过）。
MARKDOWN_SUFFIXES = {".md", ".mdx", ".markdown", ".rst"}
MARKDOWN_SYMBOL_PATTERNS = (re.compile(r"^#{1,3}\s+(.{2,60})$"),)

#: 单行注释 / 单行文档字符串。它们是本地人对这次改动的解释，比路径有信息量。
NOTE_PATTERNS = (
    re.compile(r"^\s*#\s*(.{4,70})$"),
    re.compile(r"^\s*//\s*(.{4,70})$"),
    re.compile(r"^\s*\*\s+(.{4,70})$"),
    re.compile(r'^\s*"""(?!")?(.{4,70}?)"""\s*$'),
    re.compile(r"^\s*'''(?!')?(.{4,70}?)'''\s*$"),
)


def _symbols_of(raw: bytes, limit: int = MAX_SYMBOLS) -> tuple[str, ...]:
    """从文件文本里抽出符号名（本地执行，原文不出本机）。"""
    return _extract(raw)[0]


def _extract(
    raw: bytes, suffix: str = "", limit: int = MAX_SYMBOLS
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """一次扫描抽出（符号名, 注释线索, 内容行）。

    符号模式按文件类型选：Markdown 用标题，代码用定义语句。

    内容行是「这次到底多了哪几行」的最小可用形式：

    * 文档（md/txt/rst）：保留每一行可读文字，纯文本也能 diff 出新增了什么；
    * 代码：只保留骨架行（导入、装饰器/路由、定义签名），函数体一律不取。

    三者都不含完整代码原文，长度上限 70 字符，命中疑似密钥直接丢。
    """
    try:
        text = raw.decode("utf-8", "ignore")
    except Exception:  # noqa: BLE001 - 解码失败就当二进制
        return (), (), ()
    if not text:
        return (), (), ()
    is_markdown = suffix.lower() in MARKDOWN_SUFFIXES
    is_text = is_markdown or suffix.lower() in TEXT_SUFFIXES
    symbol_patterns = MARKDOWN_SYMBOL_PATTERNS if is_markdown else SYMBOL_PATTERNS
    symbols: list[str] = []
    notes: list[str] = []
    lines: list[str] = []
    seen_symbol: set[str] = set()
    seen_note: set[str] = set()
    seen_line: set[str] = set()
    for raw_line in text.splitlines()[:MAX_SCAN_LINES]:
        line = raw_line.strip()
        for pattern in symbol_patterns:
            match = pattern.match(raw_line)
            if not match:
                continue
            name = match.group(1).strip()
            if name and name not in seen_symbol and len(name) <= 60:
                seen_symbol.add(name)
                symbols.append(name)
            break
        if not is_markdown and len(notes) < MAX_NOTES_PER_FILE:
            for pattern in NOTE_PATTERNS:
                match = pattern.match(raw_line)
                if not match:
                    continue
                note = " ".join(match.group(1).split())
                if _keep_note(note) and note not in seen_note:
                    seen_note.add(note)
                    notes.append(note)
                break
        if len(lines) < MAX_LINES_PER_FILE and line:
            if _looks_like_content(line, is_text):
                squeezed = " ".join(line.split())
                if squeezed not in seen_line:
                    seen_line.add(squeezed)
                    lines.append(squeezed)
        if (
            len(symbols) >= limit
            and len(notes) >= MAX_NOTES_PER_FILE
            and len(lines) >= MAX_LINES_PER_FILE
        ):
            break
    return tuple(symbols), tuple(notes), tuple(lines)


def _looks_like_content(line: str, is_text: bool) -> bool:
    """这一行值不值得留下来做 diff：太短/太长/像密钥/纯符号的都不要。"""
    if not 3 <= len(line) <= MAX_LINE_CHARS:
        return False
    if not any(ch.isalnum() for ch in line):
        return False
    if _looks_secret(line):
        return False
    if is_text:
        return True
    if line.startswith(("#", "//", "/*", "*", "--")):
        return True                      # 注释已在 notes 里，这里保留是为了 diff 稳定
    return any(pattern.match(line) for pattern in STRUCTURE_LINE_PATTERNS)


def _looks_secret(text: str) -> bool:
    """粗筛密钥样式：宁可不留这一行，也不冒泄漏风险。"""
    lowered = text.lower()
    if any(word in lowered for word in ("api_key", "apikey", "secret", "password", "passwd")):
        return True
    if re.search(r"\b(?:sk|ghp|gho|xoxb|akia)[-_a-z0-9]{12,}", lowered):
        return True
    if "=" in lowered and any(word in lowered for word in ("key", "token", "secret")):
        return True
    return False


def _keep_note(note: str) -> bool:
    if not 4 <= len(note) <= 70:
        return False
    lowered = note.lower()
    if lowered.startswith(("http://", "https://", "coding:", "-*-")):
        return False
    if any(mark in lowered for mark in ("noqa", "type: ignore", "eslint", "prettier", "pragma")):
        return False
    # 注释里出现「键 + 等号」一律不传：宁可少一条线索，也不冒泄密风险
    if "=" in note and any(word in lowered for word in ("key", "token", "secret", "password", "passwd")):
        return False
    return True



def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _same_content(before: "FileState", after: "FileState") -> bool:
    """mtime 变了，但内容其实没变（编辑器 touch、同内容另存）。

    有内容指纹就比指纹；二进制/超大文件没有指纹，退化成「大小 + 行数」判断。
    """
    if before.size != after.size:
        return False
    if before.digest and after.digest:
        return before.digest == after.digest
    return before.lines == after.lines


@dataclass
class FileState:
    mtime: float
    size: int
    lines: int
    digest: str = ""     # 内容指纹；二进制/超大文件留空（只靠大小判断）
    symbols: tuple[str, ...] = ()   # 本地抽出的符号清单（函数/类/文档小节）
    notes: tuple[str, ...] = ()     # 本地抽出的注释线索（单行说明）
    content: tuple[str, ...] = ()   # 本地抽出的内容行（文档正文 / 代码骨架）
    unread: bool = False  # 这一轮没读到内容（被占用/权限），下一轮要重试


@dataclass
class Change:
    path: str
    status: str          # added / modified / deleted
    added: int = 0
    removed: int = 0
    size: int = 0
    digest: str = ""
    symbols_added: tuple[str, ...] = ()
    symbols_removed: tuple[str, ...] = ()
    notes_added: tuple[str, ...] = ()
    lines_added: tuple[str, ...] = ()

    def as_file(self) -> dict:
        payload = {
            "path": self.path,
            "status": self.status,
            "added": int(self.added),
            "removed": int(self.removed),
        }
        # 符号是结构摘要而不是代码原文，静默模式下也允许上行
        if self.symbols_added:
            payload["symbols_added"] = list(self.symbols_added)[:12]
        if self.symbols_removed:
            payload["symbols_removed"] = list(self.symbols_removed)[:12]
        if self.notes_added:
            payload["notes_added"] = list(self.notes_added)[:4]
        if self.lines_added:
            payload["lines_added"] = list(self.lines_added)[:8]
        return payload


@dataclass
class WindowReport:
    """一个无人操作窗口的完整结果：日志摘要 + 供服务端推断的事件素材。"""

    window_start: str
    window_end: str
    idle_minutes: int
    changes: list[Change] = field(default_factory=list)

    @property
    def file_count(self) -> int:
        return len(self.changes)

    @property
    def additions(self) -> int:
        return sum(c.added for c in self.changes)

    @property
    def deletions(self) -> int:
        return sum(c.removed for c in self.changes)

    def counts(self) -> dict:
        buckets = {"added": 0, "modified": 0, "deleted": 0}
        for change in self.changes:
            buckets[change.status] = buckets.get(change.status, 0) + 1
        return buckets

    def summary(self) -> str:
        """给人看的一段话：新增/修改/删除各几个，具体动了哪些文件与符号。"""
        buckets = self.counts()
        head = (
            f"{self.idle_minutes} 分钟无操作，共 {self.file_count} 个文件改动"
            f"（新增 {buckets.get('added', 0)} / 修改 {buckets.get('modified', 0)}"
            f" / 删除 {buckets.get('deleted', 0)}，+{self.additions} / -{self.deletions} 行）"
        )
        if not self.changes:
            return head
        ranked = sorted(self.changes, key=lambda c: (c.added + c.removed), reverse=True)
        shown = ranked[:8]
        lines = [head, "主要改动："]
        for change in shown:
            mark = {"added": "＋", "modified": "～", "deleted": "－"}.get(change.status, "·")
            detail = ""
            if change.symbols_added:
                detail += " 新增 " + "、".join(change.symbols_added[:4])
            if change.symbols_removed:
                detail += " 移除 " + "、".join(change.symbols_removed[:4])
            if change.notes_added:
                detail += " 说明：" + "；".join(change.notes_added[:2])
            if change.lines_added:
                detail += " 新增内容：" + " / ".join(change.lines_added[:2])
            lines.append(f"  {mark} {change.path}  +{change.added}/-{change.removed}{detail}")
        if len(ranked) > len(shown):
            lines.append(f"  …还有 {len(ranked) - len(shown)} 个文件")
        return "\n".join(lines)

    def symbol_digest(self, limit: int = 16) -> dict:
        """这批改动的结构摘要（不含路径，静默模式也能安全上行）。

        四个维度合起来才回答得了「这六行是干什么的」：

        * ``added``/``removed``：新增/移除了哪些函数、类、文档小节；
        * ``notes``：新增的单行注释（写代码的人自己的解释）；
        * ``lines``：新增的内容行——文档是正文，代码是骨架（导入/路由/签名）。
        """
        added: list[str] = []
        removed: list[str] = []
        notes: list[str] = []
        lines: list[str] = []
        for change in sorted(self.changes, key=lambda c: c.path):
            for name in change.symbols_added:
                if name not in added:
                    added.append(name)
            for name in change.symbols_removed:
                if name not in removed:
                    removed.append(name)
            for note in change.notes_added:
                if note not in notes:
                    notes.append(note)
            for line in change.lines_added:
                if line not in lines:
                    lines.append(line)
        return {
            "added": added[:limit],
            "removed": removed[:limit],
            "notes": notes[:8],
            "lines": lines[:10],
        }

    def log_payload(self, *, repo: str, branch: str) -> dict:
        return {
            "repo": repo,
            "branch": branch,
            "window_start": self.window_start,
            "window_end": self.window_end,
            "idle_minutes": self.idle_minutes,
            "file_count": self.file_count,
            "additions": self.additions,
            "deletions": self.deletions,
            "summary": self.summary(),
        }

    def event_files(self) -> list[dict]:
        return [change.as_file() for change in self.changes]

    def signature(self) -> str:
        """这批改动的指纹：文件集合 + 状态 + 增删行 + 内容指纹 + 符号。

        用来判断「是不是和上次同步的那一批完全一样」——一样就不再重复上报。
        """
        digest = hashlib.sha256()
        for change in sorted(self.changes, key=lambda c: c.path):
            digest.update(
                f"{change.path}|{change.status}|{change.added}|{change.removed}|"
                f"{change.digest}|{','.join(change.symbols_added)}|"
                f"{','.join(change.symbols_removed)}|{','.join(change.notes_added)}|"
                f"{','.join(change.lines_added)}\n"
                .encode("utf-8", "replace")
            )
        return digest.hexdigest()


class FolderWatcher:
    """按快照比对捕捉文件夹变动，并按「无人操作窗口」切分日志。"""

    def __init__(
        self,
        root: str | Path,
        *,
        idle_minutes: int = 3,
        interval: float = 5.0,
        ignore_dirs: Iterable[str] | None = None,
        max_bytes: int = MAX_FILE_BYTES,
    ) -> None:
        self.root = Path(root).expanduser().resolve()
        if not self.root.exists():
            raise FileNotFoundError(f"目录不存在：{self.root}")
        if not self.root.is_dir():
            raise NotADirectoryError(f"不是文件夹：{self.root}")
        self.idle_minutes = max(1, int(idle_minutes))
        self.interval = max(0.5, float(interval))
        self.ignore_dirs = set(ignore_dirs or IGNORE_DIRS)
        self.max_bytes = max_bytes
        self._window_start: str | None = None
        self._last_change_ts: float | None = None
        self._pending: dict[str, Change] = {}
        self._baseline: dict[str, FileState] | None = None
        # 上一轮快照是否因 MAX_FILES 触顶：触顶时不判删除，否则会把没扫到的文件误报成删除
        self._truncated = False
        # 文本元数据缓存：键是相对路径，值是 (mtime_ns, size, lines, digest, symbols, notes)。
        # 一轮扫描只读一次文件就把行数/指纹/符号/注释都算出来（旧版要读两遍），
        # 而且 mtime+size 没变就直接复用，5 秒一轮不会反复啃全盘。
        self._meta_cache: dict[
            str,
            tuple[
                int, int, int, str,
                tuple[str, ...], tuple[str, ...], tuple[str, ...],
            ],
        ] = {}

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def seconds_until_settle(self) -> int | None:
        """还要等多少秒才自动结算；没有待上报改动时返回 None。"""
        if not self._pending or self._last_change_ts is None:
            return None
        left = self.idle_minutes * 60 - (time.time() - self._last_change_ts)
        return max(0, int(left))

    # ---------------------------------------------------------------- 快照

    def _iter_files(self):
        count = 0
        self._truncated = False
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = sorted(
                name for name in dirnames if name not in self.ignore_dirs
            )
            for name in filenames:
                if count >= MAX_FILES:
                    # 触顶：这一轮快照不完整，不能再拿它当「全部文件」去判删除
                    self._truncated = True
                    return
                if is_runtime_artifact(name):
                    continue
                path = Path(dirpath) / name
                try:
                    if path.is_symlink():
                        continue
                except OSError:
                    continue
                count += 1
                yield path

    def _text_meta(
        self, path: Path, rel: str, size: int, mtime_ns: int
    ) -> tuple[int, str, tuple[str, ...], tuple[str, ...], tuple[str, ...], bool]:
        """一次读取算出（行数, 内容指纹, 符号, 注释线索, 内容行, 是否没读到）。

        mtime/size 没变就复用缓存；二进制与超大文件不读：
        行数记 0、指纹留空，靠 size 判断变化。
        读失败（被占用、刚创建还没落盘）会标记 ``unread``：这种状态绝不能
        当成「文件就是空的」写进基线，否则下一轮 mtime/size 没变就再也不会重读。
        """
        if path.suffix.lower() in BINARY_SUFFIXES or size > self.max_bytes:
            return 0, "", (), (), (), False
        cached = self._meta_cache.get(rel)
        if cached and cached[0] == mtime_ns and cached[1] == size:
            return cached[2], cached[3], cached[4], cached[5], cached[6], False
        try:
            raw = path.read_bytes()
        except OSError:
            return 0, "", (), (), (), True
        digest = hashlib.sha256(raw).hexdigest()
        lines = raw.count(b"\n") + (1 if raw and not raw.endswith(b"\n") else 0)
        symbols, notes, content = _extract(raw, path.suffix)
        self._meta_cache[rel] = (mtime_ns, size, lines, digest, symbols, notes, content)
        return lines, digest, symbols, notes, content, False

    def snapshot(self) -> dict[str, FileState]:
        states: dict[str, FileState] = {}
        for path in self._iter_files():
            try:
                stat = path.stat()
            except OSError:
                continue
            rel = path.relative_to(self.root).as_posix()
            lines, digest, symbols, notes, content, unread = self._text_meta(
                path, rel, stat.st_size, stat.st_mtime_ns
            )
            states[rel] = FileState(
                mtime=stat.st_mtime,
                size=stat.st_size,
                lines=lines,
                digest=digest,
                symbols=symbols,
                notes=notes,
                content=content,
                unread=unread,
            )
        return states

    # ---------------------------------------------------------------- 比对

    def diff(
        self, before: dict[str, FileState], after: dict[str, FileState], *, deletions: bool = True
    ) -> list[Change]:
        """比对两轮快照。

        ``deletions=False`` 用于「这一轮快照可能不完整」（扫描触顶）的情况：
        没扫到的文件不能当成被删掉。
        """
        changes: list[Change] = []
        for rel, state in after.items():
            old = before.get(rel)
            if old is None:
                changes.append(
                    Change(rel, "added", added=state.lines, removed=0,
                           size=state.size, digest=state.digest,
                           symbols_added=state.symbols, notes_added=state.notes,
                           lines_added=state.content)
                )
            elif (
                old.mtime != state.mtime
                or old.size != state.size
                # 上一轮没读到内容（被占用），这一轮读到了：必须补报，
                # 否则 mtime/size 都没变，内容永远进不了洞察
                or (old.unread and not state.unread)
            ):
                if _same_content(old, state):
                    # 只是被 touch、或另存为同样的内容：不算改动，别重复上报
                    continue
                delta = state.lines - old.lines
                old_symbols = set(old.symbols)
                new_symbols = set(state.symbols)
                old_notes = set(old.notes)
                old_lines = set(old.content)
                changes.append(
                    Change(rel, "modified", added=max(delta, 0), removed=max(-delta, 0),
                           size=state.size, digest=state.digest,
                           symbols_added=tuple(s for s in state.symbols if s not in old_symbols),
                           symbols_removed=tuple(s for s in old.symbols if s not in new_symbols),
                           notes_added=tuple(s for s in state.notes if s not in old_notes),
                           # 新增了哪几行：文档是正文，代码是骨架行（导入/装饰器/签名）
                           lines_added=tuple(s for s in state.content if s not in old_lines))
                )
        for rel, old in before.items():
            if rel not in after and deletions:
                changes.append(
                    Change(rel, "deleted", added=0, removed=old.lines,
                           symbols_removed=old.symbols)
                )
        return changes

    # ---------------------------------------------------------------- 窗口

    @staticmethod
    def _merge_symbols(older: Iterable[str], newer: Iterable[str]) -> tuple[str, ...]:
        """合并两轮的符号清单：保持首次出现的顺序，去掉重复。"""
        out: list[str] = []
        for name in list(older) + list(newer):
            if name and name not in out:
                out.append(name)
        return tuple(out)

    def _merge(self, changes: list[Change]) -> None:
        for change in changes:
            prev = self._pending.get(change.path)
            if prev is None:
                self._pending[change.path] = change
                continue
            added_symbols = self._merge_symbols(prev.symbols_added, change.symbols_added)
            removed_symbols = self._merge_symbols(prev.symbols_removed, change.symbols_removed)
            added_notes = self._merge_symbols(prev.notes_added, change.notes_added)
            added_lines = self._merge_symbols(prev.lines_added, change.lines_added)
            if prev.status == "added" and change.status == "modified":
                self._pending[change.path] = Change(
                    change.path, "added", prev.added + change.added, change.removed,
                    size=change.size, digest=change.digest,
                    symbols_added=added_symbols, symbols_removed=removed_symbols,
                    notes_added=added_notes, lines_added=added_lines,
                )
            else:
                self._pending[change.path] = Change(
                    change.path,
                    change.status if change.status == "deleted" else prev.status,
                    prev.added + change.added,
                    prev.removed + change.removed,
                    size=change.size or prev.size,
                    digest=change.digest or prev.digest,
                    symbols_added=added_symbols,
                    symbols_removed=removed_symbols,
                    notes_added=added_notes,
                    lines_added=added_lines,
                )

    def rebase(self, *, keep_pending: bool = True) -> None:
        """重建基线（启动 / 暂停恢复 / 换目录后调用）。

        ``keep_pending`` 保留还没上报的改动，但把窗口计时重置到现在：
        这样暂停几小时后恢复，不会把这段时间的改动算进同一个窗口，
        也不会把还没上传的内容悄悄丢掉。
        """
        self._baseline = self.snapshot()
        if not keep_pending:
            self._pending.clear()
        if self._pending:
            self._window_start = self._window_start or _now_iso()
            self._last_change_ts = time.time()
        else:
            self._window_start = _now_iso()
            self._last_change_ts = None

    def _take_report(self) -> WindowReport | None:
        """取出一批待上报改动。

        超过 ``MAX_FILES_PER_REPORT`` 的部分**留在队列里**，把计时推到「窗口已满」，
        下一轮 poll 立刻接着发下一批。旧版是直接切片后清空队列，一次新建 80 个以上
        文件时，多出来的会被静默丢掉——这就是「偶现没发现新文件」的主要原因。
        """
        if not self._pending:
            return None
        ordered = sorted(self._pending.items(), key=lambda item: item[0])
        batch = ordered[:MAX_FILES_PER_REPORT]
        rest = ordered[MAX_FILES_PER_REPORT:]
        report = WindowReport(
            window_start=self._window_start or _now_iso(),
            window_end=_now_iso(),
            idle_minutes=self.idle_minutes,
            changes=[change for _, change in batch],
        )
        self._pending = dict(rest)
        self._window_start = _now_iso()
        if rest:
            self._last_change_ts = time.time() - self.idle_minutes * 60
        else:
            self._last_change_ts = None
        return report

    def poll(self) -> WindowReport | None:
        """推进一次检测；若刚好满一个无人操作窗口，返回该窗口的报告。"""
        current = self.snapshot()
        if self._baseline is None:
            self._baseline = current
            self._window_start = _now_iso()
            return None

        changes = self.diff(self._baseline, current, deletions=not self._truncated)
        now = time.time()
        if changes:
            if self._window_start is None:
                self._window_start = _now_iso()
            self._merge(changes)
            self._baseline = current
            self._last_change_ts = now
            return None

        if not self._pending or self._last_change_ts is None:
            return None

        if now - self._last_change_ts >= self.idle_minutes * 60:
            return self._take_report()
        return None

    def flush(self) -> WindowReport | None:
        """立刻结算当前攒下的改动（「立即分析」按钮用），不等无人操作窗口。

        结算前先补一次比对：上一次 poll 之后（最多 interval 秒）的改动也要算进来，
        否则点「立即分析」时刚保存的文件会被漏掉。
        """
        if self._baseline is not None:
            try:
                current = self.snapshot()
            except Exception:  # noqa: BLE001 - 扫描失败不应让手动结算直接报错
                current = None
            if current is not None:
                self._merge(self.diff(self._baseline, current, deletions=not self._truncated))
                self._baseline = current
        return self._take_report()

    def run(
        self,
        on_window: Callable[[WindowReport], None],
        *,
        stop: Callable[[], bool] | None = None,
    ) -> None:
        """常驻循环：每个窗口结束就回调一次。``stop`` 返回 True 时退出。"""
        while True:
            try:
                report = self.poll()
            except Exception:  # noqa: BLE001 - 单次扫描失败不应终止探针
                report = None
            if report is not None and report.changes:
                on_window(report)
            if stop is not None and stop():
                return
            time.sleep(self.interval)


def path_hash(path: str | Path) -> str:
    """仓库标识：只用路径哈希，不上送真实路径。"""
    digest = hashlib.sha256(str(Path(path).expanduser().resolve()).encode("utf-8")).hexdigest()
    return digest[:16]


def current_branch(root: str | Path) -> str:
    """尽力读当前 git 分支；不是 git 仓库时返回空串。"""
    try:
        raw = (Path(root) / ".git" / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    if raw.startswith("ref:"):
        return raw.rsplit("/", 1)[-1]
    return ""
