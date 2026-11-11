"""新建团队不能因为「编号被复用 + 旧团队子表残留」而 500。

背景：teams.id 是 SQLite 的 rowid（max+1，不是自增序列）。删掉一个团队后，
下一个新建的团队会拿到同一个编号；如果旧团队的标签/成员没清干净，
写默认标签时就会撞 (team_id, name) 唯一约束，接口直接 500。
"""
from __future__ import annotations

import sqlite3

import pytest
from sqlalchemy import select

from app.db import session as dbsession
from app.db.models import Label
from app.db.session import session_scope
from app.services import teams as team_service

DEFAULT = sorted(name for name, _ in team_service.DEFAULT_LABELS)


def _labels(session, team_id: int) -> list[str]:
    rows = session.execute(select(Label.name).where(Label.team_id == team_id)).all()
    return sorted(name for (name,) in rows)


@pytest.fixture()
def file_db(tmp_path):
    """独立的文件库：需要一条「外键关掉时留下的孤儿行」，内存库做不到。"""
    path = tmp_path / "team-create.db"
    dbsession.configure(f"sqlite:///{path.as_posix()}")
    dbsession.init_db(seed=False)
    try:
        yield path
    finally:
        # 别把文件库留给后面的用例（每个 client 夹具会重新 configure，这里只是保险）
        dbsession.configure("sqlite:///:memory:")


def test_create_team_after_dirty_delete(file_db):
    """只删团队行、留下标签（模拟不干净的清理），新团队仍要建得出来。"""
    with session_scope() as session:
        first_id = team_service.create_team(session, "旧团队", "old-team").id

    con = sqlite3.connect(file_db)
    con.execute("PRAGMA foreign_keys=OFF")   # 生产库里那次手工清理正是这个状态
    con.execute("delete from team_members where team_id = ?", (first_id,))
    con.execute("delete from teams where id = ?", (first_id,))
    con.commit()
    left = con.execute("select count(*) from labels where team_id = ?", (first_id,)).fetchone()[0]
    con.close()
    assert left == len(DEFAULT), "应当留下整套孤儿标签"

    with session_scope() as session:
        second = team_service.create_team(session, "新团队", "new-team")
        assert second.id == first_id, "SQLite 会复用被删团队留下的编号"
        assert _labels(session, second.id) == DEFAULT


def test_create_team_keeps_its_own_labels(file_db):
    """正常路径：新团队必须拿到一整套默认标签（而不是被清理成空）。"""
    with session_scope() as session:
        team = team_service.create_team(session, "正常团队", "normal-team")
        assert _labels(session, team.id) == DEFAULT


def test_two_teams_do_not_share_labels(file_db):
    """两个团队各自一套标签，互不覆盖。"""
    with session_scope() as session:
        first = team_service.create_team(session, "甲", "team-a")
        second = team_service.create_team(session, "乙", "team-b")
        assert first.id != second.id
        assert _labels(session, first.id) == DEFAULT
        assert _labels(session, second.id) == DEFAULT
