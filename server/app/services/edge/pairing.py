"""配对码：让「不是网页同一账号」的机器接入团队。

两种接入方式，对应两种信任来源：

* **同账号**：端侧用网页同一账号登录，直接为该账号建设备令牌，不需要配对码；
* **不同身份**：管理员在网页生成一次性短码，端侧用短码兑换令牌，
  兑换时按「电脑用户名」自动建一个团队成员（角色 member），
  这样工时、日志、洞察都挂在这个人名下，团队页里能看到他。

配对码只存 sha256，明文只在生成响应里出现一次；有效期默认 30 分钟，用过即作废。
"""
from __future__ import annotations

import hashlib
import re
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...db.base import utcnow_iso
from ...db.models import PairCode, Team, TeamMember, User
from ...errors import ApiError
from .devices import upsert_device

# 去掉容易看错的 0 O 1 I 2 Z 5 S 8 B
ALPHABET = "ACDEFGHJKLMNPQRTUVWXY34679"
CODE_LEN = 8
PAIR_CODE_TTL_MINUTES = 30
MAX_USERNAME = 32


def _normalize(code: str) -> str:
    return re.sub(r"[^0-9A-Z]", "", (code or "").upper())


def hash_code(code: str) -> str:
    return hashlib.sha256(_normalize(code).encode("utf-8")).hexdigest()


def sanitize_username(raw: str) -> str:
    """把电脑用户名（含空格、中文、点号）收敛成合法用户名。"""
    text = re.sub(r"[^0-9A-Za-z_-]", "", (raw or "").strip())
    return text[:MAX_USERNAME].lower()


def create_code(session: Session, *, team_id: int, created_by: int | None = None,
                ttl_minutes: int = PAIR_CODE_TTL_MINUTES) -> tuple[str, PairCode]:
    raw = "".join(secrets.choice(ALPHABET) for _ in range(CODE_LEN))
    code = f"{raw[:4]}-{raw[4:]}"
    now = datetime.now(timezone.utc)
    row = PairCode(
        team_id=team_id,
        code_hash=hash_code(code),
        created_by=created_by,
        created_at=utcnow_iso(),
        expires_at=(now + timedelta(minutes=ttl_minutes)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        used_at="",
    )
    session.add(row)
    session.flush()
    return code, row


def _expired(row: PairCode) -> bool:
    if not row.expires_at:
        return False
    return row.expires_at <= utcnow_iso()


def claim(
    session: Session,
    *,
    code: str,
    username: str = "",
    device_name: str = "",
    platform: str = "",
) -> dict:
    """用配对码换设备令牌；顺带按电脑用户名建成员。"""
    text = _normalize(code)
    if not text:
        raise ApiError("请填写配对码", 400, code="pair_code_required")
    row = session.execute(
        select(PairCode).where(PairCode.code_hash == hash_code(text))
    ).scalars().first()
    if row is None or row.used_at:
        raise ApiError("配对码无效或已被使用，请让管理员重新生成", 401, code="bad_pair_code")
    if _expired(row):
        raise ApiError("配对码已过期，请让管理员重新生成", 401, code="pair_code_expired")

    team = session.get(Team, row.team_id)
    if team is None:
        raise ApiError("团队不存在", 404, code="team_missing")

    wanted = sanitize_username(username)
    created_user = False
    if wanted:
        user = session.execute(
            select(User).where(User.username == wanted)
        ).scalars().first()
        if user is None:
            user = User(
                username=wanted,
                display_name=(username or wanted)[:80],
                title="端侧接入",
                password_hash="",       # 没有网页口令，只能由管理员后续设置
            )
            session.add(user)
            session.flush()
            created_user = True
    else:
        wanted = f"device-{secrets.token_hex(3)}"
        user = User(username=wanted, display_name=wanted, title="端侧接入", password_hash="")
        session.add(user)
        session.flush()
        created_user = True

    member = session.get(TeamMember, (team.id, user.id))
    if member is None:
        session.add(TeamMember(team_id=team.id, user_id=user.id, role="member"))
        session.flush()

    device, token = upsert_device(
        session,
        team_id=team.id,
        name=device_name or wanted,
        platform=platform,
        user_id=user.id,
    )
    row.used_at = utcnow_iso()
    row.used_by_user = user.id
    session.flush()

    return {
        "device": device,
        "token": token,
        "team": team,
        "user": user,
        "user_created": created_user,
    }


__all__ = [
    "PAIR_CODE_TTL_MINUTES",
    "claim",
    "create_code",
    "hash_code",
    "sanitize_username",
]
