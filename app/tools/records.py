"""学期报告的数据积累。

消费接口只返回最近 15 笔，不落库的话过几周就查不到历史了；
所以每次查消费顺手把明细存下来（按「时间+金额+店铺」去重）。
其余指标（长跑、电费、余额）是累计值，现查即可，只记快照用于趋势。
"""
from __future__ import annotations

import json
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
        """CREATE TABLE IF NOT EXISTS expense_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT NOT NULL,
            happened_at TEXT NOT NULL,
            day TEXT NOT NULL,
            amount REAL NOT NULL,
            place TEXT NOT NULL DEFAULT '',
            UNIQUE(chat_id, happened_at, amount, place)
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            payload TEXT NOT NULL,
            created_at TEXT NOT NULL
        )"""
    )
    conn.commit()


def log_expenses(chat_id: str, spends: list[dict]) -> int:
    """spends: [{"time": "...", "day": "...", "amount": 12.3, "place": "..."}]"""
    if not chat_id or not spends:
        return 0
    conn = connect()
    _ensure(conn)
    added = 0
    try:
        for item in spends:
            # 去重键不能带 place：店名会被清洗（"14" → 空），同一笔就会被当成两条
            cursor = conn.execute(
                """INSERT INTO expense_log (chat_id, happened_at, day, amount, place)
                   SELECT ?, ?, ?, ?, ?
                   WHERE NOT EXISTS (
                       SELECT 1 FROM expense_log
                       WHERE chat_id = ? AND happened_at = ? AND amount = ?
                   )""",
                (
                    chat_id,
                    str(item.get("time") or ""),
                    str(item.get("day") or ""),
                    float(item.get("amount") or 0),
                    str(item.get("place") or ""),
                    chat_id,
                    str(item.get("time") or ""),
                    float(item.get("amount") or 0),
                ),
            )
            added += cursor.rowcount
        conn.commit()
    finally:
        conn.close()
    return added


def expense_chat_ids() -> list[str]:
    """有消费记录落过库的会话，定时快照按这些会话补数据。"""
    conn = connect()
    _ensure(conn)
    try:
        rows = conn.execute(
            "SELECT DISTINCT chat_id FROM expense_log WHERE chat_id != ''"
        ).fetchall()
        return [str(r["chat_id"]) for r in rows]
    finally:
        conn.close()


def range_summary(chat_id: str, start: str, end: str | None = None) -> dict:
    """区间内消费汇总，和消费图同源，避免文字说 9 笔、图里却画了 8 天。"""
    conn = connect()
    _ensure(conn)
    try:
        sql = "SELECT day, amount, place FROM expense_log WHERE chat_id = ? AND day >= ?"
        params: list = [chat_id, start]
        if end:
            sql += " AND day <= ?"
            params.append(end)
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()

    by_day: dict[str, float] = {}
    place_stat: dict[str, list] = {}
    total = 0.0
    biggest = ("", 0.0)
    for row in rows:
        amount = float(row["amount"] or 0)
        day = str(row["day"] or "")
        place = str(row["place"] or "")
        total += amount
        by_day[day] = by_day.get(day, 0) + amount
        if place:
            stat = place_stat.setdefault(place, [0, 0.0])
            stat[0] += 1
            stat[1] += amount
        if amount > biggest[1]:
            biggest = (place, amount)
    return {
        "count": len(rows),
        "total": total,
        "by_day": sorted(by_day.items(), reverse=True),
        "by_place": sorted(
            [(name, stat[0], stat[1]) for name, stat in place_stat.items()],
            key=lambda x: (-x[1], -x[2]),
        ),
        "biggest": biggest,
    }


def log_snapshot(chat_id: str, kind: str, payload: dict) -> None:
    if not chat_id:
        return
    conn = connect()
    _ensure(conn)
    try:
        conn.execute(
            "INSERT INTO snapshots (chat_id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
            (chat_id, kind, json.dumps(payload, ensure_ascii=False), datetime.now().strftime("%Y-%m-%d %H:%M")),
        )
        conn.commit()
    finally:
        conn.close()


def expense_summary(chat_id: str) -> dict:
    """学期报告用：累计花了多少、几笔、常去哪。"""
    conn = connect()
    _ensure(conn)
    try:
        rows = conn.execute(
            "SELECT day, amount, place FROM expense_log WHERE chat_id = ?", (chat_id,)
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return {"count": 0, "total": 0.0, "top_places": [], "days": 0}

    total = sum(float(r["amount"]) for r in rows)
    by_place: dict[str, float] = {}
    days = set()
    for row in rows:
        if row["place"]:
            by_place[row["place"]] = by_place.get(row["place"], 0) + float(row["amount"])
        if row["day"]:
            days.add(row["day"])
    top = sorted(by_place.items(), key=lambda kv: kv[1], reverse=True)[:5]
    return {"count": len(rows), "total": total, "top_places": top, "days": len(days)}
