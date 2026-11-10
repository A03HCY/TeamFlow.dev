"""标签（label）读写。"""
from __future__ import annotations

import re

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ..db.models import Issue, Label, issue_labels
from ..errors import ApiError

COLOR_RE = re.compile(r"#[0-9a-fA-F]{6}")
FALLBACK_COLOR = "#6b7280"


def _normalize_color(value, fallback: str | None = None) -> str:
    text = (value or "").strip()
    if COLOR_RE.fullmatch(text):
        return text.lower()
    return fallback if fallback else FALLBACK_COLOR


def use_counts(session: Session, team_id: int) -> dict[int, int]:
    """{label_id: 被多少 issue 使用}（管理员视图才需要）。"""
    rows = session.execute(
        select(issue_labels.c.label_id, func.count())
        .join(Issue, Issue.id == issue_labels.c.issue_id)
        .where(Issue.team_id == team_id)
        .group_by(issue_labels.c.label_id)
    )
    return {label_id: count for label_id, count in rows}


def list_labels(session: Session, team_id: int, with_uses: bool = False) -> list[dict]:
    labels = list(
        session.scalars(select(Label).where(Label.team_id == team_id).order_by(Label.name))
    )
    counts = use_counts(session, team_id) if with_uses else {}
    out = []
    for label in labels:
        item = {"id": label.id, "team_id": label.team_id,
                "name": label.name, "color": label.color}
        if with_uses:
            item["uses"] = counts.get(label.id, 0)
        out.append(item)
    return out


def by_name(session: Session, team_id: int, name: str) -> Label | None:
    return session.scalar(
        select(Label).where(Label.team_id == team_id, Label.name == name)
    )


def get_or_404(session: Session, label_id: int) -> Label:
    label = session.get(Label, label_id)
    if label is None:
        raise ApiError("标签不存在", 404)
    return label


def label_dict(label: Label, uses: int | None = None) -> dict:
    item = {"id": label.id, "team_id": label.team_id,
            "name": label.name, "color": label.color}
    if uses is not None:
        item["uses"] = uses
    return item


def create(session: Session, team_id: int, name, color) -> Label:
    text = (name or "").strip()
    if not text:
        raise ApiError("请填写标签名")
    if by_name(session, team_id, text[:40]) is not None:
        raise ApiError("标签已存在", 409)
    label = Label(team_id=team_id, name=text[:40], color=_normalize_color(color))
    session.add(label)
    session.flush()
    return label


def update(session: Session, label: Label, name, color) -> Label:
    text = (name or label.name).strip() or label.name
    if text != label.name and by_name(session, label.team_id, text[:40]) is not None:
        raise ApiError("标签已存在", 409)
    label.name = text[:40]
    label.color = _normalize_color(color, fallback=label.color)
    session.flush()
    return label


def remove(session: Session, label: Label) -> None:
    session.execute(delete(issue_labels).where(issue_labels.c.label_id == label.id))
    session.delete(label)
    session.flush()
