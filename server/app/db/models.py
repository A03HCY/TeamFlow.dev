"""全部 14 张表的 ORM 模型（列名、约束与线上 ``data.db`` 完全一致）。"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import (
    Column,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, utcnow_iso


# ---------------------------------------------------------------- 关联表
# 无附加字段的多对多关系用普通 Table 声明，ORM 侧通过 relationship 赋值维护。

issue_labels = Table(
    "issue_labels",
    Base.metadata,
    Column("issue_id", Integer, ForeignKey("issues.id", ondelete="CASCADE"), primary_key=True),
    Column("label_id", Integer, ForeignKey("labels.id", ondelete="CASCADE"), primary_key=True),
)

schedule_participants = Table(
    "schedule_participants",
    Base.metadata,
    Column("schedule_id", Integer, ForeignKey("schedule_entries.id", ondelete="CASCADE"), primary_key=True),
    Column("user_id", Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
)


# ---------------------------------------------------------------- 用户

class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(80), nullable=False)
    title: Mapped[str] = mapped_column(String(120), nullable=True, default="")
    email: Mapped[str] = mapped_column(String(160), nullable=True, default="")
    # 头像在存储后端里的 key（如 avatars/12/ab12cd34.png），空串表示用姓名首字
    avatar: Mapped[str] = mapped_column(String(300), nullable=True, default="")
    # 卡片成员（只有姓名、没注册）的 password_hash 为空字符串
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    created_at: Mapped[str] = mapped_column(String(32), nullable=False, default=utcnow_iso)


# ---------------------------------------------------------------- 团队

class Team(Base):
    __tablename__ = "teams"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    slug: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=True, default="")
    issues_public: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    milestones_public: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    roadmap_public: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    schedules_public: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    issue_seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[str] = mapped_column(String(32), nullable=False, default=utcnow_iso)


class TeamMember(Base):
    __tablename__ = "team_members"

    team_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("teams.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="member")
    joined_at: Mapped[str] = mapped_column(String(32), nullable=False, default=utcnow_iso)
    # 新增列：技能标签 JSON，形如 [{"name": "fastapi", "level": 3, "source": "self"}]
    skills: Mapped[str] = mapped_column(Text, nullable=False, default="[]")


class SessionRow(Base):
    __tablename__ = "sessions"

    token: Mapped[str] = mapped_column(String(80), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[str] = mapped_column(String(32), nullable=False, default=utcnow_iso)
    expires_at: Mapped[str] = mapped_column(String(32), nullable=False)


class EditSession(Base):
    __tablename__ = "edit_sessions"

    token: Mapped[str] = mapped_column(String(80), primary_key=True)
    created_at: Mapped[str] = mapped_column(String(32), nullable=False, default=utcnow_iso)
    expires_at: Mapped[str] = mapped_column(String(32), nullable=False)


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False, default="")


# ---------------------------------------------------------------- 里程碑

class Milestone(Base):
    __tablename__ = "milestones"
    __table_args__ = (
        Index("uq_ms_number", "team_id", "number", unique=True),
        Index("idx_ms_team", "team_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    team_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("teams.id", ondelete="CASCADE"), nullable=False
    )
    # 新增列：GitHub 风格 URL /api/teams/{slug}/milestones/{number}
    number: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=True, default="")
    # 新增列：验收标准（Markdown 勾选清单），推断阶段判断「是否达标」的依据
    acceptance: Mapped[str] = mapped_column(Text, nullable=False, default="")
    state: Mapped[str] = mapped_column(String(20), nullable=True, default="open")
    # 新增列：scheduled（按日期排期，默认）| continuous（长期持续，没有终点）
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="scheduled")
    start_date: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    due_date: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[str] = mapped_column(String(32), nullable=False, default=utcnow_iso)
    closed_at: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)


# ---------------------------------------------------------------- 任务

class Issue(Base):
    __tablename__ = "issues"
    __table_args__ = (
        Index("uq_issues_number", "team_id", "number", unique=True),
        Index("idx_issues_team", "team_id", "state"),
        Index("idx_issues_ms", "milestone_id"),
        Index("idx_issues_week", "team_id", "plan_week"),
        Index("idx_issues_parent", "parent_id", "sort_order"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    team_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("teams.id", ondelete="CASCADE"), nullable=False
    )
    number: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    title: Mapped[str] = mapped_column(String(240), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=True, default="")
    state: Mapped[str] = mapped_column(String(20), nullable=True, default="open")
    status: Mapped[str] = mapped_column(String(20), nullable=True, default="todo")
    priority: Mapped[str] = mapped_column(String(20), nullable=True, default="medium")
    milestone_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("milestones.id", ondelete="SET NULL"), nullable=True
    )
    assignee_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    author_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    due_date: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    # 新增列：周计划（ISO 周，形如 ``2026-W45``）；与「截止日期」是两个维度
    plan_week: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    # 新增列：子议题指向父议题（NULL = 顶层）；深度上限 3，环检测在服务层做
    parent_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("issues.id", ondelete="SET NULL"), nullable=True
    )
    # 新增列：同级/子级排序
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 新增列：预估工时（小时），用于「超预估 → 建议拆分」与负载建议
    estimate_hours: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    created_at: Mapped[str] = mapped_column(String(32), nullable=False, default=utcnow_iso)
    updated_at: Mapped[str] = mapped_column(String(32), nullable=False, default=utcnow_iso)
    closed_at: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)

    labels: Mapped[list["Label"]] = relationship(
        secondary=issue_labels,
        lazy="selectin",
        order_by="Label.name",
    )
    milestone: Mapped[Optional["Milestone"]] = relationship(lazy="joined", viewonly=True)
    assignee: Mapped[Optional["User"]] = relationship(
        foreign_keys=[assignee_id], lazy="joined", viewonly=True
    )
    author: Mapped[Optional["User"]] = relationship(
        foreign_keys=[author_id], lazy="joined", viewonly=True
    )


# ---------------------------------------------------------------- 日程

class ScheduleEntry(Base):
    __tablename__ = "schedule_entries"
    __table_args__ = (
        Index("idx_sched_team", "team_id"),
        Index("idx_sched_user", "user_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    team_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("teams.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    note: Mapped[str] = mapped_column(Text, nullable=True, default="")
    kind: Mapped[str] = mapped_column(String(20), nullable=True, default="once")
    weekday: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    date: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    start_time: Mapped[Optional[str]] = mapped_column(String(5), nullable=True)
    end_time: Mapped[Optional[str]] = mapped_column(String(5), nullable=True)
    start_date: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    end_date: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    color: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    created_at: Mapped[str] = mapped_column(String(32), nullable=False, default=utcnow_iso)
    updated_at: Mapped[str] = mapped_column(String(32), nullable=False, default=utcnow_iso)

    owner: Mapped["User"] = relationship(lazy="joined", viewonly=True)
    participants: Mapped[list["User"]] = relationship(
        secondary=schedule_participants,
        lazy="selectin",
        order_by="User.display_name",
    )


# ---------------------------------------------------------------- 标签

class Label(Base):
    __tablename__ = "labels"
    __table_args__ = (Index("uq_labels_name", "team_id", "name", unique=True),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    team_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("teams.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(60), nullable=False)
    color: Mapped[str] = mapped_column(String(20), nullable=False, default="#6b7280")


# ---------------------------------------------------------------- 评论 / 动态

class Comment(Base):
    __tablename__ = "comments"
    __table_args__ = (Index("idx_comments_iss", "issue_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    issue_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("issues.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    author_name: Mapped[str] = mapped_column(String(80), nullable=True, default="")
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(String(32), nullable=False, default=utcnow_iso)

    user: Mapped[Optional["User"]] = relationship(lazy="joined", viewonly=True)


class Activity(Base):
    __tablename__ = "activity"
    __table_args__ = (Index("idx_activity_team", "team_id", "id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    team_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("teams.id", ondelete="CASCADE"), nullable=False
    )
    actor_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    actor_name: Mapped[str] = mapped_column(String(80), nullable=True, default="")
    verb: Mapped[str] = mapped_column(String(40), nullable=False)
    subject_type: Mapped[str] = mapped_column(String(30), nullable=False)
    subject_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    subject_text: Mapped[str] = mapped_column(String(240), nullable=True, default="")
    detail: Mapped[str] = mapped_column(Text, nullable=True, default="")
    created_at: Mapped[str] = mapped_column(String(32), nullable=False, default=utcnow_iso)


class AiSession(Base):
    """AI 工作台的一次对话（跨设备可恢复，标题取自首条提问）。"""

    __tablename__ = "ai_sessions"
    __table_args__ = (Index("idx_ai_sessions_team", "team_id", "id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    team_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("teams.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    title: Mapped[str] = mapped_column(String(120), nullable=True, default="")
    mode: Mapped[str] = mapped_column(String(20), nullable=True, default="ask")
    created_at: Mapped[str] = mapped_column(String(32), nullable=False, default=utcnow_iso)
    updated_at: Mapped[str] = mapped_column(String(32), nullable=False, default=utcnow_iso)


class AiMessage(Base):
    __tablename__ = "ai_messages"
    __table_args__ = (Index("idx_ai_messages_session", "session_id", "id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("ai_sessions.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="user")
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # 这条提问引用了哪些议题（编号数组的 JSON），便于回看「当时看了什么」
    context: Mapped[str] = mapped_column(Text, nullable=True, default="[]")
    created_at: Mapped[str] = mapped_column(String(32), nullable=False, default=utcnow_iso)


__all__ = [
    "Activity",
    "AiMessage",
    "AiSession",
    "Base",
    "Comment",
    "EditSession",
    "Issue",
    "Label",
    "Milestone",
    "ScheduleEntry",
    "SessionRow",
    "Setting",
    "Team",
    "TeamMember",
    "User",
    "issue_labels",
    "schedule_participants",
]
