"""进度推断的业务侧（不含 orchify 细节）。

P0 只落了地基两件：端侧事件包的校验（``events``）与审计哈希链（``audit``）。
管线（P1 起）会在此包内补充 ``pipeline``/``store``/``insights`` 等模块。
"""
from __future__ import annotations

from .audit import (
    ACTIONS,
    GENESIS_HASH,
    append as append_audit,
    canonical,
    compute_hash,
    decode as decode_audit,
    verify as verify_audit,
)
from .events import (
    EVENT_KINDS,
    FILE_STATUSES,
    CodeEventEnvelope,
    EventStats,
    FileChange,
    RepoInfo,
    audit_secrets,
    parse_envelope,
    summary as event_summary,
)

__all__ = [
    "ACTIONS",
    "EVENT_KINDS",
    "FILE_STATUSES",
    "GENESIS_HASH",
    "CodeEventEnvelope",
    "EventStats",
    "FileChange",
    "RepoInfo",
    "append_audit",
    "audit_secrets",
    "canonical",
    "compute_hash",
    "decode_audit",
    "event_summary",
    "parse_envelope",
    "verify_audit",
]
