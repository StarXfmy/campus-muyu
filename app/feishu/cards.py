"""飞书交互卡片：把纯文本回复包装成带标题、配色、分隔线的卡片。

卡片结构用飞书 open API 的 interactive 消息格式（v1）。
内容统一走 lark_md，能渲染 **粗体** 和 • 列表。
"""
from __future__ import annotations

from typing import Any

# 意图 → (标题, 顶部配色)
_STYLE = {
    "schedule": ("今日课表", "blue"),
    "schedule_week": ("本周课表", "blue"),
    "schedule_next_week": ("下周课表", "blue"),
    "schedule_weekday": ("当天课表", "blue"),
    "compare_week": ("两周对比", "indigo"),
    "schedule_all": ("本学期课程", "indigo"),
    "schedule_image": ("课表图片", "blue"),
    "balance": ("校园卡余额", "green"),
    "empty_room": ("空教室", "orange"),
    "knowledge": ("校园知识", "purple"),
    "office": ("办公地点", "purple"),
    "activities": ("校园活动", "wathet"),
    "activity_advice": ("活动推荐", "wathet"),
    "activity_conflict": ("时间冲突检查", "orange"),
    "my_activities": ("我的活动", "wathet"),
    "activity_signup": ("报名活动", "orange"),
    "plan_day": ("日程建议", "indigo"),
    "remind": ("设置提醒", "orange"),
    "my_reminders": ("我的提醒", "orange"),
    "cancel_remind": ("取消提醒", "orange"),
    "memory_add": ("记住了", "turquoise"),
    "memory_list": ("我的记忆", "turquoise"),
    "memory_delete": ("忘掉了", "turquoise"),
    "mail": ("杭电邮箱", "blue"),
    "electric": ("宿舍电费", "yellow"),
    "daily_report": ("今日速览", "indigo"),
    "daily_report_set": ("每日早报", "indigo"),
    "consumption": ("消费分析", "yellow"),
    "study_plan": ("复习计划", "indigo"),
    "lostfound": ("失物招领", "grey"),
    "sunrun": ("阳光长跑", "orange"),
    "library": ("图书馆座位", "teal"),
    "weather": ("今日天气", "cyan"),
    "grade": ("成绩查询", "yellow"),
    "help": ("功能菜单", "turquoise"),
    "unknown": ("回复", "grey"),
}
_DEFAULT_STYLE = ("回复", "grey")
_BRAND = "校园木鱼"


def _resolve_title(text: str, intent: str) -> tuple[str, str]:
    """课表类标题要跟着内容走：内容是明天就不能写"今日课表"。"""
    if intent == "schedule":
        first_line = (text or "").split("\n", 1)[0].strip()
        if first_line.startswith("明天"):
            return ("明日课表", "blue")
        if first_line.startswith("今天"):
            return ("今日课表", "blue")
        return ("课表", "blue")
    if intent == "weather":
        first_line = (text or "").split("\n", 1)[0].strip()
        if "大后天" in first_line:
            return ("后日天气", "cyan")
        if "后天" in first_line:
            return ("后日天气", "cyan")
        if "明天" in first_line:
            return ("明日天气", "cyan")
        if "今天" in first_line:
            return ("今日天气", "cyan")
        return ("天气", "cyan")
    return _STYLE.get(intent, _DEFAULT_STYLE)


# 帮助菜单按钮：显示名 → (发给机器人的文本, 意图)
_MENU = [
    ("今天课表", "今天有啥课", "schedule"),
    ("明天课表", "明天有啥课", "schedule"),
    ("本周课表", "这周有啥课", "schedule_week"),
    ("下周课表", "下周有啥课", "schedule_next_week"),
    ("空教室", "正心楼有空教室吗", "empty_room"),
    ("校园卡", "校园卡余额", "balance"),
    ("天气", "今天天气怎么样", "weather"),
    ("图书馆", "图书馆有座位吗", "library"),
    ("阳光长跑", "我今天跑了几公里", "sunrun"),
    ("校园活动", "这周有啥活动", "activities"),
    ("今日速览", "今日速览", "daily_report"),
    ("消费分析", "我最近花了多少钱", "consumption"),
    ("复习计划", "帮我做个复习计划", "study_plan"),
    ("失物招领", "查失物招领", "lostfound"),
    ("我的提醒", "我的提醒有哪些", "my_reminders"),
]

