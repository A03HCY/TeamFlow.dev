"""反向查询沙箱：服务端点名要哪些文件，端侧在这里安全地回答。

设计约束（与方案 §4.1 的 ``query.py`` 一致，但只做 SDK 需要的部分）：

* **只能读指定根目录内的文件**：拒绝绝对路径、盘符、``..``、NUL，
  并且用 ``Path.resolve()`` 再校验一次，挡住符号链接逃逸；
* **默认只回元信息**（大小 / 摘要 / 行数 / 语言 / 修改时间），
  ``mode="read"`` 才回内容，而且必须显式允许（``allow_content=True``）；
* **有上限**：一次最多 ``MAX_FILES`` 个文件，单个文件最多读 ``max_bytes``，
  超出部分截断并在回答里标注 ``truncated``；
* **只回文本**：二进制或不在白名单后缀的文件，``read`` 模式一律拒答；
* **内容先脱敏**：命中 ``sk-`` / ``AKIA`` / 私钥头 / ``password=`` 这类样式时，
  用 ``[REDACTED]`` 替换并计数，避免把密钥送上云。

典型用法::

    from client.core.query import QuerySandbox

    client.on_query_request = QuerySandbox(r"D:\\Projects\\app", allow_content=False)

``QuerySandbox`` 是同步可调用对象，``EdgeClient`` 也接受返回协程的实现。
"""
from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

MAX_FILES = 50
DEFAULT_MAX_BYTES = 64 * 1024
HARD_MAX_BYTES = 256 * 1024
# 超过这个大小就不数行数了（避免为了一个「行数」把几百 MB 读一遍）
LINE_COUNT_LIMIT = 4 * 1024 * 1024

# 允许 read 的文本后缀（够覆盖常见源码/配置/文档；二进制一律拒绝）
TEXT_SUFFIXES = frozenset(
    {
        ".py", ".pyi", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".vue",
        ".go", ".rs", ".java", ".kt", ".c", ".h", ".cc", ".cpp", ".hpp",
        ".cs", ".rb", ".php", ".swift", ".scala", ".sh", ".ps1", ".bat",
        ".sql", ".json", ".jsonc", ".toml", ".ini", ".cfg", ".conf", ".env",
        ".yaml", ".yml", ".xml", ".html", ".css", ".scss", ".less",
        ".md", ".mdx", ".rst", ".txt", ".csv", ".gitignore", ".gitattributes",
        ".dockerfile", ".tf", ".proto", ".graphql", ".lua", ".r", ".m",
    }
)

