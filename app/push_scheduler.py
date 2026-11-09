"""微信侧定时推送：每日早报 + 二课新活动监控。

跟飞书的调度器互不干扰：
- 飞书的 chat_id 不带前缀，这条线只处理 wx: 开头的目标
- 内容只写进 outbox，由微信桥接自己来取并发送（它才是能操作微信的那个进程）

启动：http_bridge.py 里作为后台线程拉起。
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from datetime import datetime, timedelta

from app.config import DB_PATH

logger = logging.getLogger("campus-bot.push")

# 微信端主动推送总开关。关掉之后这里不再生成任何东西（早报、二课都不推），
# 定时消息全部由飞书那条线负责——那边的机器 24 小时开着，不受本机开关影响。
WECHAT_PUSH_ENABLED = False

MORNING_AT = (8, 20)      # 早报时间
MORNING_LATEST = (10, 0)  # 超过这个点就不再补发，免得中午收到「早上好」
ACTIVITY_EVERY_MIN = 60   # 二课活动检查间隔
ACTIVITY_ENABLED = False  # 二课新活动改到飞书推
TICK_SECONDS = 15         # 早报准点：15 秒一查，最坏晚 15 秒入队

TARGET = ""  # 主人私聊 wxid；早报和二课新活动都发这里


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS wx_outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            target TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL,
            sent_at TEXT NOT NULL DEFAULT ''
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS wx_activity_seen (
            activity_id TEXT PRIMARY KEY,
            name TEXT NOT NULL DEFAULT '',
            first_seen TEXT NOT NULL
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS wx_push_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL DEFAULT ''
        )"""
    )
    conn.commit()


