"""提醒调度：后台线程每 30 秒检查一次到期提醒并推送。

只有 Bot 进程在跑时才工作——长连接没有云端托管，关了黑窗口就不推。
启动时会补推一次错过的提醒，标「迟到了」。
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from typing import Callable

from app.agent import handlers
from app.feishu import cards
from app.tools import homework, push, reminders, subscriptions

logger = logging.getLogger("campus-bot.scheduler")

CHECK_INTERVAL = 30  # 秒
LATE_SECONDS = 300  # 超过 5 分钟算迟到
_last_backup = {"day": ""}
_last_expense = {"day": ""}
_last_canteen = {"at": 0.0}


def _memory_target(item: dict) -> str:
    """记忆提醒发到飞书主人：优先早报订阅的会话。"""
    try:
        from app.tools import subscriptions

        conn = subscriptions.connect()
        subscriptions._ensure(conn)
        row = conn.execute(
            "SELECT chat_id FROM daily_reports WHERE enabled = 1 ORDER BY chat_id LIMIT 1"
        ).fetchone()
        conn.close()
        if row:
            return str(row[0])
    except Exception:
        pass
    chat = str(item.get("chat_id") or "")
    return chat if chat.startswith(("oc_", "ou_")) else ""


def _reminder_text(item: dict, now: datetime | None = None) -> str:
    now = now or datetime.now()
    base = f"⏰ 提醒\n{item['content']}\n"
    try:
        when = datetime.strptime(item["remind_at"], "%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return base + f"（设定时间 {item['remind_at']}）"
    if (now - when).total_seconds() > LATE_SECONDS:
        return base + f"（设定时间 {item['remind_at']}，当时机器人没开着，补一条）"
    return base + f"（设定时间 {item['remind_at']}）"


def catch_up(send_fn: Callable[..., None]) -> int:
    """启动时补推已经过点但没发出去的提醒。返回补推条数。"""
    sent = 0
    for item in reminders.due():
        try:
            send_fn(item["chat_id"], _reminder_text(item))
            reminders.mark_sent(item["id"])
            sent += 1
            logger.info("补推提醒 #%s", item["id"])
        except Exception:
            logger.exception("补推失败 #%s", item["id"])
    if sent:
        logger.info("开机补推完成，共 %s 条", sent)
    return sent


def start(send_fn: Callable[..., None], interval: int = CHECK_INTERVAL) -> threading.Thread:
    from app.ops import backup

    # 启动先备一次：上次关机到这次开机之间写的东西，现在才第一次有机会落备份
    backup.run()
    _last_backup["day"] = datetime.now().strftime("%Y-%m-%d")

    catch_up(send_fn)
    thread = threading.Thread(target=_loop, args=(send_fn, interval), daemon=True, name="reminder-scheduler")
    thread.start()
    logger.info("提醒调度已启动（每 %s 秒检查一次）", interval)
    return thread


_audit_state = {"date": ""}


def _maybe_send_rule_audit(send_fn: Callable[..., None]) -> None:
    """每天跑一次：同一问法被纠正 3 次以上，发给主人确认要不要进规则。"""
    today = datetime.now().strftime("%Y-%m-%d")
    if _audit_state["date"] == today:
        return
    _audit_state["date"] = today
    try:
        from app.tools import intent_logs

        items = intent_logs.pending_audit(threshold=3)
        if not items:
            return
        lines = [f"【规则审核】有 {len(items)} 条同类纠正待确认。", ""]
        for index, item in enumerate(items, 1):
            lines.append(f"{index}. 问「{item['question']}」被纠正 {item['cnt']} 次")
            lines.append(f"   纠正说明：{item['correction']}")
        lines += ["", "回复「确认1」记住这条纠正，回复「忽略1」丢弃。"]
        text = "\n".join(lines)

        from app.feishu import cards

        conn = intent_logs.connect()
        try:
            row = conn.execute(
                "SELECT chat_id FROM daily_reports WHERE enabled = 1 LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return
        send_fn(row["chat_id"], text, cards.build(text, "help"))
    except Exception:
        logger.exception("规则审核检查失败")


def _canteen_sync() -> None:
    """拉全量食堂数据落库。30MB 的活，放独立线程，别拖慢提醒。"""
    try:
        from app.tools import canteen

        added = canteen.sync()
        logger.info("食堂数据同步完成：新增 %s 条", added)
    except Exception:
        logger.exception("食堂数据同步失败")


def _loop(send_fn: Callable[..., None], interval: int) -> None:
    while True:
        now = datetime.now()
        try:
            for item in reminders.due():
                text = _reminder_text(item)
                try:
                    send_fn(item["chat_id"], text)
                    reminders.mark_sent(item["id"])
                    logger.info("已推送提醒 #%s", item["id"])
                except Exception:
                    logger.exception("提醒推送失败 #%s", item["id"])

            # 带时间的记忆：到点在飞书提醒一次，并问要不要删
            try:
                from app.tools import memory

                for item in memory.due_reminders(now):
                    target = _memory_target(item)
                    if not target:
                        logger.warning("记忆提醒没有飞书会话可发 #%s", item["id"])
                        continue
                    text = (
                        f"⏰ 记忆到点：{item['content']}\n"
                        f"（设在 {item['remind_at']}）\n"
                        "这条还要留着吗？要删就说「修改木鱼：忘掉 关键词」"
                    )
                    try:
                        send_fn(target, text)
                        memory.mark_reminded(item["id"])
                        logger.info("已推送记忆提醒 #%s", item["id"])
                    except Exception:
                        logger.exception("记忆提醒推送失败 #%s", item["id"])
            except Exception:
                logger.exception("记忆提醒检查失败")

            # 每天备份一次；同一天内重启不会重复备（schedule 线程常驻）
            today = now.strftime("%Y-%m-%d")
            if _last_backup["day"] != today:
                from app.ops import backup

                backup.run()
                _last_backup["day"] = today

            # 每天 19:00 拉一次校园卡流水落库：接口只给最近若干笔，
            # 不主动存的话，早几天的消费过一阵就查不到了
            if now.hour >= 19 and _last_expense["day"] != today:
                try:
                    added = handlers.sync_consumption()
                    _last_expense["day"] = today
                    logger.info("消费流水快照完成，新增 %s 条", added)
                except Exception:
                    logger.exception("消费流水快照失败")

            # 食堂数据每 6 小时同步一次：新店新菜记录，老条目只更新价格和时间
            if time.time() - _last_canteen["at"] >= 6 * 3600:
                _last_canteen["at"] = time.time()
                threading.Thread(target=_canteen_sync, daemon=True, name="canteen-sync").start()

            # 作业 DDL 提醒：用户自己录的截止时间，归「自建提醒」一类，
            # 不受主动推送总开关限制（控制开关的是早报/每日一敲那种他没要求的消息）
            for kind, item in homework.due_for_notice(now):
                who = f"{item['course']} · " if item["course"] else ""
                when = item["due_at"][5:16]
                label = "不到 2 小时" if kind == "soon" else "还有不到 1 天"
                text = f"⏰ 作业快截止（{label}）\n{who}{item['content']}\n截止 {when}"
                try:
                    send_fn(item["chat_id"], text)
                    homework.mark_notified(item["id"], kind)
                    logger.info("已推送作业提醒 #%s(%s)", item["id"], kind)
                except Exception:
                    logger.exception("作业提醒推送失败 #%s", item["id"])

            # 二课新活动：用户明确要的，不受「每天一条」限制，自带 10 分钟节流
            from app.tools import activity_watch

            for chat_id, text in activity_watch.run_once(now):
                try:
                    send_fn(chat_id, text)
                    logger.info("已推送二课新活动 → %s", chat_id)
                except Exception:
                    logger.exception("二课推送失败 %s", chat_id)

            for sub in subscriptions.due(now):
                chat_id = sub["chat_id"]
                # 主动推送总开关 + 一天最多一条；用户自建的提醒不受这里限制
                if not push.allow_today(chat_id, now):
                    continue
                try:
                    text = handlers.build_morning_report(chat_id)
                    send_fn(chat_id, text, cards.build(text, "daily_report_set"))
                    part = handlers.morning_news_card(chat_id)
                    if part:
                        news_card, news_fallback = part
                        send_fn(chat_id, news_fallback, news_card)
                    subscriptions.mark_sent(chat_id, now.strftime("%Y-%m-%d"))
                    push.mark_pushed(chat_id, now)
                    logger.info("已推送早报 %s", chat_id)
                except Exception:
                    logger.exception("早报推送失败 %s", chat_id)
        except Exception:
            logger.exception("调度循环异常")
        _maybe_send_rule_audit(send_fn)
        time.sleep(interval)
