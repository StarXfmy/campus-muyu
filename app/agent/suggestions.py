"""猜你想问：按「刚答的意图 + 当前时段」查表给 3 条追问，不调模型。"""
from __future__ import annotations

from datetime import datetime

_BUCKETS = (
    (6, 11, "morning"),
    (11, 14, "noon"),
    (14, 18, "afternoon"),
    (18, 23, "evening"),
)

# 刚答完什么，接着最可能问什么
_BY_INTENT: dict[str, tuple[str, ...]] = {
    "schedule": ("明天有啥课", "这周还有几天课", "有空教室吗"),
    "schedule_week": ("明天有啥课", "这周有啥活动", "图书馆有座位吗"),
    "schedule_next_week": ("这周有啥课", "这学期有哪些课", "今天有啥课"),
    "schedule_weekday": ("这周完整课表", "明天有啥课", "有没有空教室"),
    "schedule_all": ("这周有啥课", "帮我做个复习计划", "这周和下周哪周课多"),
    "balance": ("我这个月花了多少", "宿舍电费还有多少", "今天有啥课"),
    "empty_room": ("图书馆有座位吗", "明天有啥课", "今天天气怎么样"),
    "weather": ("今天有啥课", "今天怎么安排", "宿舍电费还有多少"),
    "activities": ("这周有啥活动值得去", "周三的讲座我能去吗", "我要报名活动"),
    "activity_advice": ("这周有啥活动", "周三的讲座我能去吗", "我报名了什么活动"),
    "my_activities": ("这周有啥活动", "我要报名活动", "校园卡余额"),
    "activity_conflict": ("这周有啥活动值得去", "明天有啥课", "帮我做个复习计划"),
    "sunrun": ("这周有啥活动", "图书馆有座位吗", "今天天气怎么样"),
    "library": ("有空教室吗", "这周有啥课", "今天天气怎么样"),
    "electric": ("校园卡余额", "我这个月花了多少", "今天有啥课"),
    "consumption": ("校园卡余额", "宿舍电费还有多少", "这周有啥活动"),
    "lostfound": ("校园卡余额", "这周有啥活动", "今天有啥课"),
    "mail": ("今天有啥课", "这周有啥活动", "校园卡余额"),
    "plan_day": ("明天有啥课", "有空教室吗", "今天天气怎么样"),
    "daily_report": ("明天有啥课", "这周有啥活动", "宿舍电费还有多少"),
    "daily_report_set": ("现在发一份早报", "我的提醒有哪些", "今天有啥课"),
    "study_plan": ("这周有啥课", "有空教室吗", "图书馆有座位吗"),
    "compare_week": ("下周有啥课", "这学期有哪些课", "这周有啥活动"),
    "remind": ("我的提醒有哪些", "明天有啥课", "现在发一份早报"),
    "my_reminders": ("明天有啥课", "现在发一份早报", "校园卡余额"),
    "cancel_remind": ("我的提醒有哪些", "明天有啥课", "这周有啥活动"),
    "grade": ("这学期有哪些课", "这周有啥课", "阳光长跑还差几次"),
    "knowledge": ("这周有啥活动", "今天有啥课", "教务处办公地点在哪"),
    "office": ("这周有啥活动", "今天有啥课", "校园卡余额"),
}

# 同一意图在不同时段补的第三条
_BY_TIME: dict[str, tuple[str, ...]] = {
    "morning": ("今天天气怎么样", "今天怎么安排", "现在发一份早报"),
    "noon": ("下午有啥课", "校园卡余额", "图书馆有座位吗"),
    "afternoon": ("晚上去哪自习", "明天有啥课", "宿舍电费还有多少"),
    "evening": ("明天有啥课", "这周有啥活动", "设个明早提醒"),
    "night": ("明天有啥课", "设个提醒", "这周有啥活动"),
}


def _bucket(hour: int) -> str:
    for start, end, name in _BUCKETS:
        if start <= hour < end:
            return name
    return "night"


def build(intent_name: str, when: datetime | None = None, limit: int = 3) -> list[str]:
    when = when or datetime.now()
    picks: list[str] = []

    for item in _BY_INTENT.get(intent_name, ())[: limit - 1]:
        if item not in picks:
            picks.append(item)
    for item in _BY_TIME.get(_bucket(when.hour), ()):
        if len(picks) >= limit:
            break
        if item not in picks:
            picks.append(item)
    if not picks:
        picks = ["今天有啥课", "校园卡余额", "这周有啥活动"][:limit]
    return picks[:limit]


def render(intent_name: str, when: datetime | None = None) -> str:
    items = build(intent_name, when)
    if not items:
        return ""
    body = "\n".join(f"· {item}" for item in items)
    return f"\n\n—— 你可能还想问：\n{body}"
