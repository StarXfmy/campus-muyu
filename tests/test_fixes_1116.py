"""2026-11-15 录屏暴露的五个问题的回归验证。

跑法：py -3 tests/test_fixes_1116.py
自测数据写在 __selftest_1116__ 这个 chat_id 下，跑完按这个 key 删干净，不碰真实会话。
第 4 组会真的调三次模型，跑一次约几秒、花一点额度。
"""
import io
import pathlib
import sqlite3
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, str(ROOT))

from app.agent import context, handlers, router, suggestions  # noqa: E402
from app.http_bridge import _trim_group_reply  # noqa: E402
from app.tools import memory  # noqa: E402

DB = str(ROOT / "data" / "campus.db")
CID = "__selftest_1116__"
ok = fail = 0


def check(name, got, want):
    global ok, fail
    if got == want:
        ok += 1
        print("  PASS %-34s %s" % (name, got))
    else:
        fail += 1
        print("  FAIL %-34s got=%r want=%r" % (name, got, want))


print("=== 1 路由：校园卡开销不再被余额抢走 ===")
for s, want in [
    ("我这周校园卡开销如何", "consumption"),
    ("校园卡开销", "consumption"),
    ("校园卡余额还剩多少", "balance"),
    ("卡里还有多少钱", "balance"),
    ("这个月花了多少", "consumption"),
    ("我的消费记录", "consumption"),
    ("这周吃饭花了多少", "consumption"),
]:
    check(s, router.detect(s, CID).name, want)

print()
print("=== 2 校园卡补办类仍走题库（不能掉进失物招领）===")
for s in ["校园卡丢了怎么办", "校园卡怎么补办", "饭卡消磁了"]:
    it = router.detect(s, CID)
    check(s + " -> faq", handlers._faq_worthy(s, it), True)

print()
print("=== 3 相对时间解析 ===")
for s, want in [
    ("30秒后提醒我洗衣服", True),
    ("30s后提醒我洗衣服", True),
    ("5分钟后提醒我喝水", True),
    ("半小时后提醒我", True),
    ("两小时后叫我", True),
    ("明天早上8点提醒我交作业", False),
    ("后天提醒我", False),
    ("这周花了多少", False),
]:
    check(s, memory.parse_relative_at(s) is not None, want)

print()
print("=== 4 提醒端到端：连跑三次「30秒后」，不能有一次判过期 ===")
for i in range(3):
    r = handlers.handle_remind("30秒后提醒我洗衣服", CID)
    bad = "已经过了" in r or "没听清" in r or "没解析" in r
    check("第%d次 未判过期" % (i + 1), bad, False)
    print("      ->", r.replace("\n", " / ")[:70])
    time.sleep(1)

print()
print("=== 5 remember 去重：同一句写两次只留一轮 ===")
conn = sqlite3.connect(DB)
conn.execute("DELETE FROM conversations WHERE chat_id=?", (CID,))
conn.execute("DELETE FROM turn_summaries WHERE chat_id=?", (CID,))
conn.commit()
context.remember(CID, "测试去重这句话", "agent", "回答一")
context.remember(CID, "测试去重这句话", "wechat", "回答一")
rows = conn.execute(
    "SELECT role,intent FROM conversations WHERE chat_id=? ORDER BY id", (CID,)
).fetchall()
check("行数（应为 2 不是 4）", len(rows), 2)
check("保留的是先写的那份", [r[1] for r in rows], ["agent", "agent"])

print()
print("=== 6 清空上下文 ===")
context.remember(CID, "第二句", "agent", "回答二")
before = conn.execute("SELECT COUNT(*) FROM conversations WHERE chat_id=?", (CID,)).fetchone()[0]
context.record_turn(CID, "第二句", "回答二")
msg = handlers.handle_clear_context(CID)
after = conn.execute("SELECT COUNT(*) FROM conversations WHERE chat_id=?", (CID,)).fetchone()[0]
turns = conn.execute("SELECT COUNT(*) FROM turn_summaries WHERE chat_id=?", (CID,)).fetchone()[0]
check("清前 >0", before > 0, True)
check("清后 conversations=0", after, 0)
check("清后 turn_summaries=0", turns, 0)
check("回复里报了条数", ("对话历史删了" in msg) and ("长期记忆" in msg), True)
print("      ->", msg.replace("\n", " / ")[:100])

print()
print("=== 7 指令识别：只认短句，别把「清空购物车」当指令 ===")
for s, want in [
    ("清空上下文", True),
    ("修改木鱼：清空上下文", True),
    ("清掉刚才聊的", True),
    ("忘掉之前说的历史", True),
    ("把聊天记录清掉", True),
    ("明天有啥课", False),
    ("清空购物车", False),
    ("修改木鱼：忘掉 3-1005", False),
    ("删掉这条记忆", False),
    ("忘掉刚才那条", False),
    ("我刚才说的那句话你还记得吗，别清空了先讲讲", False),
]:
    check(repr(s), handlers.is_clear_context(s), want)

