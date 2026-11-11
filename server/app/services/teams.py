"""团队与成员的业务逻辑（只对 Session 负责，不关心 HTTP）。"""
from __future__ import annotations

import secrets

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from ..db.base import utcnow_iso
from ..db.models import Issue, Label, Team, TeamMember, User
from ..security import hash_password
from . import hierarchy
from .avatars import url as avatar_url
from .settings import active_team_id, set_setting

DEFAULT_LABELS = [
    ("bug", "#f85149"),
    ("feature", "#2f81f7"),
    ("design", "#3fb950"),
    ("infra", "#58a6ff"),
    ("docs", "#a371f7"),
    ("good first issue", "#d29922"),
]

ADMIN_ROLES = {"owner", "admin"}
EDITABLE_ROLES = {"owner", "admin", "member"}

# 团队被删掉之后，SQLite 会把它的编号重新分配给下一个新建团队（id 是 max+1，不是自增序列）。
# 只要旧团队的子表数据没清干净，新团队写默认标签时就会撞 (team_id, name) 唯一约束，
# 表现为「新建团队 → 服务器内部错误」。建团队前先按新编号清一遍残留，代价是一次索引删除。
_TEAM_SCOPED_TABLES = (
    "labels", "team_members", "milestones", "schedule_entries", "activity",
    "ai_sessions", "devices", "repo_bindings", "code_events", "client_logs",
    "inference_jobs", "inference_audit", "team_engine", "integration_credentials",
    "issues", "insights", "pair_codes",
)


def _clear_reused_team_id(session: Session, team_id: int) -> None:
    """清掉「上一个占用这个编号的团队」留下的孤儿行（只可能有孤儿，新团队还没有任何数据）。"""
    session.execute(
        text("delete from issue_labels where issue_id in "
             "(select id from issues where team_id = :t)"), {"t": team_id})
    session.execute(
        text("delete from comments where issue_id in "
             "(select id from issues where team_id = :t)"), {"t": team_id})
    session.execute(
        text("delete from insight_events where insight_id in "
             "(select id from insights where team_id = :t)"), {"t": team_id})
    for table in _TEAM_SCOPED_TABLES:
        session.execute(text(f"delete from {table} where team_id = :t"), {"t": team_id})


# ------------------------------------------------------------------ 查询

def all_teams(session: Session) -> list[Team]:
    return list(session.scalars(select(Team).order_by(Team.id)))


def find_by_slug(session: Session, slug: str) -> Team | None:
    return session.scalar(select(Team).where(Team.slug == slug))


def resolve(session: Session, key: str) -> Team | None:
    """路径里的 ``{team}``：优先按 slug，其次兼容数字 id。"""
    if not key:
        return None
    team = find_by_slug(session, key)
    if team is not None:
        return team
    if key.isdigit():
        return session.get(Team, int(key))
    return None


def memberships(session: Session, user_id: int | None) -> dict[int, str]:
    if not user_id:
        return {}
    stmt = select(TeamMember.team_id, TeamMember.role).where(TeamMember.user_id == user_id)
    return {team_id: role for team_id, role in session.execute(stmt)}


def role_of(session: Session, team_id: int, user_id: int | None) -> str | None:
    if not user_id:
        return None
    stmt = select(TeamMember.role).where(
        TeamMember.team_id == team_id, TeamMember.user_id == user_id
    )
    return session.scalar(stmt)


def display_role(role: str | None, require_login: bool) -> str | None:
    """单团队模式（无需登录）下，匿名访问者对外显示为团队所有者。

    与 ``TeamAccess.effective_role()`` 保持一致，否则前端会认为没有编辑权限，
    从而隐藏设置页、提交按钮等入口。
    """
    if role:
        return role
    return None if require_login else "owner"


def parse_skills(raw) -> list[dict]:
    """``team_members.skills`` 的 JSON 解析：容忍脏数据，永远返回列表。"""
    import json

    if isinstance(raw, list):
        items = raw
    else:
        try:
            items = json.loads(raw or "[]")
        except (TypeError, ValueError):
            return []
    if not isinstance(items, list):
        return []
    out: list[dict] = []
    for item in items:
        if isinstance(item, str):
            name = item.strip()
            if name:
                out.append({"name": name[:40], "level": 2, "source": "self"})
        elif isinstance(item, dict):
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            try:
                level = int(item.get("level") or 2)
            except (TypeError, ValueError):
                level = 2
            out.append({
                "name": name[:40],
                "level": max(1, min(3, level)),
                "source": str(item.get("source") or "self")[:16],
            })
    return out


def dump_skills(values) -> str:
    import json

    return json.dumps(parse_skills(values), ensure_ascii=False)


def member_rows(session: Session, team_id: int) -> list[dict]:
    stmt = (
        select(TeamMember, User)
        .join(User, User.id == TeamMember.user_id)
        .where(TeamMember.team_id == team_id)
        .order_by(
            func.coalesce(
                func.nullif(TeamMember.role, "member"), "zzz"
            ),
            TeamMember.joined_at,
        )
    )
    return [
        {
            "id": user.id,
            "username": user.username,
            "display_name": user.display_name,
            "title": user.title or "",
            "role": member.role,
            "joined_at": member.joined_at,
            "skills": parse_skills(member.skills),
            # 卡片成员（新建时没有密码）不能登录，前端据此显示"未开通登录"
            "can_login": bool((user.password_hash or "").strip()),
            "avatar_url": avatar_url(user),
        }
        for member, user in session.execute(stmt)
    ]


def user_is_member(session: Session, team_id: int, user_id: int | None) -> bool:
    return role_of(session, team_id, user_id) is not None


