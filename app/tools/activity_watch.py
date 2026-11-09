"""二课新活动监控：定时查绍兴校区，有新活动就交给调度器推送。

跟微信那套分开跑：飞书 bot 在云上，用的是云上这份库；微信大脑在本机，
两边各自记基线。活动推送不受「每天最多一条」的主动推送总开关限制——
是用户明确要求的，跟提醒同一类。
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime

from app.config import DB_PATH

logger = logging.getLogger("campus-bot.activity")

CHECK_EVERY_MIN = 10
CAMPUS = "绍兴"
MAX_LISTED = 6


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS activity_seen (
            activity_id TEXT PRIMARY KEY,
            name TEXT NOT NULL DEFAULT '',
            first_seen TEXT NOT NULL
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS activity_watch_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL DEFAULT ''
        )"""
    )
    conn.commit()


def _state_get(key: str) -> str:
    conn = connect()
    _ensure(conn)
    try:
        row = conn.execute("SELECT value FROM activity_watch_state WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else ""
    finally:
        conn.close()


def _state_set(key: str, value: str) -> None:
    conn = connect()
    _ensure(conn)
    try:
        conn.execute(
            """INSERT INTO activity_watch_state (key, value) VALUES (?, ?)
               ON CONFLICT(key) DO UPDATE SET value = ?""",
            (key, value, value),
        )
        conn.commit()
    finally:
        conn.close()


def targets() -> list[str]:
    """开过主动推送的会话。用户没开过就不推，不替人做主。"""
    conn = connect()
    _ensure(conn)
    try:
        rows = conn.execute("SELECT chat_id FROM push_settings WHERE enabled = 1").fetchall()
        return [r["chat_id"] for r in rows]
    finally:
        conn.close()


def _is_campus(item: dict) -> bool:
    from app.agent.handlers import _activity_campus

    return _activity_campus(item) == CAMPUS


def _line(item: dict) -> str:
    name = item.get("activityName") or "未命名活动"
    start = (item.get("activityStartTime") or "")[:16]
    place = item.get("position") or ""
    meta = " · ".join(x for x in (start, place) if x)
    apply_end = (item.get("applyEndTime") or "")[:16]
    tail = f"\n  报名截止 {apply_end}" if apply_end and str(item.get("isNeedApply") or "") == "1" else ""
    return f"· {name}" + (f"\n  {meta}" if meta else "") + tail


def _closed(item: dict, now: datetime) -> bool:
    """报名已截止的活动不再推——推了也报不上，只会添乱。"""
    if str(item.get("isNeedApply") or "") != "1":
        return False
    end = str(item.get("applyEndTime") or "").strip()
    if not end:
        return False
    try:
        return datetime.strptime(end[:16], "%Y-%m-%d %H:%M") <= now
    except ValueError:
        return False


def run_once(now: datetime | None = None) -> list[tuple[str, str]]:
    """到点就查一次。返回 [(chat_id, 要发的文本)]；没新增或没到点返回空。"""
    now = now or datetime.now()

    last = _state_get("last_check")
    if last:
        try:
            if (now - datetime.fromisoformat(last)).total_seconds() < CHECK_EVERY_MIN * 60:
                return []
        except ValueError:
            pass

    from app.tools.hduhelp import HduhelpError, list_activities

    try:
        raw = list_activities(status=8, page_size=30)
    except HduhelpError as exc:
        logger.warning("二课活动查询失败：%s", exc)
        _state_set("last_check", now.isoformat(timespec="seconds"))
        return []

    items = (raw.get("data") or {}).get("items") or []
    picked = [it for it in items if _is_campus(it) and not _closed(it, now)]

    conn = connect()
    _ensure(conn)
    try:
        seen = {r["activity_id"] for r in conn.execute("SELECT activity_id FROM activity_seen")}
        fresh = [it for it in picked if (it.get("activityID") or "") not in seen]
        if fresh:
            stamp = now.isoformat(timespec="seconds")
            conn.executemany(
                "INSERT OR IGNORE INTO activity_seen (activity_id, name, first_seen) VALUES (?, ?, ?)",
                [(it.get("activityID") or "", it.get("activityName") or "", stamp) for it in fresh],
            )
            conn.commit()
    finally:
        conn.close()

    _state_set("last_check", now.isoformat(timespec="seconds"))

    if not seen:
        logger.info("二课基线已建立：%s %d 个", CAMPUS, len(picked))
        return []
    if not fresh:
        return []

    listed = fresh[:MAX_LISTED]
    lines = [f"【新二课活动】{CAMPUS}校区 {len(fresh)} 个"]
    lines += [_line(it) for it in listed]
    if len(fresh) > MAX_LISTED:
        lines.append(f"…还有 {len(fresh) - MAX_LISTED} 个")
    lines.append("\n想报哪个直接说，或者问「绍兴有什么活动」看全部")
    text = "\n".join(lines)

    chats = targets()
    try:
        from app.agent import handlers as _handlers

        for chat_id in chats:
            _handlers.remember_activity_list(chat_id, listed)
    except Exception:
        pass
    logger.info("推送二课新活动 %d 个", len(fresh))
    return [(chat_id, text) for chat_id in chats]