def _state_get(key: str) -> str:
    conn = connect()
    _ensure(conn)
    try:
        row = conn.execute("SELECT value FROM wx_push_state WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else ""
    finally:
        conn.close()


def _state_set(key: str, value: str) -> None:
    conn = connect()
    _ensure(conn)
    try:
        conn.execute(
            """INSERT INTO wx_push_state (key, value) VALUES (?, ?)
               ON CONFLICT(key) DO UPDATE SET value = ?""",
            (key, value, value),
        )
        conn.commit()
    finally:
        conn.close()


def enqueue(kind: str, target: str, content: str) -> int:
    conn = connect()
    _ensure(conn)
    try:
        cur = conn.execute(
            "INSERT INTO wx_outbox (kind, target, content, created_at) VALUES (?, ?, ?, ?)",
            (kind, target, content, datetime.now().isoformat(timespec="seconds")),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def pending(limit: int = 5) -> list[dict]:
    """只取 24 小时内的。电脑关了几天再开，不该把陈年早报一次性糊出来。"""
    cutoff = (datetime.now() - timedelta(hours=24)).isoformat(timespec="seconds")
    conn = connect()
    _ensure(conn)
    try:
        rows = conn.execute(
            """SELECT id, kind, target, content FROM wx_outbox
               WHERE sent_at = '' AND created_at >= ? ORDER BY id ASC LIMIT ?""",
            (cutoff, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def ack(ids: list[int]) -> int:
    if not ids:
        return 0
    conn = connect()
    _ensure(conn)
    try:
        marks = ",".join("?" for _ in ids)
        cur = conn.execute(
            f"UPDATE wx_outbox SET sent_at = ? WHERE id IN ({marks}) AND sent_at = ''",
            [datetime.now().isoformat(timespec="seconds"), *ids],
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def _is_shaoxing(item: dict) -> bool:
    from app.agent.handlers import _activity_campus

    return _activity_campus(item) == "绍兴"


def _activity_line(item: dict) -> str:
    name = item.get("activityName") or "未命名活动"
    start = (item.get("activityStartTime") or "")[:16]
    place = item.get("position") or ""
    meta = " · ".join(x for x in (start, place) if x)
    return f"· {name}" + (f"\n  {meta}" if meta else "")


def check_activities(now: datetime, first_run_baseline_only: bool = True) -> int:
    """查绍兴活动，有新增就入队。返回新增条数。"""
    from app.tools.hduhelp import HduhelpError, list_activities

    try:
        raw = list_activities(status=8, page_size=30)
    except HduhelpError as exc:
        logger.warning("二课活动查询失败：%s", exc)
        return 0

    items = (raw.get("data") or {}).get("items") or []
    shaoxing = [it for it in items if _is_shaoxing(it)]

    conn = connect()
    _ensure(conn)
    try:
        seen = {r["activity_id"] for r in conn.execute("SELECT activity_id FROM wx_activity_seen")}
        fresh = [it for it in shaoxing if (it.get("activityID") or "") not in seen]
        if not fresh:
            return 0

        stamp = now.isoformat(timespec="seconds")
        conn.executemany(
            "INSERT OR IGNORE INTO wx_activity_seen (activity_id, name, first_seen) VALUES (?, ?, ?)",
            [(it.get("activityID") or "", it.get("activityName") or "", stamp) for it in fresh],
        )
        conn.commit()

        if not seen:
            # 第一次跑只建基线，不然一上来就把现有活动全推一遍
            logger.info("二课基线已建立：绍兴 %d 个", len(shaoxing))
            return 0

        lines = [f"【新二课活动】绍兴校区 {len(fresh)} 个"]
        lines += [_activity_line(it) for it in fresh[:6]]
        if len(fresh) > 6:
            lines.append(f"…还有 {len(fresh) - 6} 个")
        lines.append("\n想看全部就问木鱼「绍兴有什么活动」")
        enqueue("activity", TARGET, "\n".join(lines))
        logger.info("推送新二课活动 %d 个", len(fresh))
        return len(fresh)
    finally:
        conn.close()


def build_morning(now: datetime) -> str:
    from app.agent import handlers

    text = handlers.build_morning_report(TARGET)

    # 列当前还能报的，而不是「新增」：基线是首次运行时建的，
    # 按时间筛会把老活动全算成新的，反而误导
    try:
        conn = connect()
        _ensure(conn)
        rows = conn.execute(
            "SELECT name FROM wx_activity_seen ORDER BY first_seen DESC LIMIT 5"
        ).fetchall()
        conn.close()
        if rows:
            text += "\n\n【绍兴可报名的二课】\n" + "\n".join(f"· {r['name']}" for r in rows)
            text += "\n想看全部就说「绍兴有什么活动」"
    except Exception:
        logger.exception("早报追加活动失败")

    return text


def _run_morning(now: datetime) -> None:
    today = now.strftime("%Y-%m-%d")
    if _state_get("morning_last") == today:
        return
    minutes = now.hour * 60 + now.minute
    if minutes < MORNING_AT[0] * 60 + MORNING_AT[1]:
        return
    if minutes > MORNING_LATEST[0] * 60 + MORNING_LATEST[1]:
        _state_set("morning_last", today)
        return
    try:
        text = build_morning(now)
    except Exception:
        logger.exception("早报生成失败")
        return
    enqueue("morning", TARGET, text)
    _state_set("morning_last", today)
    logger.info("早报已入队")


def _run_activity(now: datetime) -> None:
    last = _state_get("activity_last")
    if last:
        try:
            if (now - datetime.fromisoformat(last)).total_seconds() < ACTIVITY_EVERY_MIN * 60:
                return
        except ValueError:
            pass
    check_activities(now)
    _state_set("activity_last", now.isoformat(timespec="seconds"))


def _loop() -> None:
    while True:
        try:
            now = datetime.now()
            _run_morning(now)
            if ACTIVITY_ENABLED:
                _run_activity(now)
        except Exception:
            logger.exception("微信推送调度异常")
        time.sleep(TICK_SECONDS)


def start(target: str) -> threading.Thread | None:
    global TARGET
    TARGET = target
    if not WECHAT_PUSH_ENABLED:
        print("[push] 微信端主动推送已关闭（早报和二课都由飞书负责）", flush=True)
        return None
    thread = threading.Thread(target=_loop, daemon=True, name="wx-push-scheduler")
    thread.start()
    print(
        f"[push] 已启动：早报 {MORNING_AT[0]:02d}:{MORNING_AT[1]:02d}，"
        f"二课每 {ACTIVITY_EVERY_MIN} 分钟，目标 {target}",
        flush=True,
    )
    return thread
