"""意图判断与纠正的记录，给自学习用。

三层关系：
1. 记录层：每次问答写一行 intent_logs（本文件）
2. 纠正记忆层：用户说「错了/不是」写 intent_corrections，下次问类似问题时注入提示
3. 规则审核层：同一问法被纠正次数达标，提给主人确认后才进 router._RULES

规则永远不自动改，只由主人确认。
"""
from __future__ import annotations

import re
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
        """CREATE TABLE IF NOT EXISTS intent_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT,
            question TEXT NOT NULL,
            intent TEXT,
            route TEXT,
            answer TEXT,
            created_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS intent_corrections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT,
            question TEXT NOT NULL,
            wrong_intent TEXT,
            correction TEXT NOT NULL,
            confirmed INTEGER DEFAULT 0,
            created_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS answer_feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT,
            question TEXT NOT NULL,
            answer TEXT,
            verdict TEXT NOT NULL,
            created_at TEXT NOT NULL
        )"""
    )
    conn.commit()


def record_answer_wrong(chat_id: str | None, question: str, answer: str = "") -> None:
    """用户点「答得不对」时记一笔，供以后看哪些问题老出错。"""
    if not (question or "").strip():
        return
    conn = connect()
    _ensure(conn)
    try:
        conn.execute(
            "INSERT INTO answer_feedback (chat_id, question, answer, verdict, created_at) "
            "VALUES (?,?,?,?,?)",
            (
                chat_id or "",
                question.strip()[:200],
                (answer or "")[:500],
                "wrong",
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def record(chat_id: str | None, question: str, intent: str, route: str) -> int:
    """记一次意图判断。route: rule / faq / agent / sdk。"""
    conn = connect()
    _ensure(conn)
    try:
        cur = conn.execute(
            "INSERT INTO intent_logs (chat_id, question, intent, route, answer, created_at) "
            "VALUES (?, ?, ?, ?, '', ?)",
            (
                chat_id or "",
                question or "",
                intent or "",
                route or "",
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def record_answer(chat_id: str | None, answer: str) -> None:
    """给该 chat 最后一条日志补上回答。"""
    if not chat_id:
        return
    conn = connect()
    _ensure(conn)
    try:
        row = conn.execute(
            "SELECT id FROM intent_logs WHERE chat_id = ? ORDER BY id DESC LIMIT 1",
            (chat_id,),
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE intent_logs SET answer = ? WHERE id = ?",
                ((answer or "")[:2000], row["id"]),
            )
            conn.commit()
    finally:
        conn.close()


def record_correction(
    chat_id: str | None,
    question: str,
    wrong_intent: str,
    correction: str,
) -> int:
    """记一条用户纠正。question 是用户说错了的那句原话。"""
    conn = connect()
    _ensure(conn)
    try:
        cur = conn.execute(
            "INSERT INTO intent_corrections "
            "(chat_id, question, wrong_intent, correction, confirmed, created_at) "
            "VALUES (?, ?, ?, ?, 0, ?)",
            (
                chat_id or "",
                question or "",
                wrong_intent or "",
                correction or "",
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def similar_corrections(text: str, limit: int = 3) -> list[dict]:
    """找和当前问法相关的已确认纠正，给提示词用。"""
    if not text:
        return []
    tokens = [t for t in re.split(r"[^\w一-鿿]+", text) if len(t) >= 2][:3]
    if not tokens:
        return []
    conn = connect()
    _ensure(conn)
    try:
        rows = conn.execute(
            "SELECT question, correction FROM intent_corrections "
            "WHERE confirmed = 1 ORDER BY id DESC LIMIT 50"
        ).fetchall()
    finally:
        conn.close()

    scored: list[tuple[int, dict]] = []
    seen_questions: set[str] = set()
    for row in rows:
        question = row["question"] or ""
        # 同问法只取最新一条：旧纠正保留在库里可查，但不再生效
        if question in seen_questions:
            continue
        correction = row["correction"] or ""
        hits = sum(1 for t in tokens if t in question or t in correction)
        if hits:
            seen_questions.add(question)
            scored.append((hits, dict(row)))
    scored.sort(key=lambda item: -item[0])
    return [item for _, item in scored[:limit]]


def pending_audit(threshold: int = 3, limit: int = 5) -> list[dict]:
    """同一问法被纠正 threshold 次以上、还没进规则的，给审核用。"""
    conn = connect()
    _ensure(conn)
    try:
        rows = conn.execute(
            """SELECT question, wrong_intent, MIN(correction) AS correction, COUNT(*) AS cnt,
                      MIN(id) AS first_id
               FROM intent_corrections
               WHERE confirmed = 0
               GROUP BY question, wrong_intent
               HAVING cnt >= ?
               ORDER BY cnt DESC
               LIMIT ?""",
            (threshold, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def mark_confirmed(first_id: int) -> None:
    """主人确认后，把这批纠正标成已处理。"""
    conn = connect()
    _ensure(conn)
    try:
        conn.execute(
            "UPDATE intent_corrections SET confirmed = 1 WHERE id >= ?",
            (first_id,),
        )
        conn.commit()
    finally:
        conn.close()


def resolve_audit(question: str, wrong_intent: str, status: int) -> int:
    """按问法批量处理待审核纠正：1=确认记住，-2=忽略丢弃。"""
    conn = connect()
    _ensure(conn)
    try:
        cur = conn.execute(
            "UPDATE intent_corrections SET confirmed = ? "
            "WHERE question = ? AND wrong_intent = ? AND confirmed = 0",
            (status, question or "", wrong_intent or ""),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def update_correction(chat_id: str | None, index: int, new_text: str) -> tuple[str, str] | None:
    """改第 index 条纠正（1 是最新）。返回 (旧问题, 新内容)，改不到返回 None。"""
    if index < 1 or not new_text:
        return None
    conn = connect()
    _ensure(conn)
    try:
        row = conn.execute(
            "SELECT id, question FROM intent_corrections "
            "WHERE chat_id = ? AND confirmed >= 0 ORDER BY id DESC LIMIT 1 OFFSET ?",
            (chat_id or "", index - 1),
        ).fetchone()
        if not row:
            return None
        conn.execute(
            "UPDATE intent_corrections SET correction = ? WHERE id = ?",
            (new_text, row["id"]),
        )
        conn.commit()
        return row["question"], new_text
    finally:
        conn.close()
