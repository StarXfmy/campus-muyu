"""可观测：滚动文件日志 + audit 表。

只记元数据——谁问的（哈希）、什么意图、调了哪些工具、多久、成没成、有没有降级。
不记聊天原文：那是最该保护的东西，排障也不需要它。
"""
from __future__ import annotations

import hashlib
import logging
import logging.handlers
import sqlite3
from datetime import datetime
from pathlib import Path

from app.config import DATA_DIR, DB_PATH

logger = logging.getLogger("campus-bot.audit")

LOG_DIR = DATA_DIR.parent / "logs"
MAX_BYTES = 10 * 1024 * 1024
BACKUP_COUNT = 5

SCHEMA = """
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    chat_id_hash TEXT NOT NULL DEFAULT '',
    intent TEXT NOT NULL DEFAULT '',
    tools TEXT NOT NULL DEFAULT '',
    latency_ms INTEGER NOT NULL DEFAULT 0,
    ok INTEGER NOT NULL DEFAULT 1,
    degraded INTEGER NOT NULL DEFAULT 0
)
"""


def setup_logging(level: int = logging.INFO, log_dir: Path | None = None) -> Path:
    """挂上按天滚动的文件日志。同一天内单文件超 10MB 自动切分，留 5 份。

    重复调用是安全的（已有同名 handler 就不再挂）。
    """
    target = Path(log_dir or LOG_DIR)
    target.mkdir(parents=True, exist_ok=True)
    path = target / f"bot-{datetime.now():%Y%m%d}.log"

    root = logging.getLogger()
    for handler in root.handlers:
        if isinstance(handler, logging.handlers.RotatingFileHandler) and getattr(
            handler, "baseFilename", ""
        ) == str(path.resolve()):
            return path

    handler = logging.handlers.RotatingFileHandler(
        path, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    root.addHandler(handler)
    if root.level > level:
        root.setLevel(level)
    return path


def hash_chat(chat_id: str) -> str:
    """聊天 ID 只存哈希前 12 位，能在日志里对上同一个人，但还原不出是谁。"""
    return hashlib.sha256((chat_id or "").encode("utf-8")).hexdigest()[:12]


def _connect(db_path: Path | None = None) -> sqlite3.Connection:
    target = Path(db_path or DB_PATH)
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target, timeout=10)
    conn.execute(SCHEMA)
    conn.commit()
    return conn


def record(
    chat_id: str,
    intent: str,
    tools: str = "",
    latency_ms: int = 0,
    ok: bool = True,
    degraded: bool = False,
    db_path: Path | None = None,
) -> bool:
    """写一条审计记录。失败只记日志，不影响正常回复。"""
    try:
        conn = _connect(db_path)
        try:
            conn.execute(
                "INSERT INTO audit (ts, chat_id_hash, intent, tools, latency_ms, ok, degraded)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    hash_chat(chat_id),
                    intent or "",
                    tools or "",
                    int(latency_ms),
                    1 if ok else 0,
                    1 if degraded else 0,
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception:
        logger.exception("审计写入失败")
        return False


def recent(limit: int = 10, db_path: Path | None = None) -> list[dict]:
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT ts, chat_id_hash, intent, tools, latency_ms, ok, degraded"
            " FROM audit ORDER BY id DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
    finally:
        conn.close()
    return [
        {
            "ts": row[0],
            "chat_id_hash": row[1],
            "intent": row[2],
            "tools": row[3],
            "latency_ms": row[4],
            "ok": bool(row[5]),
            "degraded": bool(row[6]),
        }
        for row in rows
    ]