print()
print("=== 8 群聊字数：单段压 70，清单豁免 ===")
long_one = "在的，本鱼在。" + "这句是凑长度的说明文字用来验证截断逻辑能不能正常工作。" * 3
r1 = _trim_group_reply(long_one)
check("单段被压到 70 以内", len(r1) <= 70, True)
check("截在句末标点", r1.endswith(("。", "！", "？", "；", "，", "…")), True)
list_reply = "绍兴校区活动 5 个：\n\n活动一\n2026-11-18 13:30\n\n活动二\n2026-11-19 15:00"
check("清单不截", _trim_group_reply(list_reply), list_reply)
check("短回复不动", _trim_group_reply("在的，本鱼在。"), "在的，本鱼在。")

print()
print("=== 9 天气：重试与冷却参数 ===")
from app.tools import weather as w  # noqa: E402

check("冷却 15 秒", w._FAIL_COOLDOWN, 15)
check("超时仍是 8 秒", w._TIMEOUT, 8)
src = open(w.__file__, encoding="utf-8").read()
check("fetch 里有一次重试", src.count("_parse(_fetch(url), name)") >= 2, True)
real = w.fetch({"name": "绍兴", "lat": 30.0, "lon": 120.5, "local": True, "unknown": False})
check("本机真拉一次能成", real.get("ok"), True)
check("预报里有 week", bool(real.get("week")), True)
if real.get("week"):
    print("      -> 明天：", real["week"][1] if len(real["week"]) > 1 else real["week"][0])

print()
print("=== 10 二次修复：裸「饭卡」不再抢消费，「如何/怎么样」不再抢题库 ===")
for s, want in [
    ("上周饭卡消费", "consumption"),
    ("我上周校园卡开销如何", "consumption"),
    ("饭卡还剩多少", "balance"),
    ("一卡通余额", "balance"),
]:
    check(s, router.detect(s, CID).name, want)
for s, want in [
    ("上周校园卡开销如何", False),
    ("上周校园卡开销怎么样", False),
    ("校园卡丢了怎么办", True),
    ("饭卡怎么充值", True),
    ("快递在哪里取", True),
]:
    check(s + " 走题库", handlers._faq_worthy(s, router.detect(s, CID)), want)

print()
print("=== 11 内容兜底 + 微信端提醒入口摘除 ===")
import re as _re  # noqa: E402

_STRIP = _re.compile(r"(提醒我|提醒一下|叫我一声|叫我|记得提醒|别忘了提醒|通知我|提醒)")
for s, want in [
    ("30秒后提醒我洗衣服", "洗衣服"),
    ("下周三提醒我交实验报告", "交实验报告"),
    ("后天下午两点叫我交表", "交表"),
    ("晚上8点半提醒我取快递", "取快递"),
    ("明天早上八点提醒我交作业", "交作业"),
    ("11月20日提醒我交报告", "交报告"),
    ("交高数作业", "交高数作业"),  # 没有时间词时不该动
]:
    got = _STRIP.sub("", memory.strip_time_phrase(s)).strip(" ，,。！!")
    check("剥成事项 " + repr(s), got, want)

from app import http_bridge as hb  # noqa: E402

for s, want in [
    ("30秒后提醒我洗衣服", True),
    ("我的提醒有哪些", True),
    ("取消提醒 104", True),
    ("修改提醒 104 改成明天", True),
    ("上周消费", False),
    ("明天有什么课", False),
    ("快递在哪里取", False),
]:
    blocked = router.detect(s, CID).name in hb._REMIND_INTENTS
    check("%s 被微信端拦下" % s, blocked, want)
check("飞书侧 handle_remind 仍在", callable(handlers.handle_remind), True)

print()
print("=== 12 微信端追问里不再推荐「设个提醒」 ===")
for src, want in [
    ("答案。\n\n—— 你可能还想问：\n· 明天有啥课\n· 设个提醒\n· 这周有啥活动",
     "答案。\n\n—— 你可能还想问：\n· 明天有啥课\n· 这周有啥活动"),
    ("答案。\n\n—— 你可能还想问：\n· 设个提醒\n· 我的提醒有哪些", "答案。"),
    ("答案。\n\n—— 你可能还想问：\n· 明天有啥课\n· 校园卡余额",
     "答案。\n\n—— 你可能还想问：\n· 明天有啥课\n· 校园卡余额"),
    ("没有追问块", "没有追问块"),
]:
    check("剥掉提醒类追问", hb._drop_remind_hints(src), want)
check("飞书侧 suggestions 未受影响",
      "我的提醒有哪些" in "".join(suggestions.build("daily_report_set")), True)

print()
print("=== 清理自测数据 ===")
for t in ("conversations", "turn_summaries", "reminders"):
    n = conn.execute("DELETE FROM %s WHERE chat_id=?" % t, (CID,)).rowcount
    print("  %-16s 删了 %d 行" % (t, n))
conn.commit()
left = {t: conn.execute("SELECT COUNT(*) FROM %s WHERE chat_id=?" % t, (CID,)).fetchone()[0]
        for t in ("conversations", "turn_summaries", "reminders")}
check("三表自测数据归零", sum(left.values()), 0)
conn.close()

print()
print("结果：%d 通过，%d 失败" % (ok, fail))
sys.exit(1 if fail else 0)
