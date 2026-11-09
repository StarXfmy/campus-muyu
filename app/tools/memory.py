"""长期记忆：用户说「记住…」就存下来，之后每轮对话自动注入。

不按会话隔离：主人在微信私聊里说的偏好，微信群、飞书私聊也该知道。
chat_id 只用来记录「这条是哪说的」，读取和删除都不看它。

带时间的记忆（「周五交高数」「明天下午3点开会」）会解析出 remind_at，
到点在飞书提醒一次，并问要不要删。
"""
from __future__ import annotations

import re
import sqlite3
from datetime import date, datetime, time, timedelta

from app.config import DB_PATH

_WEEKDAY = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "日": 7, "天": 7}


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL,
            remind_at TEXT NOT NULL DEFAULT '',
            reminded INTEGER NOT NULL DEFAULT 0
        )"""
    )
    # 老库补列：SQLite 没有 ADD COLUMN IF NOT EXISTS
    for ddl in (
        "ALTER TABLE memories ADD COLUMN remind_at TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE memories ADD COLUMN reminded INTEGER NOT NULL DEFAULT 0",
    ):
        try:
            conn.execute(ddl)
        except sqlite3.OperationalError:
            pass
    conn.commit()


def _touch() -> None:
    try:
        from app.tools import sync_bitable

        sync_bitable.mark_dirty()
    except Exception:
        pass


def parse_remind_at(text: str) -> str:
    """从记忆原话里抓提醒时间，抓不到返回空串。

    只认常见说法：「明天下午3点」「周五」「11月5日」「晚上8点半」。
    只给日期不给时刻的，按当天 09:00 提醒。
    """
    text = text or ""
    now = datetime.now()
    day = None
    weekday_given = False

    if "大后天" in text:
        day = now.date() + timedelta(days=3)
    elif "后天" in text:
        day = now.date() + timedelta(days=2)
    elif "明天" in text or "明日" in text:
        day = now.date() + timedelta(days=1)
    elif "今天" in text or "今日" in text:
        day = now.date()
    else:
        match = re.search(r"(下下|下|本|这)?\s*(?:周|星期|礼拜)\s*([一二三四五六日天])", text)
        if match:
            target = _WEEKDAY[match.group(2)]
            delta = (target - now.isoweekday()) % 7
            if match.group(1) == "下":
                delta += 7
            elif match.group(1) == "下下":
                delta += 14
            day = now.date() + timedelta(days=delta)
            weekday_given = True
        else:
            match = re.search(r"(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]", text)
            if match:
                try:
                    day = date(now.year, int(match.group(1)), int(match.group(2)))
                except ValueError:
                    day = None

    hour = minute = None
    match = re.search(
        r"(上午|早上|中午|下午|傍晚|晚上)?\s*(\d{1,2})\s*[点时](?:\s*(\d{1,2})\s*分?|\s*半)?",
        text,
    )
    if match:
        period = match.group(1) or ""
        h = int(match.group(2))
        mm = match.group(3)
        if 0 <= h <= 23:
            if "半" in match.group(0):
                minute = 30
            else:
                minute = int(mm) if mm else 0
            if period in ("下午", "傍晚", "晚上") and h < 12:
                h += 12
            if period == "中午" and h < 11:
                h += 12
            hour = h
        else:
            minute = None

    if day is None and hour is None:
        return ""
    if day is None:
        day = now.date()

    if hour is None:
        hour, minute = 9, 0
    when = datetime.combine(day, time(hour, minute or 0))
    if when <= now:
        # 「周五」已经过点就指下周五；只说时刻的顺延到明天
        when += timedelta(days=7 if weekday_given else 1)
    return when.strftime("%Y-%m-%d %H:%M")


def add(chat_id: str, content: str, remind_at: str = "") -> int:
    content = (content or "").strip()[:120]
    conn = connect()
    _ensure(conn)
    try:
        cursor = conn.execute(
            "INSERT INTO memories (chat_id, content, created_at, remind_at, reminded) "
            "VALUES (?, ?, ?, ?, 0)",
            (
                chat_id,
                content,
                datetime.now().strftime("%Y-%m-%d %H:%M"),
                (remind_at or "").strip(),
            ),
        )
        conn.commit()
        _touch()
        return int(cursor.lastrowid or 0)
    finally:
        conn.close()


def all_items(chat_id: str | None = None, limit: int = 30) -> list[dict]:
    """所有记忆。chat_id 保留只为兼容旧调用，不参与过滤。"""
    conn = connect()
    _ensure(conn)
    try:
        rows = conn.execute(
            "SELECT id, content, created_at, remind_at, reminded "
            "FROM memories ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in reversed(rows)]


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", (text or "")).lower()


def remove(chat_id: str | None, keyword: str) -> int:
    """按关键词删，不分会话——记住的是同一个人。

    存进库的内容常被模型规整（「今天10点有ai应用课」），
    用原文做 LIKE 会因空格和大小写差异删不掉，所以两边归一化再比。
    """
    conn = connect()
    _ensure(conn)
    try:
        target = _norm(keyword)
        if not target:
            return 0
        rows = conn.execute("SELECT id, content FROM memories").fetchall()
        ids = [row[0] for row in rows if target in _norm(row[1])]
        for mem_id in ids:
            conn.execute("DELETE FROM memories WHERE id = ?", (mem_id,))
        conn.commit()
        _touch()
        return len(ids)
    finally:
        conn.close()


def due_reminders(now: datetime | None = None) -> list[dict]:
    """到点且还没提醒过的记忆。"""
    now = now or datetime.now()
    conn = connect()
    _ensure(conn)
    try:
        rows = conn.execute(
            "SELECT id, chat_id, content, remind_at FROM memories "
            "WHERE reminded = 0 AND remind_at != '' AND remind_at <= ? "
            "ORDER BY remind_at",
            (now.strftime("%Y-%m-%d %H:%M"),),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def mark_reminded(memory_id: int) -> None:
    conn = connect()
    _ensure(conn)
    try:
        conn.execute("UPDATE memories SET reminded = 1 WHERE id = ?", (int(memory_id),))
        conn.commit()
        _touch()
    finally:
        conn.close()
