"""主人额外定的人设规矩：全局生效，微信和飞书都读。

和 memories 的区别：memories 是「关于用户的事」，按会话存；
这里是「木鱼该怎么说话」，所有会话共用。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime

from app.config import DB_PATH

MAX_ITEMS = 30
MAX_CHARS = 120


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS persona_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL
        )"""
    )
    conn.commit()


def _touch() -> None:
    try:
        from app.tools import sync_bitable

        sync_bitable.mark_dirty()
    except Exception:
        pass


def all_items(limit: int = MAX_ITEMS) -> list[dict]:
    conn = connect()
    _ensure(conn)
    try:
        rows = conn.execute(
            "SELECT id, content, created_at FROM persona_rules ORDER BY id LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def add(content: str) -> int:
    """返回新 id；0=重复或空；-1=条数已满。"""
    content = (content or "").strip()[:MAX_CHARS]
    if not content:
        return 0
    conn = connect()
    _ensure(conn)
    try:
        if conn.execute("SELECT id FROM persona_rules WHERE content = ?", (content,)).fetchone():
            return 0
        count = conn.execute("SELECT COUNT(*) FROM persona_rules").fetchone()[0]
        if count >= MAX_ITEMS:
            return -1
        cur = conn.execute(
            "INSERT INTO persona_rules (content, created_at) VALUES (?, ?)",
            (content, datetime.now().strftime("%Y-%m-%d %H:%M")),
        )
        conn.commit()
        _touch()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


def remove(keyword: str) -> int:
    conn = connect()
    _ensure(conn)
    try:
        cur = conn.execute(
            "DELETE FROM persona_rules WHERE content LIKE ?", (f"%{keyword}%",)
        )
        conn.commit()
        _touch()
        return cur.rowcount
    finally:
        conn.close()


def clear() -> int:
    conn = connect()
    _ensure(conn)
    try:
        cur = conn.execute("DELETE FROM persona_rules")
        conn.commit()
        _touch()
        return cur.rowcount
    finally:
        conn.close()