BUTTON_ACTIONS: dict[str, tuple[str, str]] = {
    f"btn_{index}": (text, intent) for index, (_, text, intent) in enumerate(_MENU)
}

_BUTTONS_PER_ROW = 3


def build_help_menu(note: str = "") -> dict[str, Any]:
    """带按钮的功能菜单卡片。note 用来放「🎤 听到：xxx」这类附注。"""
    rows = []
    current: list[dict[str, Any]] = []
    for index, (label, _, _) in enumerate(_MENU):
        current.append({
            "tag": "button",
            "text": {"tag": "plain_text", "content": label},
            "type": "default",
            "value": {"action": f"btn_{index}"},
        })
        if len(current) == _BUTTONS_PER_ROW:
            rows.append({"tag": "action", "actions": current})
            current = []
    if current:
        rows.append({"tag": "action", "actions": current})

    intro = "**常用查询**\n点下面的按钮，或直接发送你的问题。"
    if note:
        intro = f"{note}\n\n{intro}"
    elements: list[dict[str, Any]] = [
        {"tag": "div", "text": {"tag": "lark_md", "content": intro}},
        *rows,
        {"tag": "hr"},
        {"tag": "note", "elements": [{"tag": "plain_text", "content": f"数据来自 HDUHelp · {_BRAND}"}]},
    ]
    return {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {"template": "turquoise", "title": {"tag": "plain_text", "content": f"{_BRAND} · 功能菜单"}},
        "elements": elements,
    }


def build_activity_confirm(detail: str, activity_id: str, name: str) -> dict[str, Any]:
    """写操作前的确认卡片：点按钮才真正提交。"""
    return {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {"template": "orange", "title": {"tag": "plain_text", "content": f"{_BRAND} · 确认报名"}},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": detail}},
            {"tag": "hr"},
            {"tag": "div", "text": {"tag": "lark_md", "content": "确认要报名吗？点下面按钮才会真正提交。"}},
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "✅ 确认报名"},
                        "type": "primary",
                        "value": {"action": "act_apply", "activity_id": activity_id, "activity_name": name},
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "算了"},
                        "type": "default",
                        "value": {"action": "act_abort"},
                    },
                ],
            },
        ],
    }


def build_kb_preview(text: str, token: str) -> dict[str, Any]:
    """知识库收录预览：点「入库」才真的写，点「丢弃」什么都不留。"""
    return {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {"template": "turquoise", "title": {"tag": "plain_text", "content": f"{_BRAND} · 收录预览"}},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": text}},
            {"tag": "hr"},
            {"tag": "div", "text": {"tag": "lark_md", "content": "入库之后可以用关键词搜到。确认收吗？"}},
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "入库"},
                        "type": "primary",
                        "value": {"action": "kb_save", "token": token},
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "丢弃"},
                        "type": "default",
                        "value": {"action": "kb_drop", "token": token},
                    },
                ],
            },
        ],
    }


def build_mail_confirm(text: str, token: str) -> dict[str, Any]:
    """发信确认：邮件发出去收不回，必须点一下才发。"""
    return {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {"template": "orange", "title": {"tag": "plain_text", "content": f"{_BRAND} · 确认发送"}},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": text}},
            {"tag": "hr"},
            {"tag": "div", "text": {"tag": "lark_md", "content": "点「发送」才会真的发出去，发出去收不回。"}},
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "发送"},
                        "type": "primary",
                        "value": {"action": "mail_send", "token": token},
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "取消"},
                        "type": "default",
                        "value": {"action": "mail_abort", "token": token},
                    },
                ],
            },
        ],
    }


def build_paged(text: str, token: str, page: int, has_next: bool) -> dict[str, Any]:
    """长列表卡片：正文 + 「下一页」按钮。分页状态在 pager 里，卡片只带令牌。"""
    elements: list[dict[str, Any]] = [
        {"tag": "div", "text": {"tag": "lark_md", "content": text}},
    ]
    actions: list[dict[str, Any]] = []
    if page > 0:
        actions.append(
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "上一页"},
                "type": "default",
                "value": {"action": "page_go", "token": token, "page": page - 1},
            }
        )
    if has_next:
        actions.append(
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "下一页"},
                "type": "primary",
                "value": {"action": "page_go", "token": token, "page": page + 1},
            }
        )
    if actions:
        elements.append({"tag": "hr"})
        elements.append({"tag": "action", "actions": actions})
    return {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {"template": "blue", "title": {"tag": "plain_text", "content": f"{_BRAND} · 回复"}},
        "elements": elements,
    }


