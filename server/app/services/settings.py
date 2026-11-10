"""settings 表的读写糖（登录模式、默认团队、当前团队、AI 配置等）。"""
from __future__ import annotations

from sqlalchemy.orm import Session

from ..db.models import Setting


def get_setting(session: Session, key: str, default=None):
    row = session.get(Setting, key)
    return row.value if row is not None else default


def set_setting(session: Session, key: str, value) -> None:
    text = str(value)
    row = session.get(Setting, key)
    if row is None:
        session.add(Setting(key=key, value=text))
    else:
        row.value = text
    session.flush()


def del_setting(session: Session, key: str) -> None:
    row = session.get(Setting, key)
    if row is not None:
        session.delete(row)
        session.flush()


def flag(session: Session, key: str, default: str = "0") -> bool:
    return str(get_setting(session, key, default)) in ("1", "true", "True", "on")


def set_flag(session: Session, key: str, value: bool) -> None:
    set_setting(session, key, "1" if value else "0")


def require_login_on(session: Session) -> bool:
    return flag(session, "require_login")


def default_team_id(session: Session) -> int | None:
    value = get_setting(session, "default_team_id")
    if value and str(value).lstrip("-").isdigit():
        return int(value)
    return None


def active_team_id(session: Session, user_id: int | None) -> int | None:
    if not user_id:
        return None
    value = get_setting(session, f"active_team_{user_id}")
    if value and str(value).lstrip("-").isdigit():
        return int(value)
    return None