# 与 server/app/services/inference/events.py 的 SECRET_PATTERNS 保持同一套口径，
# 另外补上「赋值式密钥」与邮箱，避免内容里顺手带出去的凭据。
SECRET_PATTERNS: tuple[tuple[str, "re.Pattern[str]"], ...] = tuple(
    (name, re.compile(pattern))
    for name, pattern in (
        ("private_key", r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----"),
        ("aws_key", r"\bAKIA[0-9A-Z]{16}\b"),
        ("openai_key", r"\bsk-[A-Za-z0-9_\-]{16,}\b"),
        ("github_token", r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
        ("slack_token", r"\bxox[abposr]-[A-Za-z0-9\-]{10,}\b"),
        ("assign", r"(?i)\b(?:password|passwd|secret|api_key|apikey|access_token|token)\b\s*[:=]\s*['\"]?[^\s'\"]{6,}"),
        ("email", r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"),
    )
)

LANG_BY_SUFFIX = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".jsx": "javascript",
    ".vue": "vue",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".kt": "kotlin",
    ".c": "c",
    ".h": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".hpp": "cpp",
    ".cs": "csharp",
    ".rb": "ruby",
    ".php": "php",
    ".swift": "swift",
    ".sh": "shell",
    ".ps1": "powershell",
    ".sql": "sql",
    ".json": "json",
    ".toml": "toml",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".xml": "xml",
    ".html": "html",
    ".css": "css",
    ".scss": "scss",
    ".md": "markdown",
    ".mdx": "markdown",
    ".rst": "rst",
    ".txt": "text",
}


def lang_for(path: str) -> str:
    """按后缀猜语言，未收录的统一叫 ``text``。"""
    suffix = Path(str(path)).suffix.lower()
    return LANG_BY_SUFFIX.get(suffix, "text")


def is_text_path(path: str) -> bool:
    """``read`` 模式是否允许读这个后缀（不认识的按二进制处理）。"""
    name = Path(str(path)).name.lower()
    if name in TEXT_SUFFIXES:  # .gitignore / .env / Dockerfile 这类无后缀文件名
        return True
    return Path(str(path)).suffix.lower() in TEXT_SUFFIXES


def redact_text(text: str) -> tuple[str, dict[str, int]]:
    """把疑似密钥/邮箱替换成 ``[REDACTED]``，返回（新文本，按类型的计数）。"""
    counts: dict[str, int] = {}
    result = text
    for name, pattern in SECRET_PATTERNS:
        result, hits = pattern.subn("[REDACTED]", result)
        if hits:
            counts[name] = hits
    return result, counts


def _looks_binary(data: bytes) -> bool:
    return b"\x00" in data[:4096]


def file_fingerprint(path: str | Path, *, max_bytes: int = DEFAULT_MAX_BYTES) -> dict:
    """给 ``stat`` 模式用：大小、sha256、行数（只数到上限内）、语言、修改时间。"""
    target = Path(path)
    stat = target.stat()
    size = int(stat.st_size)
    digest = hashlib.sha256()
    lines = 0
    count_lines = size <= LINE_COUNT_LIMIT
    with target.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
            if count_lines:
                lines += chunk.count(b"\n")
    return {
        "ok": True,
        "size": size,
        "sha256": digest.hexdigest(),
        "lines": lines if count_lines else None,
        "lang": lang_for(target.name),
        "modified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(stat.st_mtime)),
        "truncated": size > max_bytes,
    }


@dataclass
class QueryRequest:
    """``on_query_request`` 收到的一次查询。"""

    request_id: str = ""
    files: tuple[str, ...] = ()
    mode: str = "stat"
    max_bytes: int = DEFAULT_MAX_BYTES
    reason: str = ""

    @classmethod
    def from_frame(cls, frame: dict | None) -> "QueryRequest":
        data = frame or {}
        raw = data.get("files")
        if raw is None:
            raw = data.get("paths")
        if isinstance(raw, str):
            raw = [raw]
        files = tuple(str(item) for item in (raw or []) if str(item or "").strip())
        try:
            max_bytes = int(data.get("max_bytes") or DEFAULT_MAX_BYTES)
        except (TypeError, ValueError):
            max_bytes = DEFAULT_MAX_BYTES
        mode = str(data.get("mode") or "stat")
        return cls(
            request_id=str(data.get("request_id") or ""),
            files=files,
            mode=mode if mode in ("stat", "read") else "stat",
            max_bytes=max(1024, min(HARD_MAX_BYTES, max_bytes)),
            reason=str(data.get("reason") or ""),
        )


class QuerySandbox:
    """可调用对象：``QuerySandbox(root, allow_content=False)``。

    ``allow_content`` 为假时，``mode="read"`` 会返回错误说明而不是内容
    （默认不上行代码行，要开内容必须由部署方显式打开）。
    """

    def __init__(
        self,
        root: str | Path,
        *,
        allow_content: bool = False,
        max_files: int = MAX_FILES,
        max_bytes: int = DEFAULT_MAX_BYTES,
        suffixes: Iterable[str] | None = None,
    ) -> None:
        self.root = Path(root).expanduser().resolve()
        self.allow_content = bool(allow_content)
        self.max_files = max(1, int(max_files))
        self.max_bytes = max(1024, min(HARD_MAX_BYTES, int(max_bytes)))
        self.suffixes = frozenset(suffixes) if suffixes else TEXT_SUFFIXES

    # -- 路径安全
    def resolve(self, relative: str) -> Path:
        """把请求里的相对路径解析成绝对路径；不安全就抛 ``ValueError``。"""
        text = str(relative or "").strip().replace("\\", "/")
        if not text:
            raise ValueError("文件路径为空")
        if "\x00" in text:
            raise ValueError("文件路径包含非法字符")
        if text.startswith("/") or re.match(r"^[A-Za-z]:", text) or text.startswith("~"):
            raise ValueError("只能查询仓库内的相对路径")
        if any(part == ".." for part in text.split("/")):
            raise ValueError("文件路径不能包含上级目录")
        candidate = (self.root / text).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise ValueError("文件路径越出了允许的根目录") from exc
        return candidate

    # -- 单个文件
    def describe(self, relative: str, *, mode: str, max_bytes: int) -> dict:
        try:
            target = self.resolve(relative)
        except ValueError as exc:
            return {"ok": False, "path": relative, "error": str(exc)}
        if not target.is_file():
            return {"ok": False, "path": relative, "error": "文件不存在或不是普通文件"}
        try:
            info = file_fingerprint(target, max_bytes=max_bytes)
        except OSError as exc:
            return {"ok": False, "path": relative, "error": f"读取失败：{exc}"}
        info["path"] = relative
        if mode != "read":
            return info
        if not self.allow_content:
            info["content_allowed"] = False
            info["error"] = "端侧未开启内容读取（只回元信息）"
            return info
        if not self._readable(target):
            info["error"] = "该文件类型不允许读取内容"
            return info
        try:
            data = target.read_bytes()[: max(1024, max_bytes)]
        except OSError as exc:
            info["error"] = f"读取失败：{exc}"
            return info
        if _looks_binary(data):
            info["error"] = "疑似二进制文件，拒绝返回内容"
            return info
        text, counts = redact_text(data.decode("utf-8", "replace"))
        info["content"] = text
        info["truncated"] = bool(info.get("truncated")) or len(data) >= max_bytes
        info["redactions"] = counts
        return info

    def _readable(self, target: Path) -> bool:
        name = target.name.lower()
        if name in self.suffixes:
            return True
        return target.suffix.lower() in self.suffixes

    # -- 一次查询
    def answer(self, request: "QueryRequest | dict") -> dict:
        req = request if isinstance(request, QueryRequest) else QueryRequest.from_frame(request)
        limit = min(req.max_bytes, self.max_bytes)
        files: dict[str, dict] = {}
        for relative in list(req.files)[: self.max_files]:
            files[relative] = self.describe(relative, mode=req.mode, max_bytes=limit)
        result: dict[str, Any] = {
            "request_id": req.request_id,
            "mode": req.mode,
            "files": files,
        }
        if len(req.files) > self.max_files:
            result["error"] = f"一次最多查询 {self.max_files} 个文件，已忽略多余的 {len(req.files) - self.max_files} 个"
        if not files:
            result["error"] = "请求里没有可查询的文件"
        return result

    def __call__(self, request: "QueryRequest | dict") -> dict:
        return self.answer(request)


__all__ = [
    "DEFAULT_MAX_BYTES",
    "HARD_MAX_BYTES",
    "LANG_BY_SUFFIX",
    "MAX_FILES",
    "QueryRequest",
    "QuerySandbox",
    "SECRET_PATTERNS",
    "TEXT_SUFFIXES",
    "file_fingerprint",
    "is_text_path",
    "lang_for",
    "redact_text",
]
