"""SQLAlchemy 声明式基类。

约定（原项目的硬性约束，逐条保留）：

* 所有时间列都是 **TEXT**，格式 ``YYYY-MM-DDTHH:MM:SSZ``（UTC）。
  这样旧的 ``data.db`` 数据、前端的 ``new Date(str)`` 与 ``str.slice(0,10)`` 全部照旧可用。
* 布尔值统一存 INTEGER ``0``/``1``（旧库就是这样，避免跨库差异）。
* 文本默认值走 Python 侧 ``default=``，不用 ``server_default``：
  MySQL 的 TEXT 列不允许默认值，Python 侧默认三库通吃。
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

# 统一的约束命名，方便 Alembic 与手工迁移对齐
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


# ------------------------------------------------------------------ 时间

def utcnow_iso() -> str:
    """当前 UTC 时间的 ISO 字符串（本项目统一的落库格式）。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today_iso() -> str:
    """本地日期 ``YYYY-MM-DD``（逾期判断用本地时区更符合直觉）。"""
    return date.today().isoformat()
