"""推理引擎的数据表（P0 地基，9 张表）。

与 ``models.py``（协作工作台的核心表）分文件，是为了让「引擎」这一层
可以整体开关：``engine_models`` 只在 ``sync_schema`` 里被导入一次，表结构与
协作表之间只有 ``team_id`` / ``issue_id`` 之类的外键联系。

约定与核心表一致：
* 时间统一 TEXT、UTC、``YYYY-MM-DDTHH:MM:SSZ``（``utcnow_iso``）；
* 布尔用 INTEGER 0/1；
* 结构化数据（文件列表、证据、建议、审计载荷）用 TEXT 存 JSON 文本，
  跨 SQLite / PostgreSQL / MySQL 都不需要方言特性；
* 所有表都带 ``team_id``，多租户查询永远先按团队过滤。
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, utcnow_iso


class Device(Base):
    """端侧探针设备（一台机器 = 一行）。

    配对流程：管理员生成一次性配对码 → 探针调用 ``POST /api/edge/devices/claim``
    用配对码换回 ``token_hash``（明文 token 只出现一次）。撤销即写 ``revoked_at``，
    之后该设备的 WS 连接被立即关闭、REST 事件上报 401。
    """

    __tablename__ = "devices"
    __table_args__ = (
        Index("idx_devices_team", "team_id", "id"),
        Index("uq_devices_token", "token_hash", unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    team_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("teams.id", ondelete="CASCADE"), nullable=False
    )
    # 单用户多设备：user_id 不做唯一约束
    user_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    platform: Mapped[str] = mapped_column(String(40), nullable=True, default="")
    agent_version: Mapped[str] = mapped_column(String(40), nullable=True, default="")
    # 设备 token 的 sha256（明文不落库）
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    meta: Mapped[str] = mapped_column(Text, nullable=True, default="{}")
    paired_at: Mapped[str] = mapped_column(String(32), nullable=False, default=utcnow_iso)
    last_seen_at: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    revoked_at: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    created_at: Mapped[str] = mapped_column(String(32), nullable=False, default=utcnow_iso)


class RepoBinding(Base):
    """设备上被监听的仓库（本地绝对路径只存哈希，不存明文）。"""

    __tablename__ = "repo_bindings"
    __table_args__ = (
        Index("uq_repo_device_path", "device_id", "path_hash", unique=True),
        Index("idx_repo_team", "team_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    team_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("teams.id", ondelete="CASCADE"), nullable=False
    )
    device_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("devices.id", ondelete="CASCADE"), nullable=False
    )
    path_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    label: Mapped[str] = mapped_column(String(200), nullable=True, default="")
    branch: Mapped[str] = mapped_column(String(160), nullable=True, default="")
    remote_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[str] = mapped_column(String(32), nullable=False, default=utcnow_iso)


class CodeEvent(Base):
    """端侧上报的脱敏事件包（一条 = 一次保存触发的增量）。

    ``event_uid`` 由端侧生成（设备内唯一），配合 ``(device_id, event_uid)``
    唯一索引做幂等：断网重放不会产生重复洞察。
    """

    __tablename__ = "code_events"
    __table_args__ = (
        Index("uq_events_device_uid", "device_id", "event_uid", unique=True),
        Index("idx_events_team_id", "team_id", "id"),
        Index("idx_events_team_ts", "team_id", "ts"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    team_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("teams.id", ondelete="CASCADE"), nullable=False
    )
    device_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("devices.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    event_uid: Mapped[str] = mapped_column(String(80), nullable=False)
    kind: Mapped[str] = mapped_column(String(40), nullable=False, default="save")
    ts: Mapped[str] = mapped_column(String(32), nullable=False, default=utcnow_iso)
    branch: Mapped[str] = mapped_column(String(160), nullable=True, default="")
    head_commit: Mapped[str] = mapped_column(String(64), nullable=True, default="")
    remote_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # 变更文件摘要：[{path, status, added, removed, sensitive}]
    files: Mapped[str] = mapped_column(Text, nullable=True, default="[]")
    stats: Mapped[str] = mapped_column(Text, nullable=True, default="{}")
    # 脱敏后的完整事件包（原始 diff 不入库）
    payload: Mapped[str] = mapped_column(Text, nullable=True, default="{}")
    redacted: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[str] = mapped_column(String(32), nullable=False, default=utcnow_iso)


class InferenceJob(Base):
    """一次四段管线的执行记录（状态机：queued → running → done/failed）。"""

    __tablename__ = "inference_jobs"
    __table_args__ = (
        Index("idx_jobs_team_status", "team_id", "status"),
        Index("idx_jobs_status_id", "status", "id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    team_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("teams.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued")
    coalesce_key: Mapped[str] = mapped_column(String(160), nullable=True, default="")
    payload: Mapped[str] = mapped_column(Text, nullable=True, default="{}")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str] = mapped_column(Text, nullable=True, default="")
    # orchify run_id（形如 `team-3.analyst#ab12cd34`），用于回放事件
    run_id: Mapped[str] = mapped_column(String(120), nullable=True, default="")
    budget: Mapped[str] = mapped_column(Text, nullable=True, default="{}")
    created_at: Mapped[str] = mapped_column(String(32), nullable=False, default=utcnow_iso)
    started_at: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    finished_at: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)


class Insight(Base):
    """一条推断洞察（draft → pending → confirmed/rejected/expired/reverted）。

    只有 ``confirmed`` 才允许写回 Issue / 周计划，写回结果记在 ``applied``。
    """

    __tablename__ = "insights"
    __table_args__ = (
        Index("idx_insights_team_state", "team_id", "state", "id"),
        Index("idx_insights_dedupe", "team_id", "dedupe_key"),
        Index("idx_insights_issue", "issue_id", "id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    team_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("teams.id", ondelete="CASCADE"), nullable=False
    )
    device_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("devices.id", ondelete="SET NULL"), nullable=True
    )
    job_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("inference_jobs.id", ondelete="SET NULL"), nullable=True
    )
    issue_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("issues.id", ondelete="SET NULL"), nullable=True
    )
    milestone_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("milestones.id", ondelete="SET NULL"), nullable=True
    )
    kind: Mapped[str] = mapped_column(String(40), nullable=False, default="progress")
    title: Mapped[str] = mapped_column(String(240), nullable=False, default="")
    summary: Mapped[str] = mapped_column(Text, nullable=True, default="")
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")
    # 证据引用：[{type, ref, note}]
    evidence: Mapped[str] = mapped_column(Text, nullable=True, default="[]")
    # Coordinator 的建议：{status, plan_week, comment, ...}
    suggestion: Mapped[str] = mapped_column(Text, nullable=True, default="{}")
    applied: Mapped[str] = mapped_column(Text, nullable=True, default="{}")
    model: Mapped[str] = mapped_column(String(120), nullable=True, default="")
    dedupe_key: Mapped[str] = mapped_column(String(160), nullable=True, default="")
    created_at: Mapped[str] = mapped_column(String(32), nullable=False, default=utcnow_iso)
    decided_at: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    # 决策人：显示名 + 可选账号外键。匿名模式（未开登录）下 decided_by 记操作身份，
    # user 外键为空——审计链里另有更完整的 actor 信息。
    decided_by: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    decided_by_user_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    expires_at: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)


class InsightEvent(Base):
    """洞察的时间线（创建、推进、确认、写回、撤销……），前端时间线直接用。"""

    __tablename__ = "insight_events"
    __table_args__ = (Index("uq_insight_events_seq", "insight_id", "seq", unique=True),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    insight_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("insights.id", ondelete="CASCADE"), nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    type: Mapped[str] = mapped_column(String(40), nullable=False, default="created")
    detail: Mapped[str] = mapped_column(Text, nullable=True, default="{}")
    created_at: Mapped[str] = mapped_column(String(32), nullable=False, default=utcnow_iso)


class TeamEngine(Base):
    """团队级引擎配置（一行一个团队）。``api_key_enc`` 存加密后的密钥。"""

    __tablename__ = "team_engine"

    team_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("teams.id", ondelete="CASCADE"), primary_key=True
    )
    enabled: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    base_url: Mapped[str] = mapped_column(String(300), nullable=True, default="")
    model: Mapped[str] = mapped_column(String(120), nullable=True, default="")
    api_key_enc: Mapped[str] = mapped_column(Text, nullable=True, default="")
    embedding_model: Mapped[str] = mapped_column(String(120), nullable=True, default="hashing")
    confidence_threshold: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.6
    )
    approval_timeout: Mapped[int] = mapped_column(Integer, nullable=False, default=3600)
    budget_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.05)
    budget_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=200_000)
    budget_seconds: Mapped[float] = mapped_column(Float, nullable=False, default=120.0)
    dry_run: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    auto_writeback: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[str] = mapped_column(String(32), nullable=False, default=utcnow_iso)


class IntegrationCredential(Base):
    """外部系统凭据（GitHub/Jira/MCP），P3 才真正用；密钥同样加密存。"""

    __tablename__ = "integration_credentials"
    __table_args__ = (
        Index("uq_integration_team_provider", "team_id", "provider", unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    team_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("teams.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    secret_enc: Mapped[str] = mapped_column(Text, nullable=True, default="")
    config: Mapped[str] = mapped_column(Text, nullable=True, default="{}")
    created_at: Mapped[str] = mapped_column(String(32), nullable=False, default=utcnow_iso)
    updated_at: Mapped[str] = mapped_column(String(32), nullable=False, default=utcnow_iso)


class InferenceAudit(Base):
    """审计哈希链：``hash = sha256(prev_hash + canonical(payload))``，按团队首尾相接。"""

    __tablename__ = "inference_audit"
    __table_args__ = (
        Index("idx_audit_team_id", "team_id", "id"),
        Index("idx_audit_hash", "hash"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    team_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("teams.id", ondelete="CASCADE"), nullable=False
    )
    actor_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    actor_name: Mapped[str] = mapped_column(String(80), nullable=True, default="")
    action: Mapped[str] = mapped_column(String(60), nullable=False)
    subject_type: Mapped[str] = mapped_column(String(40), nullable=True, default="")
    subject_id: Mapped[str] = mapped_column(String(80), nullable=True, default="")
    payload: Mapped[str] = mapped_column(Text, nullable=True, default="{}")
    prev_hash: Mapped[str] = mapped_column(String(64), nullable=True, default="")
    hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[str] = mapped_column(String(32), nullable=False, default=utcnow_iso)


__all__ = [
    "CodeEvent",
    "Device",
    "InferenceAudit",
    "InferenceJob",
    "Insight",
    "InsightEvent",
    "IntegrationCredential",
    "RepoBinding",
    "TeamEngine",
]
