"""主动推送开关：每个会话一个总开关，且一天最多推一条。

用户自建的「提醒」不受这个限制——那是他自己要求的时间点。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime

from app.config import DB_PATH


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS push_settings (
            chat_id TEXT PRIMARY KEY,
            enabled INTEGER NOT NULL DEFAULT 1,
            last_push_date TEXT NOT NULL DEFAULT ''
        )"""
    )
    conn.commit()


def set_enabled(chat_id: str, enabled: bool) -> None:
    conn = connect()
    _ensure(conn)
    try:
        conn.execute(
            """INSERT INTO push_settings (chat_id, enabled) VALUES (?, ?)
               ON CONFLICT(chat_id) DO UPDATE SET enabled = ?""",
            (chat_id, 1 if enabled else 0, 1 if enabled else 0),
        )
        conn.commit()
    finally:
        conn.close()


def is_enabled(chat_id: str) -> bool:
    """默认关：用户没显式开过就不主动推。

    他没说过关，同样没说过开——不能替他默认同意收推送。
    """
    conn = connect()
    _ensure(conn)
    try:
        row = conn.execute(
            "SELECT enabled FROM push_settings WHERE chat_id = ?", (chat_id,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return False
    return bool(row["enabled"])


def allow_today(chat_id: str, now: datetime | None = None) -> bool:
    """开着、且今天还没推过主动消息。

    无记录 = 用户从没开过 = 不许推。两个函数必须一致，否则开关等于没有。
    """
    now = now or datetime.now()
    today = now.strftime("%Y-%m-%d")
    conn = connect()
    _ensure(conn)
    try:
        row = conn.execute(
            "SELECT enabled, last_push_date FROM push_settings WHERE chat_id = ?", (chat_id,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return False
    if not row["enabled"]:
        return False
    return row["last_push_date"] != today


def mark_pushed(chat_id: str, now: datetime | None = None) -> None:
    now = now or datetime.now()
    conn = connect()
    _ensure(conn)
    try:
        conn.execute(
            "UPDATE push_settings SET last_push_date = ? WHERE chat_id = ?",
            (now.strftime("%Y-%m-%d"), chat_id),
        )
        conn.commit()
    finally:
        conn.close()
