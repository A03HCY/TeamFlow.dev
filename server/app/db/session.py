"""引擎 / 会话 / 初始化。

数据库由 ``TEAMFLOW_DB_URL`` 决定（也认 ``DATABASE_URL`` / 旧变量 ``TMT_DB``）：

    sqlite:///D:/Projects/TeamFlow/data.db     # 默认
    postgresql://user:pass@127.0.0.1:5432/teamflow
    mysql://user:pass@127.0.0.1:3306/teamflow

SQLite 需要自带驱动（标准库），PostgreSQL 需要 ``psycopg``（或 ``psycopg2``），
MySQL 需要 ``pymysql``；缺失时给出明确的安装提示而不是一个含糊的 ImportError。
"""
from __future__ import annotations

import importlib.util
import threading
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import event, inspect, text
from sqlalchemy.engine import URL, Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from ..config import DatabaseConfig, database_config
from .base import Base
from . import engine_models  # noqa: F401  推理引擎的 9 张表
from . import models  # noqa: F401  确保所有表注册进 Base.metadata


class DriverMissing(RuntimeError):
    """缺少某个数据库的 Python 驱动。"""


_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None
_lock = threading.Lock()


# ------------------------------------------------------------------ 引擎

def _has_module(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def _sqlalchemy_url(cfg: DatabaseConfig) -> URL | str:
    if cfg.backend == "sqlite":
        if cfg.memory:
            return "sqlite://"
        return URL.create("sqlite", database=str(cfg.path))
    if cfg.backend == "postgresql":
        if _has_module("psycopg"):
            drivername = "postgresql+psycopg"
        elif _has_module("psycopg2"):
            drivername = "postgresql+psycopg2"
        else:  # pragma: no cover - 取决于部署环境
            raise DriverMissing(
                "连接 PostgreSQL 需要驱动，请先执行：\n"
                '  pip install "psycopg[binary]"     # 推荐\n'
                "  pip install psycopg2-binary       # 备选"
            )
        return URL.create(
            drivername,
            username=cfg.user or None,
            password=cfg.password or None,
            host=cfg.host or None,
            port=cfg.port,
            database=cfg.database or None,
        )
    if cfg.backend == "mysql":
        if not _has_module("pymysql"):  # pragma: no cover - 取决于部署环境
            raise DriverMissing(
                "连接 MySQL 需要驱动，请先执行：\n  pip install pymysql"
            )
        return URL.create(
            "mysql+pymysql",
            username=cfg.user or None,
            password=cfg.password or None,
            host=cfg.host or None,
            port=cfg.port,
            database=cfg.database or None,
        )
    raise ValueError(f"不支持的数据库协议：{cfg.backend}")


def _engine_options(cfg: DatabaseConfig) -> dict:
    options: dict = {"future": True, "pool_pre_ping": True}
    if cfg.backend == "sqlite":
        options["connect_args"] = {
            "timeout": 15,
            "check_same_thread": False,
        }
        if cfg.memory:
            # 内存库必须共用同一条连接，否则每个连接看到的是不同的空库
            options["poolclass"] = StaticPool
    return options


def _install_listeners(engine: Engine, cfg: DatabaseConfig) -> None:
    if cfg.backend != "sqlite":
        return

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_conn, _record):  # noqa: ANN001
        cur = dbapi_conn.cursor()
        try:
            cur.execute("PRAGMA foreign_keys = ON")
            cur.execute("PRAGMA synchronous = NORMAL")
            if not cfg.memory:
                cur.execute("PRAGMA journal_mode = WAL")
        finally:
            cur.close()


def create_database_engine(cfg: DatabaseConfig | None = None) -> Engine:
    cfg = cfg or database_config()
    engine = create_engine(_sqlalchemy_url(cfg), **_engine_options(cfg))
    _install_listeners(engine, cfg)
    return engine


