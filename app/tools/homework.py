"""作业/DDL 看板：口述录入、本周欠交、截止前推送。

课程名从真实课表里模糊匹配，写死的别名只是补充。
推送属于「用户自己录的」，不受主动推送总开关限制（和提醒同类）。
"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta

from app.config import DB_PATH
from app.tools import schedule_store

# 口语简称 → 真实课程名里的关键词（真实课表只有 11 门，别名够用）
_ALIASES = {
    "高数": "高等数学",
    "大英": "大学英语",
    "英语": "大学英语",
    "线代": "线性代数",
    "近代史": "中国近现代史",
    "党史": "中国共产党党史",
    "c语言": "C语言程序设计",
    "制图": "工程制图",
    "心理": "大学生心理健康",
    "导论": "电子信息类学科导论",
    "形势": "形势与政策",
    "体育": "体育",
}


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS homework (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT NOT NULL,
            course TEXT NOT NULL DEFAULT '',
            content TEXT NOT NULL,
            due_at TEXT NOT NULL,
            done INTEGER NOT NULL DEFAULT 0,
            notified_day INTEGER NOT NULL DEFAULT 0,
            notified_soon INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )"""
    )
    conn.commit()


def match_course(text: str) -> str:
    """把「高数」「大英」对上真实课程名；对不上就返回空串。"""
    text = (text or "").lower()
    try:
        courses = [item["course"] for item in schedule_store.all_courses()]
    except Exception:
        courses = []

    for name in courses:
        base = re.sub(r"[（(].*?[)）]", "", name).strip()
        if name.lower() in text or (base and base.lower() in text):
            return name
    for alias, keyword in _ALIASES.items():
        if alias in text:
            for name in courses:
                if keyword in name:
                    return name
            return keyword
    return ""


def add(chat_id: str, course: str, content: str, due_at: str) -> int:
    conn = connect()
    _ensure(conn)
    try:
        cursor = conn.execute(
            """INSERT INTO homework (chat_id, course, content, due_at, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (chat_id, course, content[:80], due_at, datetime.now().strftime("%Y-%m-%d %H:%M")),
        )
        conn.commit()
        return int(cursor.lastrowid or 0)
    finally:
        conn.close()


def pending(chat_id: str) -> list[dict]:
    conn = connect()
    _ensure(conn)
    try:
        rows = conn.execute(
            "SELECT id, course, content, due_at FROM homework WHERE chat_id = ? AND done = 0 ORDER BY due_at",
            (chat_id,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def finish(chat_id: str, keyword: str) -> int:
    conn = connect()
    _ensure(conn)
    try:
        cursor = conn.execute(
            """UPDATE homework SET done = 1
               WHERE chat_id = ? AND done = 0 AND (content LIKE ? OR course LIKE ?)""",
            (chat_id, f"%{keyword}%", f"%{keyword}%"),
        )
        conn.commit()
        return cursor.rowcount
    finally:
        conn.close()


def reschedule(chat_id: str, keyword: str, due_at: str) -> int:
    """改截止时间，并把两档提醒标记清零，按新时间重新提醒。"""
    conn = connect()
    _ensure(conn)
    try:
        cursor = conn.execute(
            """UPDATE homework SET due_at = ?, notified_day = 0, notified_soon = 0
               WHERE chat_id = ? AND done = 0 AND (content LIKE ? OR course LIKE ?)""",
            (due_at, chat_id, f"%{keyword}%", f"%{keyword}%"),
        )
        conn.commit()
        return cursor.rowcount
    finally:
        conn.close()


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", (text or "")).lower()


def remove(chat_id: str, keyword: str) -> int:
    """按关键词直接删除（录错、不想要了）。

    库里存的可能是「今天10点有ai应用课」，用户说的是「今天 10 点有 AI 应用课」，
    原文 LIKE 匹配不上，所以归一化再比。
    """
    conn = connect()
    _ensure(conn)
    try:
        target = _norm(keyword)
        if not target:
            return 0
        rows = conn.execute(
            "SELECT id, course, content FROM homework WHERE chat_id = ?", (chat_id,)
        ).fetchall()
        ids = [row[0] for row in rows if target in _norm(row[1]) or target in _norm(row[2])]
        for hw_id in ids:
            conn.execute("DELETE FROM homework WHERE id = ?", (hw_id,))
        conn.commit()
        return len(ids)
    finally:
        conn.close()


def due_for_notice(now: datetime | None = None) -> list[dict]:
    """截止前 24 小时、前 2 小时各推一次；返回 [(kind, item)]。"""
    now = now or datetime.now()
    conn = connect()
    _ensure(conn)
    try:
        rows = conn.execute(
            "SELECT id, chat_id, course, content, due_at, notified_day, notified_soon "
            "FROM homework WHERE done = 0"
        ).fetchall()
    finally:
        conn.close()

    hits: list[tuple[str, dict]] = []
    for row in rows:
        try:
            due = datetime.strptime(row["due_at"], "%Y-%m-%d %H:%M")
        except (ValueError, TypeError):
            continue
        left = (due - now).total_seconds()
        if left < 0:
            continue
        if left <= 2 * 3600 and not row["notified_soon"]:
            hits.append(("soon", dict(row)))
        elif left <= 24 * 3600 and not row["notified_day"]:
            hits.append(("day", dict(row)))
    return hits


def mark_notified(item_id: int, kind: str) -> None:
    conn = connect()
    _ensure(conn)
    try:
        if kind == "soon":
            # 2 小时那条推过之后，24 小时那条也不用再补了
            conn.execute("UPDATE homework SET notified_soon = 1, notified_day = 1 WHERE id = ?", (item_id,))
        else:
            conn.execute("UPDATE homework SET notified_day = 1 WHERE id = ?", (item_id,))
        conn.commit()
    finally:
        conn.close()