def build_error(text: str) -> dict[str, Any]:
    return {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {"template": "red", "title": {"tag": "plain_text", "content": f"{_BRAND} · 出错了"}},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": text}},
        ],
    }


def build_progress(text: str) -> dict[str, Any]:
    """查询中的占位卡片，出结果后会被原地替换掉。"""
    return {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {"template": "grey", "title": {"tag": "plain_text", "content": f"{_BRAND} · 查询中"}},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": text}},
            {"tag": "note", "elements": [{"tag": "plain_text", "content": "出结果会自动替换这张卡片"}]},
        ],
    }


def build_blocks(blocks: list[str], intent: str) -> dict[str, Any]:
    """多段回答：每段一个 markdown 块，段间加分隔线。

    标题和配色仍按整段内容走 _resolve_title，和单段时一致。
    """
    text = "\n\n".join(blocks)
    title, template = _resolve_title(text, intent)
    elements: list[dict[str, Any]] = []
    for index, block in enumerate(blocks):
        if index:
            elements.append({"tag": "hr"})
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": block}})
    elements.append({"tag": "hr"})
    elements.append(
        {
            "tag": "note",
            "elements": [{"tag": "plain_text", "content": f"数据来自 HDUHelp · {_BRAND}"}],
        }
    )
    return {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {
            "template": template,
            "title": {"tag": "plain_text", "content": f"{_BRAND} · {title}"},
        },
        "elements": elements,
    }


def with_feedback(card: dict[str, Any], question: str) -> dict[str, Any]:
    """在卡片底部加「答得不对」按钮；点了会记录样本并原地重答。"""
    text = (question or "").strip()[:200]
    if not text:
        return card
    action = {
        "tag": "action",
        "actions": [
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "答得不对"},
                "type": "default",
                "value": {"action": "answer_wrong", "question": text},
            }
        ],
    }
    elements = card.get("elements") or []
    if elements and elements[-1].get("tag") == "note":
        elements.insert(len(elements) - 1, action)
    else:
        elements.append(action)
    return card


def build(text: str, intent: str) -> dict[str, Any]:
    title, template = _resolve_title(text, intent)
    return {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {
            "template": template,
            "title": {"tag": "plain_text", "content": f"{_BRAND} · {title}"},
        },
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": text}},
            {"tag": "hr"},
            {
                "tag": "note",
                "elements": [
                    {"tag": "plain_text", "content": f"数据来自 HDUHelp · {_BRAND}"},
                ],
            },
        ],
    }


def _md_title(title: str) -> str:
    return str(title or "").replace("[", "［").replace("]", "］")


def build_news(
    groups: dict[str, list[dict[str, Any]]],
    hot: list[str] | None = None,
    campus: list[str] | None = None,
    title: str = "新闻",
) -> dict[str, Any]:
    """新闻卡片：校内通知 + 热搜 + 分类新闻，每类一个「多来点」按钮。"""
    elements: list[dict[str, Any]] = []
    if campus:
        elements.append(
            {"tag": "div", "text": {"tag": "lark_md", "content": "**【校内通知】**\n" + "\n".join(campus)}}
        )
        elements.append({"tag": "hr"})
    if hot:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "**【热搜】**\n" + "\n".join(hot)}})
        elements.append({"tag": "hr"})
    for cat, items in groups.items():
        body = []
        for it in items:
            name = _md_title(str(it.get("t") or ""))
            src = str(it.get("s") or "")
            suffix = f"（{src}）" if src else ""
            link = str(it.get("l") or "")
            body.append(f"[{name}]({link}){suffix}" if link else f"{name}{suffix}")
        elements.append(
            {"tag": "div", "text": {"tag": "lark_md", "content": f"**【{cat}】**\n" + "\n".join(body)}}
        )
        elements.append(
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": f"多来点{cat}"},
                        "type": "default",
                        "value": {"action": "news_like", "category": cat},
                    }
                ],
            }
        )
        elements.append({"tag": "hr"})
    elements.append(
        {"tag": "note", "elements": [{"tag": "plain_text", "content": "点分类按钮，以后这类会多推"}]}
    )
    return {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {"template": "blue", "title": {"tag": "plain_text", "content": f"{_BRAND} · {title}"}},
        "elements": elements,
    }
