"""每日早报订阅：存哪个会话、几点推、今天发过没。"""
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
        """CREATE TABLE IF NOT EXISTS daily_reports (
            chat_id TEXT PRIMARY KEY,
            hour INTEGER NOT NULL,
            minute INTEGER NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            last_sent TEXT NOT NULL DEFAULT '',
            sections TEXT NOT NULL DEFAULT ''
        )"""
    )
    try:
        conn.execute("ALTER TABLE daily_reports ADD COLUMN sections TEXT NOT NULL DEFAULT ''")
    except sqlite3.OperationalError:
        pass
    conn.commit()


def set_time(chat_id: str, hour: int, minute: int) -> None:
    conn = connect()
    _ensure(conn)
    try:
        conn.execute(
            """INSERT INTO daily_reports (chat_id, hour, minute, enabled, last_sent)
               VALUES (?, ?, ?, 1, '')
               ON CONFLICT(chat_id) DO UPDATE SET hour=?, minute=?, enabled=1""",
            (chat_id, hour, minute, hour, minute),
        )
        conn.commit()
    finally:
        conn.close()


def disable(chat_id: str) -> bool:
    conn = connect()
    _ensure(conn)
    try:
        cursor = conn.execute("UPDATE daily_reports SET enabled = 0 WHERE chat_id = ?", (chat_id,))
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def set_sections(chat_id: str, sections: str) -> None:
    """早报内容模块，逗号分隔；空串表示四项全发。"""
    conn = connect()
    _ensure(conn)
    try:
        conn.execute(
            """INSERT INTO daily_reports (chat_id, hour, minute, enabled, last_sent, sections)
               VALUES (?, 7, 30, 1, '', ?)
               ON CONFLICT(chat_id) DO UPDATE SET sections = ?""",
            (chat_id, sections, sections),
        )
        conn.commit()
    finally:
        conn.close()


def get(chat_id: str) -> dict | None:
    conn = connect()
    _ensure(conn)
    try:
        row = conn.execute(
            "SELECT chat_id, hour, minute, enabled, sections FROM daily_reports WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def due(now: datetime | None = None) -> list[dict]:
    """设定时间已过、且今天还没发过的订阅。开机晚了也能补发当天的。"""
    now = now or datetime.now()
    today = now.strftime("%Y-%m-%d")
    now_minutes = now.hour * 60 + now.minute
    conn = connect()
    _ensure(conn)
    try:
        rows = conn.execute(
            "SELECT chat_id, hour, minute FROM daily_reports WHERE enabled = 1 AND last_sent != ?",
            (today,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows if r["hour"] * 60 + r["minute"] <= now_minutes]


def mark_sent(chat_id: str, day: str | None = None) -> None:
    day = day or datetime.now().strftime("%Y-%m-%d")
    conn = connect()
    _ensure(conn)
    try:
        conn.execute("UPDATE daily_reports SET last_sent = ? WHERE chat_id = ?", (day, chat_id))
        conn.commit()
    finally:
        conn.close()