def is_admin_everywhere(session: Session, user_id: int) -> bool:
    stmt = select(func.count()).select_from(TeamMember).where(
        TeamMember.user_id == user_id, TeamMember.role == "owner"
    )
    return bool(session.scalar(stmt))


def default_team(session: Session) -> Team | None:
    from .settings import default_team_id

    team_id = default_team_id(session)
    if team_id:
        team = session.get(Team, team_id)
        if team is not None:
            return team
    return session.scalar(select(Team).order_by(Team.id))


def active_team(session: Session, user_id: int | None) -> Team | None:
    team_id = active_team_id(session, user_id)
    if team_id:
        team = session.get(Team, team_id)
        if team is not None:
            return team
    return default_team(session)


def counts(session: Session, team_id: int) -> dict:
    """团队卡片的计数：只算叶子议题（父子不双计）。"""
    leaf = hierarchy.leaf_condition()
    total = session.scalar(
        select(func.count()).select_from(Issue).where(Issue.team_id == team_id, leaf)
    ) or 0
    open_count = session.scalar(
        select(func.count()).select_from(Issue).where(
            Issue.team_id == team_id, Issue.state == "open", leaf
        )
    ) or 0
    return {"total": total, "open": open_count}


# ------------------------------------------------------------------ 写入

def slugify(base: str) -> str:
    """生成 URL 友好的 slug：只保留 ASCII 字母数字与 ``-_``（GitHub 仓库名的规则）。"""
    chars = []
    for ch in (base or "").lower():
        if ch in "-_" or ch.isdigit() or ("a" <= ch <= "z"):
            chars.append(ch)
        else:
            chars.append("-")
    slug = "".join(chars)
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug.strip("-_")[:60] or "team"


def unique_slug(session: Session, base: str) -> str:
    slug = slugify(base)
    candidate, n = slug, 2
    while find_by_slug(session, candidate) is not None:
        candidate = f"{slug}-{n}"
        n += 1
    return candidate


def clean_slug(value: str) -> str:
    """校验并规范化用户手填的 slug；不合法时抛 ValueError（中文提示）。"""
    slug = (value or "").strip().lower()
    if not slug:
        raise ValueError("请填写团队地址")
    if len(slug) > 60:
        raise ValueError("团队地址不能超过 60 个字符")
    if not all(ch.isdigit() or ("a" <= ch <= "z") or ch in "-_" for ch in slug):
        raise ValueError("团队地址只能包含小写字母、数字、- 和 _")
    if slug.strip("-_") == "":
        raise ValueError("团队地址不能只有 - 或 _")
    if slug.isdigit():
        raise ValueError("团队地址不能是纯数字（会和旧版数字 ID 混淆）")
    return slug


def create_team(session: Session, name: str, slug: str | None = None,
                description: str = "", owner_id: int | None = None) -> Team:
    team = Team(
        name=name,
        slug=slug or unique_slug(session, name),
        description=description or "",
        created_at=utcnow_iso(),
    )
    session.add(team)
    session.flush()
    _clear_reused_team_id(session, team.id)
    for label_name, color in DEFAULT_LABELS:
        session.add(Label(team_id=team.id, name=label_name, color=color))
    if owner_id:
        session.add(TeamMember(
            team_id=team.id, user_id=owner_id, role="owner", joined_at=utcnow_iso()
        ))
        set_setting(session, f"active_team_{owner_id}", team.id)
    session.flush()
    return team


def add_member(session: Session, team: Team, user: User, role: str = "member") -> TeamMember:
    member = TeamMember(
        team_id=team.id,
        user_id=user.id,
        role="admin" if role == "admin" else "member",
        joined_at=utcnow_iso(),
    )
    session.add(member)
    session.flush()
    return member


def set_role(session: Session, team: Team, user: User, role: str) -> None:
    member = session.get(TeamMember, (team.id, user.id))
    if member is None:
        raise KeyError("member")
    member.role = role
    session.flush()


def remove_member(session: Session, team: Team, user: User) -> None:
    member = session.get(TeamMember, (team.id, user.id))
    if member is not None:
        session.delete(member)
        session.flush()


def unique_member_username(session: Session) -> str:
    while True:
        candidate = "member-" + secrets.token_hex(3)
        if session.scalar(select(User.id).where(User.username == candidate)) is None:
            return candidate


def find_user(session: Session, username: str) -> User | None:
    return session.scalar(select(User).where(User.username == username.lower()))


def create_member_user(session: Session, display_name: str, username: str | None,
                       title: str = "团队成员") -> User:
    user = User(
        username=username or unique_member_username(session),
        display_name=display_name,
        title=title,
        email="",
        password_hash="",
        created_at=utcnow_iso(),
    )
    session.add(user)
    session.flush()
    return user


def has_same_display_name(session: Session, team_id: int, display_name: str) -> bool:
    stmt = (
        select(User.id)
        .join(TeamMember, TeamMember.user_id == User.id)
        .where(TeamMember.team_id == team_id, User.display_name == display_name)
    )
    return session.scalar(stmt) is not None


# ------------------------------------------------------------------ 计数自增

def next_issue_number(session: Session, team: Team) -> int:
    team.issue_seq = (team.issue_seq or 0) + 1
    session.flush()
    return team.issue_seq


def next_milestone_number(session: Session, team_id: int) -> int:
    from ..db.models import Milestone

    current = session.scalar(
        select(func.max(Milestone.number)).where(Milestone.team_id == team_id)
    )
    return int(current or 0) + 1


def ensure_password_hash_placeholder(user: User) -> None:  # pragma: no cover - 文档用
    if not user.password_hash:
        user.password_hash = hash_password(secrets.token_hex(8))
