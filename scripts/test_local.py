"""本地测试：不依赖飞书，直接调意图路由和工具，验证真实数据。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.console import setup  # noqa: E402

setup()

from app.agent import handlers, router  # noqa: E402

CASES = [
    "你好",
    "今天有啥课",
    "明天有什么课",
    "这周有啥课",
    "下周有啥课",
    "周三有啥课",
    "这学期有哪些课",
    "校园卡还有多少钱",
    "3 楼有空教室吗",
    "正心楼4楼空教室",
    "正心楼二楼有空教室吗",
    "帮我找个自习的地方",
    "这周有啥活动值得去",
    "周三的AI讲座我能去吗",
    "明天怎么安排",
    "我报名了什么活动",
    "明天早上8点提醒我交作业",
    "我的提醒有哪些",
    "今天天气怎么样",
    "教务处办公地点在哪",
    "奖学金申请要什么材料",
    "我绩点多少",
    "这周和下周哪周课多",
    "我这个月消费多少",
    "图书馆还有座位吗",
    "阳光长跑还差几次",
    "宿舍电费还有多少",
    "最近有什么失物",
    "帮我做个期末复习计划",
    "今日速览",
    "后天的课表",
    "取消提醒 1",
    "每天7点半发早报",
    "关掉早报",
    "课表图片",
    "后天的新闻",
    "记住我住 3-1005",
    "你记得什么",
    "关闭推送",
    "考试安排出来了吗",
    "现在去哪自习合适",
    "我有哪些作业",
    "这学期总结一下",
    "成绩单怎么开",
    "我要报名会计学院职业规划大赛",
    "第3教研楼4楼空教室",
    # 消费图表：规则要抢在消费分析前面
    "消费图表",
    "我的消费图表在哪",
    # 学期报告：库里有数据 / 换个说法
    "学期报告",
    "这学期我过得怎么样",
    # FAQ：有原文的条目 + 仍是 [待补] 的条目
    "转专业要什么条件",
    "助学贷款怎么申请",
    "宿舍灯坏了怎么报修",
    # 学院信息：指定学院（列表类）/ 指定学院（内容类）/ 不指定（必须只提示不猜）
    "电子信息学院有啥通知",
    "计算机学院有哪些专业",
    "学院有啥通知",
]


def _check_push_default() -> int:
    """push_settings 没有记录时默认必须是「关」：用户没显式开过，就不能主动推。"""
    from app.tools import push

    chat = "test_default_push"
    conn = push.connect()
    conn.execute("DELETE FROM push_settings WHERE chat_id = ?", (chat,))
    conn.commit()
    conn.close()

    enabled = push.is_enabled(chat)
    allowed = push.allow_today(chat)
    print(f"\n{'=' * 50}")
    print("默认推送开关（无记录时）")
    print(f"is_enabled={enabled}  allow_today={allowed}  期望都是 False")
    ok = enabled is False and allowed is False

    conn = push.connect()
    conn.execute("DELETE FROM push_settings WHERE chat_id = ?", (chat,))
    conn.commit()
    conn.close()
    return 0 if ok else 1


def _check_reminder_not_gated() -> int:
    """边界：默认关推送时，用户自己设的到期提醒仍然要能推出来。

    防以后有人把推送开关误套到提醒路径上——提醒是用户明确要求的时间点，
    不属于「主动推送」。
    """
    from datetime import datetime, timedelta

    from app.feishu import scheduler
    from app.tools import push, reminders

    chat = "test_reminder_not_gated"
    conn = push.connect()
    conn.execute("DELETE FROM push_settings WHERE chat_id = ?", (chat,))
    conn.commit()
    conn.close()

    # 有真实待发提醒时跳过，避免把它误标成已发
    if [item for item in reminders.due() if item["chat_id"] != chat]:
        print(f"\n{'=' * 50}")
        print("提醒不受推送开关影响：跳过（库里有真实待发提醒）")
        return 0

    reminders.add(chat, "边界测试", datetime.now() - timedelta(minutes=10))
    sent: list[str] = []
    scheduler.catch_up(lambda cid, msg, card=None: sent.append(cid))
    ok = chat in sent

    conn = reminders.connect()
    conn.execute("DELETE FROM reminders WHERE chat_id = ?", (chat,))
    conn.commit()
    conn.close()
    conn = push.connect()
    conn.execute("DELETE FROM push_settings WHERE chat_id = ?", (chat,))
    conn.commit()
    conn.close()

    print(f"\n{'=' * 50}")
    print("提醒不受推送开关影响（默认关推送时）")
    print(f"到期提醒被推送={ok}  期望 True")
    return 0 if ok else 1


TEST_CHAT = "test_redline"


def _clean_expense(chat: str) -> None:
    from app.tools import records

    conn = records.connect()
    records._ensure(conn)
    conn.execute("DELETE FROM expense_log WHERE chat_id = ?", (chat,))
    conn.commit()
    conn.close()


def _check_semester_report_empty_expense() -> int:
    """红线：expense_log 为空时，学期报告必须写「暂无数据」，且不能产出图表 PNG。

    防以后有人给空数据补一句「本鱼估算你大约花了…」——那是编数据。
    """
    from app.config import DATA_DIR
    from app.tools import chart_image

    _clean_expense(TEST_CHAT)
    png = DATA_DIR / "expense_chart.png"
    before = png.stat().st_mtime_ns if png.exists() else None

    text = handlers.handle_semester_report(TEST_CHAT)
    path = chart_image.render_expense_from_log(TEST_CHAT)

    after = png.stat().st_mtime_ns if png.exists() else None
    has_marker = "暂无数据" in text
    no_png = path is None and before == after

    _clean_expense(TEST_CHAT)

    print(f"\n{'=' * 50}")
    print("红线：学期报告在无消费数据时")
    print(f"含「暂无数据」={has_marker}  期望 True")
    print(f"未生成图表 PNG={no_png}  期望 True")
    if not has_marker:
        print(f"[实际回复]\n{text}")
    return 0 if has_marker and no_png else 1


def _check_faq_pending_marker() -> int:
    """红线：命中 [待补] 条目的回答必须保留 [待补]，不许被改写成确定说法。

    题库里带 [待补] 的就是「没查到官网原文」，任何一层改写成肯定的建议都算造假。
    """
    from app.tools import faq

    pending = [item for item in faq.ITEMS if "[待补]" in item["answer"]]
    queries = {
        "休学 / 复学 / 退学": "休学要办什么手续",
        "医保 / 报销": "医保怎么报销",
        "一卡通 / 校园卡补办": "一卡通丢了怎么补办",
        "奖学金 / 助学金": "奖学金怎么评",
        "助学贷款": "助学贷款怎么申请",
    }

    print(f"\n{'=' * 50}")
    print(f"红线：[待补] 条目共 {len(pending)} 条，逐条走完整回答链路")

    failures = 0
    for item in pending:
        query = queries.get(item["title"])
        if not query:
            print(f"  [跳过] {item['title']}：没有配测试问句")
            continue
        reply = handlers.answer(query, TEST_CHAT)
        kept = "[待补]" in reply
        if not kept:
            failures += 1
        print(f"  {'[OK]  ' if kept else '[FAIL]'} {item['title']} ← 「{query}」")
        if not kept:
            print(f"        实际回复：{reply[:90]}")

    if failures:
        print(f"  失败 {failures} 条：有 [待补] 标记被吞掉或被改写")

    from app.agent import context

    context.clear(TEST_CHAT)
    return 0 if failures == 0 else 1


def _check_college_needs_name() -> int:
    """红线：没说哪个学院时只能提示，不许自己挑一个学院去查。"""
    reply = handlers.answer("学院有啥通知", TEST_CHAT)
    asked = "哪个学院" in reply
    # 学院列表里只有名字；出现 /list.htm 说明真去抓了某个学院
    leaked = "/list.htm" in reply or "/2026/" in reply

    print(f"\n{'=' * 50}")
    print("红线：没指明学院时不擅自查询")
    print(f"提示「哪个学院」={asked}  期望 True")
    print(f"未擅自抓取页面={not leaked}  期望 True")
    if leaked:
        print(f"[实际回复]\n{reply[:200]}")

    from app.agent import context

    context.clear(TEST_CHAT)
    return 0 if asked and not leaked else 1


def main() -> int:
    failures = 0
    failures += _check_push_default()
    failures += _check_reminder_not_gated()
    failures += _check_semester_report_empty_expense()
    failures += _check_faq_pending_marker()
    failures += _check_college_needs_name()
    for text in CASES:
        intent = router.detect(text)
        print(f"\n{'=' * 50}")
        print(f"输入：{text}")
        print(f"意图：{intent.name}" + (f"（楼栋 {intent.building}）" if intent.building else ""))
        print(f"{'-' * 50}")
        try:
            reply = handlers.answer(text)
        except Exception as exc:
            failures += 1
            print(f"[异常] {type(exc).__name__}: {exc}")
            continue
        print(reply)
    print(f"\n{'=' * 50}")
    print(f"完成，共 {len(CASES)} 条，异常 {failures} 条")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
