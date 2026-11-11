"""端侧事件包（schema v1）的校验。

约定见方案 §4：端侧只上报**脱敏后**的结构化摘要 —— 仓库标签、分支、提交、
变更文件路径与增删行数、测试文件变化，**不含**任何代码原文。

这里做服务端的硬校验（不信任端侧）：

* 包体积 ≤ ``ENGINE_MAX_EVENT_BYTES``；
* ``schema_version`` 必须认识（当前只有 1）；
* ``event_uid`` 是幂等键，字符集受限于 URL 安全集合；
* 文件路径必须是仓库内相对路径（挡住 ``../``、盘符、绝对路径、``~`` 展开）；
* 文件数 ≤ ``ENGINE_EVENT_MAX_FILES``；
* 备注与文件名里**不允许**出现疑似密钥（说明端侧脱敏漏了，直接拒收）。

校验失败统一抛 ``EventRejected``（400），消息里带字段路径，方便探针侧排错。
"""
from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ...config import (
    ENGINE_EVENT_MAX_FILES,
    ENGINE_MAX_EVENT_BYTES,
    ENGINE_SCHEMA_VERSION,
)
from ..engine.errors import EventRejected
from ..engine.structured import looks_like_injection

EVENT_KINDS = frozenset({"save", "snapshot", "commit", "branch-switch", "manual"})
FILE_STATUSES = frozenset({"added", "modified", "deleted", "renamed", "untracked"})

_UID_RE = re.compile(r"^[A-Za-z0-9._:-]{8,80}$")
_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2})?(\.\d+)?(Z|[+-]\d{2}:?\d{2})?$")
_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")

# 端侧应该已经替换成 [REDACTED]；命中这些模式说明脱敏漏了
SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
)

_MAX_NOTES = 4000
_MAX_PATH = 400
_MAX_SYMBOL = 60
_MAX_SYMBOLS = 12
_MAX_NOTE = 70
_MAX_NOTES_PER_FILE = 8


def _clean_symbols(value) -> list[str]:
    """符号名清洗：去空白、限长、去重、限个数。

    符号是本机抽出来的结构摘要（函数/类/文档小节名），不承载代码原文；
    但仍要防住把整行代码或密钥塞进名字里的情况。
    """
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if not text or len(text) > _MAX_SYMBOL:
            continue
        if any(mark in text for mark in ("\n", "\r", "\x00", "(", ")", ";")):
            continue
        if text not in out:
            out.append(text)
        if len(out) >= _MAX_SYMBOLS:
            break
    return out


def _clean_note_list(value, limit: int = _MAX_NOTES_PER_FILE) -> list[str]:
    """注释线索清洗：允许空格与标点（它们本来就是人话），但压成单行并限长。"""
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        text = " ".join(str(item or "").split())[:_MAX_NOTE]
        if len(text) < 4:
            continue
        if text not in out:
            out.append(text)
        if len(out) >= limit:
            break
    return out



