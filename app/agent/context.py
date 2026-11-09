"""会话上下文：按 chat_id 存最近若干轮对话，供意图识别消歧。

存 SQLite，Bot 重启后还能记得之前聊过什么。
每个会话只保留最近 20 条，超出的自动删除，防止库无限膨胀。
"""
from __future__ import annotations

import re
import sqlite3
import threading
from datetime import datetime, timedelta
from typing import Any

from app.tools.classroom import connect

MAX_MESSAGES = 20

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id TEXT NOT NULL,
    role TEXT NOT NULL,
    text TEXT NOT NULL,
    intent TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conversations_chat ON conversations (chat_id, id);
CREATE TABLE IF NOT EXISTS turn_summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id TEXT NOT NULL,
    question TEXT NOT NULL,
    summary TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_turn_chat ON turn_summaries (chat_id, id);
"""


def _ensure(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def remember(chat_id: str, user_text: str, intent: str, bot_text: str = "") -> None:
    if not chat_id or not user_text:
        return
    conn = connect()
    _ensure(conn)
    try:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "INSERT INTO conversations (chat_id, role, text, intent, created_at) VALUES (?,?,?,?,?)",
            (chat_id, "user", user_text[:500], intent, now),
        )
        if bot_text:
            conn.execute(
                "INSERT INTO conversations (chat_id, role, text, intent, created_at) VALUES (?,?,?,?,?)",
                (chat_id, "bot", bot_text[:500], intent, now),
            )
        conn.execute(
            "DELETE FROM conversations WHERE chat_id = ? AND id NOT IN "
            "(SELECT id FROM conversations WHERE chat_id = ? ORDER BY id DESC LIMIT ?)",
            (chat_id, chat_id, MAX_MESSAGES),
        )
        conn.commit()
    finally:
        conn.close()


def last_intent(chat_id: str) -> str | None:
    if not chat_id:
        return None
    conn = connect()
    _ensure(conn)
    try:
        row = conn.execute(
            "SELECT intent FROM conversations WHERE chat_id = ? AND role = 'user' ORDER BY id DESC LIMIT 1",
            (chat_id,),
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row else None


def last_user_text(chat_id: str) -> str | None:
    if not chat_id:
        return None
    conn = connect()
    _ensure(conn)
    try:
        row = conn.execute(
            "SELECT text FROM conversations WHERE chat_id = ? AND role = 'user' ORDER BY id DESC LIMIT 1",
            (chat_id,),
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row else None


def last_reply(chat_id: str) -> str:
    if not chat_id:
        return ""
    conn = connect()
    _ensure(conn)
    try:
        row = conn.execute(
            "SELECT text FROM conversations WHERE chat_id = ? AND role = 'bot' ORDER BY id DESC LIMIT 1",
            (chat_id,),
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row else ""


def recent(chat_id: str, limit: int = 10) -> list[dict[str, Any]]:
    if not chat_id:
        return []
    conn = connect()
    _ensure(conn)
    try:
        rows = conn.execute(
            "SELECT role, text FROM conversations WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
            (chat_id, limit),
        ).fetchall()
    finally:
        conn.close()
    return [{"role": r[0], "text": r[1]} for r in reversed(rows)]


def clear(chat_id: str) -> None:
    conn = connect()
    _ensure(conn)
    try:
        conn.execute("DELETE FROM conversations WHERE chat_id = ?", (chat_id,))
        conn.commit()
    finally:
        conn.close()


# ---- 短期上下文：只给下一两轮接话用，不写长期记忆 ----

_FILLER_PREFIXES = ("好的", "好嘞", "嗯", "收到", "没问题", "哈哈", "行", "ok", "OK")
_SENTENCE_SPLIT = re.compile(r"(?<=[。！？\n])")


def _first_meaningful(text: str) -> str:
    """取第一句实质内容，跳过「好的主人～」这种短客套句。"""
    raw = (text or "").strip()
    if not raw:
        return ""
    for part in _SENTENCE_SPLIT.split(raw):
        sentence = part.strip()
        if not sentence:
            continue
        # 短 + 客套开头，两个条件同时满足才跳过，避免误伤「嗯，本鱼记下了」
        if len(sentence) < 10 and any(sentence.startswith(word) for word in _FILLER_PREFIXES):
            continue
        return sentence
    return raw[:80]


def _rule_summary(answer: str) -> str:
    sentence = _first_meaningful(answer)
    return sentence[:160] + "…" if len(sentence) > 160 else sentence


def _refine_summary(turn_id: int, question: str, answer: str) -> None:
    """长回答的摘要交给模型精炼一次；失败就保留规则摘要。"""
    try:
        from app.llm.client import chat

        text = chat(
            f"上一轮问答：用户问「{question}」。回答是：\n{answer[:800]}\n\n"
            "用一句话概括这轮在聊什么、结论是什么。30 字以内，不要客套，不要复述原话。",
            max_tokens=60,
            purpose="summary",
        )
        if not text or not text.strip():
            return
        conn = connect()
        _ensure(conn)
        try:
            conn.execute(
                "UPDATE turn_summaries SET summary = ? WHERE id = ?",
                (text.strip()[:160], turn_id),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def record_turn(chat_id: str | None, question: str, answer: str) -> None:
    """回答发出后调用：存一行「问题 + 摘要」，最多留 20 行。"""
    ask = (question or "").strip()
    reply = (answer or "").strip()
    if not chat_id or not ask or not reply:
        return
    short_question = ask[:60] + ("…" if len(ask) > 60 else "")
    summary = _rule_summary(reply)
    conn = connect()
    _ensure(conn)
    try:
        cursor = conn.execute(
            "INSERT INTO turn_summaries (chat_id, question, summary, created_at) VALUES (?,?,?,?)",
            (chat_id, short_question, summary, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )
        turn_id = cursor.lastrowid
        conn.execute(
            "DELETE FROM turn_summaries WHERE chat_id = ? AND id NOT IN "
            "(SELECT id FROM turn_summaries WHERE chat_id = ? ORDER BY id DESC LIMIT 20)",
            (chat_id, chat_id),
        )
        conn.commit()
    finally:
        conn.close()
    if len(reply) > 200:
        threading.Thread(
            target=_refine_summary, args=(turn_id, short_question, reply), daemon=True
        ).start()


def recent_turns_block(chat_id: str | None, rounds: int = 2, within_minutes: int = 30) -> str:
    """给模型看的「最近聊过」两行，放在提示词末尾。

    只取 30 分钟内的：隔太久就不是同一个话题了，接上去反而串味。
    """
    if not chat_id:
        return ""
    cutoff = (datetime.now() - timedelta(minutes=within_minutes)).strftime("%Y-%m-%d %H:%M:%S")
    conn = connect()
    _ensure(conn)
    try:
        rows = conn.execute(
            "SELECT question, summary FROM turn_summaries WHERE chat_id = ? AND created_at >= ? "
            "ORDER BY id DESC LIMIT ?",
            (chat_id, cutoff, rounds),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return ""
    lines = ["", "【最近聊过】"]
    for question, summary in reversed(rows):
        lines.append(f"问「{question}」→ 答「{summary}」")
    return "\n".join(lines)
