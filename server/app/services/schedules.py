"""日程（schedule）校验、归属与参与者。"""
from __future__ import annotations

import datetime as _dt
import re
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import ADMIN_ROLES, EDITABLE_ROLES
from ..db.base import utcnow_iso
from ..db.models import ScheduleEntry, TeamMember, User
from ..errors import ApiError
from ..security import new_token
from .teams import unique_member_username

WEEKDAY_NAMES = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
COLOR_RE = re.compile(r"#[0-9a-fA-F]{6}")
ICS_BYDAY = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]


def parse_date(value) -> str | None:
    """``YYYY-MM-DD``；空值与非法值都返回 None（由调用方决定是否报错）。"""
    if value in (None, "", "null"):
        return None
    text = str(value).strip()[:10]
    try:
        _dt.date.fromisoformat(text)
    except ValueError:
        return None
    return text


def parse_time(value) -> str | None:
    if value in (None, "", "null"):
        return None
    text = str(value).strip()[:5]
    if not TIME_RE.match(text):
        raise ApiError(f"时间格式应为 HH:MM：{value}")
    return text


def normalize_color(value) -> str | None:
    if value in (None, "", "null"):
        return None
    text = str(value).strip()
    if not COLOR_RE.fullmatch(text):
        raise ApiError("颜色格式应为 #RRGGBB")
    return text.lower()


def normalize_schedule(base: dict, body: dict, *, require_day: bool = True) -> dict:
    """合并 base 与 body（body 优先）后校验并归一化日程字段。"""
    data = {
        key: (body[key] if key in body else base.get(key))
        for key in ("title", "note", "kind", "weekday", "date",
                    "start_time", "end_time", "start_date", "end_date")
    }

    title = str(data.get("title") or "").strip()
    if not title:
        raise ApiError("请填写日程标题")
    data["title"] = title[:200]
    data["note"] = str(data.get("note") or "").strip()[:500]

    kind = data.get("kind") or "once"
    data["kind"] = kind if kind in ("once", "recurring") else "once"

    if data["kind"] == "recurring":
        weekday = data.get("weekday")
        if isinstance(weekday, str) and weekday.strip().isdigit():
            weekday = int(weekday)
        if weekday is None:
            if require_day:
                raise ApiError("每周循环的日程需要选择星期")
        elif not isinstance(weekday, int) or isinstance(weekday, bool) or not 0 <= weekday <= 6:
            raise ApiError("星期取值应为 0-6（0=周一）")
        data["weekday"] = weekday
        data["date"] = None
    else:
        data["date"] = parse_date(data.get("date"))
        if not data["date"] and require_day:
            raise ApiError("单日日程需要选择日期")
        data["weekday"] = None

    for key in ("start_time", "end_time"):
        data[key] = parse_time(data.get(key))
    if data["start_time"] and data["end_time"] and data["end_time"] <= data["start_time"]:
        raise ApiError("结束时间要晚于开始时间")

    for key in ("start_date", "end_date"):
        data[key] = parse_date(data.get(key))
    if data["start_date"] and data["end_date"] and data["end_date"] < data["start_date"]:
        raise ApiError("生效结束日期不能早于开始日期")
    return data


def validate_participants(session: Session, team_id: int, pids: Iterable) -> list[int]:
    out: list[int] = []
    for raw in pids or []:
        try:
            pid = int(raw)
        except (TypeError, ValueError):
            continue
        if pid in out:
            continue
        exists = session.scalar(
            select(TeamMember.user_id).where(
                TeamMember.team_id == team_id, TeamMember.user_id == pid
            )
        )
        if exists is None:
            raise ApiError("共有成员必须是团队成员", 404)
        out.append(pid)
    return out


def replace_participants(session: Session, entry: ScheduleEntry, pids: list[int]) -> None:
    users = list(session.scalars(select(User).where(User.id.in_(pids)))) if pids else []
    entry.participants = users


def resolve_owner(access, explicit) -> int:
    """新日程归属：默认自己；指定他人需 admin（免登录访客可通过 X-Actor-Id 指定自己）。"""
    ctx, team, role = access.ctx, access.team, access.effective_role()
    current_id = ctx.user_id
    if explicit not in (None, "", "null"):
        try:
            target = int(explicit)
        except (TypeError, ValueError):
            raise ApiError("归属成员必须是团队成员", 404) from None
        is_member = ctx.session.scalar(
            select(TeamMember.user_id).where(
                TeamMember.team_id == team.id, TeamMember.user_id == target
            )
        )
        if is_member is None:
            raise ApiError("归属成员必须是团队成员", 404)
        if target == current_id:
            return target
        if role in ADMIN_ROLES:
            return target
        if ctx.actor()[0] == target:
            return target
        raise ApiError("只有管理员可以代其他成员创建日程", 403)
    if current_id:
        return current_id
    actor_id = ctx.actor()[0]
    if actor_id is not None and ctx.session.scalar(
        select(TeamMember.user_id).where(
            TeamMember.team_id == team.id, TeamMember.user_id == actor_id
        )
    ):
        return actor_id
    # 免登录模式且还没选身份：团队一个成员都没有时自动建「我」占位
    has_member = ctx.session.scalar(
        select(TeamMember.user_id).where(TeamMember.team_id == team.id)
    )
    if ctx.user is None and has_member is None:
        user = User(
            username=unique_member_username(ctx.session),
            display_name="我",
            title="",
            email="",
            password_hash="",
            created_at=utcnow_iso(),
        )
        ctx.session.add(user)
        ctx.session.flush()
        ctx.session.add(
            TeamMember(team_id=team.id, user_id=user.id, role="member",
                       joined_at=utcnow_iso())
        )
        ctx.session.flush()
        return user.id
    raise ApiError("请先在侧栏选择操作身份")


def ensure_schedule_owner(access, entry: ScheduleEntry) -> None:
    """编辑/删除权限：本人或 admin。"""
    if access.effective_role() in ADMIN_ROLES:
        return
    actor_id = access.ctx.actor()[0]
    if actor_id is not None and actor_id == entry.user_id:
        return
    raise ApiError("只能修改自己的日程（管理员可修改全部）", 403)


def list_schedules(session: Session, team_id: int) -> list[ScheduleEntry]:
    return list(
        session.scalars(
            select(ScheduleEntry)
            .where(ScheduleEntry.team_id == team_id)
            .order_by(ScheduleEntry.id)
        )
    )


def get_or_404(session: Session, schedule_id: int) -> ScheduleEntry:
    entry = session.get(ScheduleEntry, schedule_id)
    if entry is None:
        raise ApiError("日程不存在", 404)
    return entry


def entry_fields(entry: ScheduleEntry) -> dict:
    """把已有日程摊平成 normalize_schedule 需要的 base 字典。"""
    return {
        "title": entry.title,
        "note": entry.note,
        "kind": entry.kind,
        "weekday": entry.weekday,
        "date": entry.date,
        "start_time": entry.start_time,
        "end_time": entry.end_time,
        "start_date": entry.start_date,
        "end_date": entry.end_date,
        "color": entry.color,
    }


def is_public(access) -> bool:
    return bool(access.team.schedules_public)


def can_read(access) -> bool:
    return access.effective_role() in EDITABLE_ROLES or is_public(access)


def ics_token_setting(team_id: int) -> str:
    return f"ics_token_{team_id}"


def new_ics_token() -> str:
    return new_token(16)
