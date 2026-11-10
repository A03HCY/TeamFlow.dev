"""SQLite 备份：WAL 模式下直接拷文件会漏数据，必须走 sqlite 的在线备份 API。"""
from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime

from ..config import backup_dir, backup_enabled, database_config

_lock = threading.Lock()
_last_backup = 0.0


def backup_now(force: bool = False, min_interval: int = 600, keep: int = 20) -> str | None:
    """把 SQLite 库快照成 ``backups/data-YYYYmmdd-HHMMSS.db``。

    非 SQLite 后端直接返回 ``None``——Postgres/MySQL 请用 pg_dump / mysqldump。
    """
    global _last_backup
    cfg = database_config()
    if cfg.backend != "sqlite" or cfg.memory or not backup_enabled():
        return None

    with _lock:
        now_ts = time.time()
        if not force and now_ts - _last_backup < min_interval:
            return None
        directory = backup_dir()
        directory.mkdir(parents=True, exist_ok=True)
        dest = directory / datetime.now().strftime("data-%Y%m%d-%H%M%S.db")
        source = sqlite3.connect(str(cfg.path), timeout=15)
        try:
            target = sqlite3.connect(str(dest))
            try:
                source.backup(target)
            finally:
                target.close()
        finally:
            source.close()
        _last_backup = now_ts

        olds = sorted(
            (item for item in directory.iterdir()
             if item.name.startswith("data-") and item.suffix == ".db"),
            key=lambda item: item.name,
        )
        for stale in olds[:-keep] if len(olds) > keep else []:
            try:
                stale.unlink()
            except OSError:  # pragma: no cover
                pass
        return str(dest)