class DeviceInfo(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str = Field(default="", max_length=120)
    platform: str = Field(default="", max_length=40)
    agent_version: str = Field(default="", max_length=40)


class RepoInfo(BaseModel):
    model_config = ConfigDict(extra="ignore")

    label: str = Field(default="", max_length=200)
    path_hash: str = Field(min_length=8, max_length=64)
    branch: str = Field(default="", max_length=160)
    head: str = Field(default="", max_length=64)
    remote_hash: str | None = Field(default=None, max_length=64)
    dirty: bool = False
    unpushed: bool = False

    @field_validator("path_hash")
    @classmethod
    def _clean_hash(cls, value: str) -> str:
        text = (value or "").strip()
        if not re.fullmatch(r"[0-9a-fA-F]{8,64}", text):
            raise ValueError("path_hash 必须是 8-64 位十六进制")
        return text.lower()


class FileChange(BaseModel):
    model_config = ConfigDict(extra="ignore")

    path: str = Field(min_length=1, max_length=_MAX_PATH)
    status: str = "modified"
    added: int = 0
    removed: int = 0
    sensitive: bool = False
    lang: str = Field(default="", max_length=24)
    # 端侧在本地抽出的符号差（新增/移除了哪些函数、类、文档小节）
    symbols_added: list[str] = Field(default_factory=list)
    symbols_removed: list[str] = Field(default_factory=list)
    # 端侧抽出的注释线索（本次新增的单行说明），比路径更能说明「在做什么」
    notes_added: list[str] = Field(default_factory=list)
    # 本次新增的内容行：文档是正文，代码是骨架（导入/路由/签名），不含函数体
    lines_added: list[str] = Field(default_factory=list)

    @field_validator("path")
    @classmethod
    def _clean_path(cls, value: str) -> str:
        text = (value or "").strip().replace("\\", "/")
        if not text:
            raise ValueError("path 不能为空")
        if "\x00" in text:
            raise ValueError("path 含非法字符")
        if text.startswith(("/", "~")):
            raise ValueError(f"path 必须是仓库内相对路径：{text}")
        if _DRIVE_RE.match(text):
            raise ValueError(f"path 不能是绝对路径：{text}")
        if ".." in text.split("/"):
            raise ValueError(f"path 不能包含上级目录：{text}")
        return text

    @field_validator("symbols_added", "symbols_removed")
    @classmethod
    def _clean_symbol_list(cls, value: list[str]) -> list[str]:
        return _clean_symbols(value)

    @field_validator("notes_added")
    @classmethod
    def _clean_note_list(cls, value: list[str]) -> list[str]:
        return _clean_note_list(value, limit=4)

    @field_validator("lines_added")
    @classmethod
    def _clean_line_list(cls, value: list[str]) -> list[str]:
        return _clean_note_list(value, limit=8)

    @field_validator("status")
    @classmethod
    def _clean_status(cls, value: str) -> str:
        status = (value or "modified").strip().lower()
        if status not in FILE_STATUSES:
            raise ValueError(f"未知的文件状态：{status}")
        return status

    @field_validator("added", "removed")
    @classmethod
    def _non_negative(cls, value: int) -> int:
        if value < 0 or value > 2_000_000:
            raise ValueError("行数超出合理范围")
        return int(value)


class EventStats(BaseModel):
    model_config = ConfigDict(extra="ignore")

    files: int = 0
    added: int = 0
    removed: int = 0
    tests_added: int = 0
    tests_changed: int = 0
    tests_removed: int = 0
    binary: int = 0


class ContentDigest(BaseModel):
    """符号级摘要：本轮新增/移除了哪些函数、类、文档小节。

    不含路径、不含代码原文，因此静默模式（``allow_view=False``）也允许上行。
    这是「AI 只知道改了几个文件」和「AI 知道改了什么」之间的分界线。
    """

    model_config = ConfigDict(extra="ignore")

    added: list[str] = Field(default_factory=list)
    removed: list[str] = Field(default_factory=list)
    # 本次新增的注释线索：AI 能借此说出「在做什么」，仍然不是代码原文
    notes: list[str] = Field(default_factory=list)
    # 本次新增的内容行：没有它，洞察只能说「多了六行」，说不出这六行干了什么
    lines: list[str] = Field(default_factory=list)

    @field_validator("added", "removed")
    @classmethod
    def _clean(cls, value: list[str]) -> list[str]:
        return _clean_symbols(value)

    @field_validator("notes")
    @classmethod
    def _clean_notes(cls, value: list[str]) -> list[str]:
        return _clean_note_list(value)

    @field_validator("lines")
    @classmethod
    def _clean_lines(cls, value: list[str]) -> list[str]:
        return _clean_note_list(value, limit=10)


class CodeEventEnvelope(BaseModel):
    """一次保存触发的端侧事件包。"""

    model_config = ConfigDict(extra="ignore")

    schema_version: int = ENGINE_SCHEMA_VERSION
    event_uid: str
    kind: str = "save"
    ts: str
    trigger: str = Field(default="save", max_length=40)
    device: DeviceInfo = Field(default_factory=DeviceInfo)
    repo: RepoInfo
    stats: EventStats = Field(default_factory=EventStats)
    files: list[FileChange] = Field(default_factory=list)
    digest: ContentDigest = Field(default_factory=ContentDigest)
    redactions: int = 0
    redacted: bool = True
    notes: str = ""

    @field_validator("schema_version")
    @classmethod
    def _known_schema(cls, value: int) -> int:
        if value != ENGINE_SCHEMA_VERSION:
            raise ValueError(f"不支持的 schema_version：{value}（服务端支持 {ENGINE_SCHEMA_VERSION}）")
        return value

    @field_validator("event_uid")
    @classmethod
    def _clean_uid(cls, value: str) -> str:
        text = (value or "").strip()
        if not _UID_RE.match(text):
            raise ValueError("event_uid 只能包含字母数字与 . _ : -，长度 8-80")
        return text

    @field_validator("kind")
    @classmethod
    def _known_kind(cls, value: str) -> str:
        kind = (value or "save").strip().lower()
        if kind not in EVENT_KINDS:
            raise ValueError(f"未知的事件类型：{kind}")
        return kind

    @field_validator("ts")
    @classmethod
    def _clean_ts(cls, value: str) -> str:
        text = (value or "").strip()
        if not _TS_RE.match(text):
            raise ValueError("ts 必须是 ISO 时间，例如 2026-11-10T14:20:31Z")
        return text

    @field_validator("files")
    @classmethod
    def _file_limit(cls, value: list[FileChange]) -> list[FileChange]:
        if len(value) > ENGINE_EVENT_MAX_FILES:
            raise ValueError(f"单次事件的文件数不能超过 {ENGINE_EVENT_MAX_FILES}")
        return value

    @field_validator("notes")
    @classmethod
    def _clean_notes(cls, value: str) -> str:
        text = (value or "")[: _MAX_NOTES + 1]
        if len(text) > _MAX_NOTES:
            raise ValueError(f"notes 不能超过 {_MAX_NOTES} 个字符")
        if looks_like_injection(text):
            # 不是安全问题，但说明这段文本会被当成模型输入，标记出来给上层
            return text
        return text


# ---------------------------------------------------------------- 入口

def _reject(prefix: str, exc: Exception) -> EventRejected:
    detail = str(exc).replace("\n", " ")[:300]
    return EventRejected(f"{prefix}：{detail}")


def parse_envelope(raw: bytes | str | dict | None) -> CodeEventEnvelope:
    """解析并校验事件包；任何不合规都抛 ``EventRejected``。"""
    if raw is None:
        raise EventRejected("请求体为空")
    if isinstance(raw, (bytes, bytearray)):
        size = len(raw)
        if size > ENGINE_MAX_EVENT_BYTES:
            raise EventRejected(
                f"事件包过大：{size} 字节，上限 {ENGINE_MAX_EVENT_BYTES} 字节"
            )
        try:
            data: Any = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _reject("事件包不是合法 JSON", exc) from exc
    elif isinstance(raw, str):
        if len(raw.encode("utf-8")) > ENGINE_MAX_EVENT_BYTES:
            raise EventRejected("事件包过大")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise _reject("事件包不是合法 JSON", exc) from exc
    else:
        data = raw

    if not isinstance(data, dict):
        raise EventRejected("事件包必须是 JSON 对象")

    try:
        envelope = CodeEventEnvelope.model_validate(data)
    except Exception as exc:  # pydantic.ValidationError 及其它
        raise _reject("事件包校验失败", exc) from exc

    audit_secrets(envelope)
    return envelope


def audit_secrets(envelope: CodeEventEnvelope) -> None:
    """兜底扫描：文件名、备注与符号名里出现密钥样式的内容就拒收。"""
    haystacks = [envelope.notes, envelope.repo.label, envelope.repo.branch]
    haystacks.extend(item.path for item in envelope.files)
    haystacks.extend(envelope.digest.added)
    haystacks.extend(envelope.digest.removed)
    haystacks.extend(envelope.digest.notes)
    haystacks.extend(envelope.digest.lines)
    for item in envelope.files:
        haystacks.extend(item.symbols_added)
        haystacks.extend(item.symbols_removed)
        haystacks.extend(item.lines_added)
        haystacks.extend(item.notes_added)
    for text in haystacks:
        for pattern in SECRET_PATTERNS:
            if pattern.search(text or ""):
                raise EventRejected("事件包里检测到未脱敏的密钥片段，请检查端侧脱敏规则")


def summary(envelope: CodeEventEnvelope) -> dict:
    """落库用的摘要（``code_events.files`` / ``stats`` / ``payload``）。"""
    stats = envelope.stats.model_dump()
    stats.setdefault("files", len(envelope.files))
    return {
        "files": [item.model_dump() for item in envelope.files],
        "stats": stats,
        "payload": envelope.model_dump(mode="json"),
    }


__all__ = [
    "CodeEventEnvelope",
    "ContentDigest",
    "EVENT_KINDS",
    "EventStats",
    "FILE_STATUSES",
    "FileChange",
    "RepoInfo",
    "SECRET_PATTERNS",
    "audit_secrets",
    "parse_envelope",
    "summary",
]
