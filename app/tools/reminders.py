"""提醒：存 SQLite，后台调度到点推飞书。

限制：只有 Bot 进程在跑时才会推送（长连接模式没有云端托管）。
进程重启后未到期的提醒还在，到期的会立刻补发一次。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

from app.tools.classroom import connect

SCHEMA = """
CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id TEXT NOT NULL,
    content TEXT NOT NULL,
    remind_at TEXT NOT NULL,
    sent INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders (sent, remind_at);
"""


def _ensure(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def add(chat_id: str, content: str, remind_at: datetime) -> int:
    conn = connect()
    _ensure(conn)
    try:
        cursor = conn.execute(
            "INSERT INTO reminders (chat_id, content, remind_at, created_at) VALUES (?,?,?,?)",
            (chat_id, content, remind_at.strftime("%Y-%m-%d %H:%M"), datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )
        conn.commit()
        return cursor.lastrowid or 0
    finally:
        conn.close()


def due(now: datetime | None = None) -> list[dict[str, Any]]:
    """取所有已到点且未发送的提醒。"""
    now = now or datetime.now()
    conn = connect()
    _ensure(conn)
    try:
        rows = conn.execute(
            "SELECT id, chat_id, content, remind_at FROM reminders "
            "WHERE sent = 0 AND remind_at <= ? ORDER BY remind_at",
            (now.strftime("%Y-%m-%d %H:%M"),),
        ).fetchall()
    finally:
        conn.close()
    return [{"id": r[0], "chat_id": r[1], "content": r[2], "remind_at": r[3]} for r in rows]


def mark_sent(reminder_id: int) -> None:
    conn = connect()
    _ensure(conn)
    try:
        conn.execute("UPDATE reminders SET sent = 1 WHERE id = ?", (reminder_id,))
        conn.commit()
    finally:
        conn.close()


def pending(chat_id: str) -> list[dict[str, Any]]:
    conn = connect()
    _ensure(conn)
    try:
        rows = conn.execute(
            "SELECT id, content, remind_at FROM reminders WHERE chat_id = ? AND sent = 0 ORDER BY remind_at",
            (chat_id,),
        ).fetchall()
    finally:
        conn.close()
    return [{"id": r[0], "content": r[1], "remind_at": r[2]} for r in rows]


def retext(chat_id: str, reminder_id: int, content: str) -> bool:
    """改一条还没触发的提醒的内容。"""
    conn = connect()
    _ensure(conn)
    try:
        cursor = conn.execute(
            "UPDATE reminders SET content = ? WHERE id = ? AND chat_id = ? AND sent = 0",
            (content[:60], reminder_id, chat_id),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def reschedule(chat_id: str, reminder_id: int, remind_at: datetime) -> bool:
    """改一条还没触发的提醒的时间。"""
    conn = connect()
    _ensure(conn)
    try:
        cursor = conn.execute(
            "UPDATE reminders SET remind_at = ? WHERE id = ? AND chat_id = ? AND sent = 0",
            (remind_at.strftime("%Y-%m-%d %H:%M"), reminder_id, chat_id),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def cancel(chat_id: str, reminder_id: int) -> bool:
    conn = connect()
    _ensure(conn)
    try:
        cursor = conn.execute(
            "DELETE FROM reminders WHERE id = ? AND chat_id = ? AND sent = 0",
            (reminder_id, chat_id),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()