def configure(url: str | None = None) -> Engine:
    """（重新）配置全局引擎；``url`` 为空时读取环境变量。"""
    global _engine, _session_factory
    cfg = database_config(url)
    engine = create_database_engine(cfg)
    with _lock:
        if _engine is not None:
            _engine.dispose()
        _engine = engine
        _session_factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    return engine


def get_engine() -> Engine:
    if _engine is None:
        configure()
    assert _engine is not None
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    get_engine()
    assert _session_factory is not None
    return _session_factory


@contextmanager
def session_scope() -> Iterator[Session]:
    """非请求上下文（启动、脚本、测试）里的事务包装。"""
    factory = get_session_factory()
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Iterator[Session]:
    """FastAPI 依赖：每个请求一个 Session，请求结束即提交/回滚。"""
    factory = get_session_factory()
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# ------------------------------------------------------------------ 建表 / 补列

def _default_literal(column) -> str | None:  # noqa: ANN001
    default = getattr(column, "default", None)
    if default is None or not getattr(default, "is_scalar", False):
        return None
    value = default.arg
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    return None


def sync_schema(engine: Engine | None = None) -> None:
    """``create_all`` 不会改动已存在的表；这里补上后来新增的列。"""
    engine = engine or get_engine()
    inspector = inspect(engine)
    existing = set(inspector.get_table_names())
    Base.metadata.create_all(engine)
    missing_columns = 0
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in existing:
                continue
            have = {col["name"] for col in inspect(engine).get_columns(table.name)}
            for column in table.columns:
                if column.name in have or column.primary_key:
                    continue
                ddl_type = column.type.compile(dialect=engine.dialect)
                ddl = (
                    f"ALTER TABLE {conn.dialect.identifier_preparer.quote(table.name)} "
                    f"ADD COLUMN {conn.dialect.identifier_preparer.quote(column.name)} {ddl_type}"
                )
                literal = _default_literal(column)
                if not column.nullable and literal is not None:
                    ddl += f" DEFAULT {literal}"
                conn.execute(text(ddl))
                missing_columns += 1
    # 先补编号再建唯一索引：老库刚加的 number 列全是 0，直接建唯一索引会冲突。
    _backfill_milestone_numbers(engine)
    # 老表上新增的索引（如 uq_ms_number）create_all 不会补，需要显式创建。
    for table in Base.metadata.sorted_tables:
        for index in table.indexes:
            try:
                index.create(bind=engine, checkfirst=True)
            except Exception:  # noqa: BLE001 - 索引已存在或后端不支持时忽略
                pass


def _backfill_milestone_numbers(engine: Engine) -> None:
    """老库里 milestones 没有 number，按 id 顺序补成 1..N。"""
    with engine.begin() as conn:
        rows = conn.execute(
            text("SELECT id, team_id FROM milestones ORDER BY team_id, id")
        ).fetchall()
    if not rows:
        return
    counters: dict[int, int] = {}
    with engine.begin() as conn:
        for milestone_id, team_id in rows:
            counters[team_id] = counters.get(team_id, 0) + 1
            conn.execute(
                text("UPDATE milestones SET number = :n WHERE id = :i AND (number IS NULL OR number = 0)"),
                {"n": counters[team_id], "i": milestone_id},
            )


# ------------------------------------------------------------------ 初始化

def init_db(seed: bool | None = None) -> Engine:
    """建表、补列、必要时写入初始数据；幂等。"""
    from .seed import create_default_team, seed_enabled, seed_demo  # 延迟导入避免环

    engine = get_engine()
    sync_schema(engine)
    with session_scope() as session:
        if session.query(models.Team).count() == 0:
            if seed is None:
                seed = seed_enabled()
            if seed:
                seed_demo(session)
            else:
                create_default_team(session)
        from ..services.settings import get_setting, set_setting

        if get_setting(session, "require_login") is None:
            set_setting(session, "require_login", "0")
    return engine
