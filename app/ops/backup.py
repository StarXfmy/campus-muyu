"""数据库在线备份。

用 SQLite 的 backup API，不直接复制文件——WAL 模式下拷文件可能拿到不一致的快照。
文件名固定 campus-YYYYMMDD.db（一天一份，同天重跑覆盖），保留 7 份。
手工留的别的备份（campus-pre-batch-*.db）不参与清理。
"""
from __future__ import annotations

import logging
import re
import sqlite3
from datetime import datetime
from pathlib import Path

from app.config import DATA_DIR, DB_PATH

logger = logging.getLogger("campus-bot.backup")

BACKUP_DIR = DATA_DIR / "backups"
KEEP = 7
_PATTERN = re.compile(r"campus-\d{8}\.db")


def _dest(now: datetime | None = None) -> Path:
    now = now or datetime.now()
    return BACKUP_DIR / f"campus-{now:%Y%m%d}.db"


def prune(keep: int = KEEP) -> list[str]:
    """只清自己的每日备份，返回被删的文件名。"""
    files = sorted(
        path for path in BACKUP_DIR.glob("campus-*.db") if _PATTERN.fullmatch(path.name)
    )
    removed = []
    for old in files[:-keep] if len(files) > keep else []:
        try:
            old.unlink()
            removed.append(old.name)
            logger.info("清理旧备份 %s", old.name)
        except OSError:
            logger.warning("旧备份删不掉 %s", old.name)
    return removed


def run(now: datetime | None = None, db_path: Path | None = None) -> Path | None:
    """备份一次，返回备份文件路径；失败返回 None。"""
    source = Path(db_path or DB_PATH)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    dest = _dest(now)

    src = dst = None
    try:
        src = sqlite3.connect(source, timeout=10)
        dst = sqlite3.connect(dest, timeout=10)
        src.backup(dst)
        logger.info("数据库已备份 %s", dest.name)
    except Exception:
        logger.exception("数据库备份失败")
        return None
    finally:
        for conn in (dst, src):
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    prune()
    return dest


def stats(path: Path) -> dict:
    """备份文件里有哪些表、各多少行——恢复核对用。"""
    conn = sqlite3.connect(path)
    try:
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        return {name: conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0] for name in tables}
    finally:
        conn.close()
