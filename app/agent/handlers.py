"""各意图的处理逻辑，统一返回飞书可直接发送的纯文本。"""
from __future__ import annotations

import random
import re
import threading
import time
from datetime import datetime, timedelta

from app.agent import router
from app.tools import classroom, schedule_store
from app.tools.hduhelp import (
    HduhelpError,
    get_card_balance,
    get_grade,
    get_schedule_now,
    get_sunrun_overview,
    get_weather,
    knowledge_retrieve,
    list_activities,
)

HELP_TEXT = (
    "咚～本鱼在。可以帮你：\n"
    "• 课表：今天有啥课 / 这周有啥课 / 下周有啥课 / 这学期有哪些课\n"
    "• 空间：正心楼4楼空教室 / 图书馆还有座位吗\n"
    "• 生活：校园卡余额 / 宿舍电费 / 消费分析 / 天气\n"
    "• 活动：这周有啥活动值得去 / 周三的讲座我能去吗\n"
    "• 助理：明天8点提醒我交作业 / 今日速览 / 复习计划 / 失物招领\n"
    "也可以直接发语音。"
)

FALLBACK_TEXT = (
    "这个本鱼还没学会。你可以试试：\n"
    "• 今天有啥课 / 这周有啥课 / 下周有啥课\n"
    "• 校园卡余额 / 宿舍电费 / 今日速览\n"
    "• 正心楼4楼空教室 / 图书馆有座位吗\n"
    "• 明天8点提醒我交作业"
)


_TIP_INTENTS = {
    "schedule", "schedule_week", "schedule_next_week", "schedule_weekday", "schedule_all",
    "compare_week", "balance", "empty_room", "activities", "activity_advice",
    "plan_day", "daily_report", "consumption", "study_plan", "lostfound",
    "sunrun", "library", "weather", "electric", "mail",
}

_FUTURE_HINTS = ("大后天", "后天", "明天", "下周", "下个月", "未来")
_NEWS_HINTS = ("新闻", "头条", "时事", "发生了什么", "有什么大事", "出了什么事")


def _future_news_reply(text: str) -> str | None:
    """「后天的新闻」这种未来事件，现在不可能有结果，别拿旧内容凑数。"""
    text = text or ""
    if not (any(f in text for f in _FUTURE_HINTS) and any(n in text for n in _NEWS_HINTS)):
        return None
    when = next((w for w in _FUTURE_HINTS if w in text), "那会儿")
    return f"{when}的新闻还没发生，现在没有。等到了那天再问本鱼，或者本鱼先说说今天的？"


# 只匹配 URL 合法字符：紧跟着的中文是用户的话（「…/概括内容」里的「概括内容」），不能吞进网址
_URL_RE = re.compile(r"https?://[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+")


def _fetch_failed(page: str) -> bool:
    return page.startswith("抓取失败") or any(k in page for k in ("打不开", "不是网页", "没抓到正文"))


_SMALL_TALK = ("谢谢", "多谢", "感谢", "好的", "好嘞", "ok", "okay", "嗯", "收到",
               "明白了", "知道了", "懂了", "行吧")
# 带这些字的不是纯寒暄，可能是接着问事
_QUESTION_HINTS = ("课", "天气", "余额", "活动", "提醒", "查", "多少", "什么", "哪", "几")


def _is_small_talk(text: str) -> bool:
    """「OK，谢谢」这类纯应答：不调模型。

    模型拿到这种话会顺着上下文继续展开，而上一条回答如果本身有错，
    它就把错误当事实接着编（实测：把「今天上午有英语课」说成「明天 15:05」）。
    """
    value = re.sub(r"[\s，,。.!！?？~～、]", "", (text or "").lower())
    if not value or len(value) > 10:
        return False
    if any(hint in value for hint in _QUESTION_HINTS):
        return False
    return any(value.startswith(word) for word in _SMALL_TALK)


_SMALL_TALK_STYLES = (
    "直接回一句口语，别用比喻，也别用固定开场白",
    "可以带一个木头或敲击的比喻，只用一个",
    "像熟人聊天那样回应，短一点，别客套",
)


def _small_talk_reply(text: str) -> str:
    """寒暄交给模型说，但把上下文切断。

    只给它这一句 + 人设，不给历史、不给工具。上一轮回答要是错的，
    模型会顺着改写成新事实（实测：「今天上午有英语课」被续成「明天 15:05 英语课」）。
    每次随机挑一种回应风格，避免每条都「咚～主人」+ 雨滴比喻。
    """
    try:
        from app.llm.client import chat

        style = random.choice(_SMALL_TALK_STYLES)
        reply = chat(
            f"用户说：{text}\n"
            f"{style}。保持你的性格，一两句话就够。\n"
            "不要提任何课程、时间、地点、金额、活动——这些你现在不知道，也不许猜。",
            max_tokens=120,
            purpose="small_talk",
        )
        if reply and reply.strip():
            return reply.strip()
    except Exception:
        pass
    return "好嘞，有事随时叫本鱼。"


def answer(text: str, chat_id: str | None = None, intent: router.Intent | None = None) -> str:
    # 用户在说「上一条理解错了」：先记下来，下次同类问法会把这条纠正注入提示词
    if _is_audit_confirm(text or ""):
        return _handle_audit_confirm(text or "", chat_id)
    if _is_audit_ignore(text or ""):
        return _handle_audit_ignore(text or "", chat_id)
    if _is_modify_correction(text or ""):
        return _handle_modify_correction(text or "", chat_id)
    if _is_list_corrections(text or ""):
        return _handle_list_corrections(chat_id)
    if _is_retract(text or ""):
        return _handle_retract(text or "", chat_id)
    if _is_correction(text or ""):
        _handle_correction(text or "", chat_id)
        return "记下了，下次遇到类似说法本鱼会注意。"

    # 文字确认：微信没有卡片按钮，靠回一句「确认入库/确认发送/确认报名」执行。
    # 飞书走自己的卡片回调，这里对它是新增入口，不改变原行为。
    quick = _text_confirm(text or "", chat_id)
    if quick:
        return quick

    # 写操作不走模型：模型爱自己加戏（「人格能改课表改不了」），代码直接出结果
    if "修改木鱼" in (text or ""):
        direct = _handle_write_instruction(text or "", chat_id)
        if direct:
            return direct

    # 问课表/自习安排「为什么」：先认是不是自己加的自定义，再解释，不查题库、不联网
    if "为什么" in (text or "") and any(k in (text or "") for k in ("课", "自习", "教室")):
        return _explain_schedule(text or "", chat_id)

    # 纯寒暄：给模型自由发挥，但不让它碰事实
    if _is_small_talk(text or ""):
        return _small_talk_reply(text)

    # 「收录 网址」走另一条路：抽要点 → 弹预览卡 → 用户点确认才入库。
    # 必须排在下面的「带链接先抓正文」之前，否则会被当普通链接去总结。
    if "收录" in (text or "") and _URL_RE.search(text or ""):
        if intent is None:
            intent = router.detect(text, chat_id)
        if intent.name == router.INTENT_KB_ADD:
            return handle_kb_add(text, chat_id)

    # 消息里带链接：先把正文抓下来，不管模型会不会主动调工具
    urls = _URL_RE.findall(text or "")
    page_text = ""
    if urls:
        from app.tools import webpage

        page_text = webpage.fetch(urls[0])
        if _fetch_failed(page_text):
            reply = f"这个链接本鱼打不开（{page_text[:50]}）。把正文贴给本鱼，或者给个标题本鱼帮你搜。"
            if chat_id:
                from app.agent import context

                context.remember(chat_id, text, "webpage", reply)
            return reply

    if page_text:
        try:
            from app.agent import agent as agent_runner

            prompt = f"{text}\n\n【链接正文】\n{page_text[:3000]}\n\n（请基于上面链接里的内容回答）"
            reply = agent_runner.run(prompt, chat_id)
            if reply:
                if chat_id:
                    from app.agent import context

                    context.remember(chat_id, text, "webpage", reply)
                return reply
        except Exception:
            pass

    future = _future_news_reply(text)
    if future:
        if chat_id:
            from app.agent import context

            context.remember(chat_id, text, "future_news", future)
        return future

    if intent is None:
        intent = router.detect(text, chat_id)

    # 公共问答：几个容易被关键词带偏的说法，先查本地题库（带官网出处、零 LLM）
    from app.tools import faq

    # 复合问题：规则只能答一半，交给 SDK 接着查（SDK 关着会自动跳过）
    if _needs_deep_agent(text, intent):
        from app.agent import sdk_agent

        deep = sdk_agent.run(text, chat_id)
        if deep:
            from app.agent import suggestions as _deep_suggestions

            if chat_id:
                from app.agent import context as _deep_context

                _deep_context.remember(chat_id, text, "agent", deep)
            return deep + _deep_suggestions.render(intent.name)

    if _faq_worthy(text, intent):
        hits = _faq_multi(text) or faq.search(text)
        if hits:
            reply = _faq_reply(text, hits)
            from app.agent import suggestions

            if chat_id:
                from app.agent import context

                context.remember(chat_id, text, "faq", reply)
            return reply + suggestions.render(intent.name)

    # 规则没认出来、或者一句话跨多个领域 → 直接交给 Agent 去查数据。
    # 不让 LLM 先猜意图：只看一句话猜「今天有什么安排」会猜成查课表，
    # Agent 拿到工具能自己查课表+天气+空教室，不靠猜。
    # 例外是规则已明确识别的复合意图（"哪周课多"），走代码更快更准。
    try:
        from app.agent import agent as agent_runner

        explicit = {router.INTENT_COMPARE_WEEK}
        need_agent = intent.name == router.INTENT_UNKNOWN or agent_runner.is_multi_intent(text)
        if intent.name not in explicit and need_agent:
            from app.agent import sdk_agent

            # SDK 兜底默认关；关着或失败都返回 None，自动落到现有 agent
            agent_reply = sdk_agent.run(text, chat_id) or agent_runner.run(text, chat_id)
            if agent_reply:
                # Agent 查了数据的回复也补一句建议，和规则路径保持一致
                if "（已查询：" in agent_reply and "💡" not in agent_reply:
                    tip = _ai_tip(agent_reply[:400], "给一句建议或提醒")
                    if tip:
                        agent_reply = f"{agent_reply}\n\n💡 {tip}"
                from app.agent import suggestions

                if chat_id:
                    from app.agent import context

                    context.remember(chat_id, text, "agent", agent_reply)
                return agent_reply + suggestions.render(intent.name)

            if intent.name == router.INTENT_UNKNOWN:
                llm_intent = router.classify_with_llm(text, chat_id)
                if llm_intent:
                    intent = llm_intent
    except Exception:
        pass
    handlers = {
        router.INTENT_BALANCE: lambda: handle_balance(chat_id),
        router.INTENT_EMPTY_ROOM: lambda: handle_empty_room(intent.building, intent.building_text, intent.floor),
        router.INTENT_GRADE: handle_grade,
        router.INTENT_EXAM: handle_exam,
        router.INTENT_COLLEGE: lambda: handle_college(text, chat_id),
        router.INTENT_KNOWLEDGE: lambda: handle_knowledge(text),
        router.INTENT_OFFICE: lambda: handle_knowledge(text),
        router.INTENT_SELF_CHECK: handle_self_check,
        router.INTENT_USAGE: handle_usage,
        router.INTENT_HELP: lambda: HELP_TEXT,
        router.INTENT_SCHEDULE_WEEK: handle_week,
        router.INTENT_SCHEDULE_NEXT_WEEK: handle_next_week,
        router.INTENT_COMPARE_WEEK: handle_compare_week,
        router.INTENT_SCHEDULE_WEEKDAY: lambda: handle_weekday(intent.weekday),
        router.INTENT_SCHEDULE_ALL: handle_all_courses,
        router.INTENT_SCHEDULE_IMAGE: handle_schedule_image,
        router.INTENT_ACTIVITIES: lambda: handle_activities(text, chat_id=chat_id),
        router.INTENT_CANTEEN: lambda: handle_canteen(text),
        router.INTENT_ACTIVITY_SIGNUP: lambda: handle_activity_signup(text, chat_id),
        router.INTENT_MY_ACTIVITIES: handle_my_activities,
        router.INTENT_ACTIVITY_ADVICE: lambda: handle_activity_advice(text),
        router.INTENT_PLAN_DAY: lambda: handle_plan_day(text, chat_id),
        router.INTENT_ACTIVITY_CONFLICT: lambda: handle_activity_conflict(text),
        router.INTENT_REMIND: lambda: handle_remind(text, chat_id),
        router.INTENT_MY_REMINDERS: lambda: handle_my_reminders(chat_id),
        router.INTENT_HOMEWORK: lambda: handle_homework(text, chat_id),
        router.INTENT_BEST_PLACE: lambda: handle_best_place(text, chat_id),
        router.INTENT_SEMESTER_REPORT: lambda: handle_semester_report(chat_id),
        router.INTENT_EXPENSE_CHART: lambda: _consumption_reply(text, chat_id),
        router.INTENT_CANCEL_REMIND: lambda: handle_cancel_remind(text, chat_id),
        router.INTENT_EDIT_REMIND: lambda: handle_edit_remind(text, chat_id),
        router.INTENT_MEMORY_ADD: lambda: handle_memory_add(text, chat_id),
        router.INTENT_MEMORY_LIST: lambda: handle_memory_list(chat_id),
        router.INTENT_MEMORY_DELETE: lambda: handle_memory_delete(text, chat_id),
        router.INTENT_PUSH_SET: lambda: handle_push_switch(text, chat_id),
        router.INTENT_MAIL_SEND: lambda: handle_mail_send(text, chat_id),
        router.INTENT_MAIL: handle_mail,
        router.INTENT_ELECTRIC: lambda: handle_electric(chat_id),
        router.INTENT_DAILY_REPORT: lambda: handle_daily_report(text, chat_id),
        router.INTENT_DAILY_REPORT_SET: lambda: handle_morning_report(text, chat_id),
        router.INTENT_CONSUMPTION: lambda: _consumption_reply(text, chat_id),
        router.INTENT_STUDY_PLAN: lambda: handle_study_plan(text),
        router.INTENT_LOSTFOUND: lambda: handle_lostfound(text),
        router.INTENT_WEATHER: lambda: handle_weather(text),
        router.INTENT_NEWS_PREF: lambda: handle_news_pref(text, chat_id),
        router.INTENT_NEWS: lambda: handle_news(text, chat_id),
        router.INTENT_SUNRUN: lambda: handle_sunrun(chat_id),
        router.INTENT_LIBRARY: handle_library,
    }
    if intent.name == router.INTENT_SCHEDULE:
        if intent.weekday:
            return handle_weekday(intent.weekday)
        if any(k in text for k in ("后天", "大后天")):
            offset = 3 if "大后天" in text else 2
            target = datetime.now().isoweekday() + offset
            if target <= 7:
                return handle_weekday(target)
            return "这两天已经跨到下周了，问「下周有啥课」能看到整周安排。"
        if any(k in text for k in ("明天", "明日")):
            only = "tomorrow"
        elif any(k in text for k in ("今天", "今日")):
            only = "today"
        else:
            only = None
        # 「安排怎么样」「如何」这类是问评价，要带点评
        want_comment = any(k in text for k in ("怎么样", "如何", "评价", "安排", "忙不忙", "累不累"))
        handler = lambda: handle_schedule(only, want_comment)  # noqa: E731
    else:
        handler = handlers.get(intent.name)
    if not handler:
        # 规则和意图都认不出 → 交给 Agent 自主选工具（最多 2 轮）
        try:
            from app.agent import agent as agent_runner

            agent_reply = agent_runner.run(text, chat_id)
        except Exception:
            agent_reply = None
        reply = agent_reply or FALLBACK_TEXT
    else:
        try:
            reply = handler()
        except HduhelpError as exc:
            reply = f"查询失败：{exc}"
        except Exception as exc:  # 兜底，避免机器人静默无响应
            reply = f"出了点问题：{type(exc).__name__}: {exc}"

    # 数据类回复统一补一句 AI 建议（闲聊不加，免得每条都煽情）
    if intent.name in _TIP_INTENTS and "💡" not in reply and len(reply) > 15:
        tip = _ai_tip(reply[:400], "给一句建议或提醒")
        if tip:
            reply = f"{reply}\n\n💡 {tip}"

    # 知识库挂了别把「暂时不可用」直接甩给用户，让 Agent 换个方式答
    if intent.name in (router.INTENT_KNOWLEDGE, router.INTENT_OFFICE) and "暂时不可用" in reply:
        try:
            from app.agent import agent as agent_runner

            agent_reply = agent_runner.run(text, chat_id)
            if agent_reply:
                reply = agent_reply
        except Exception:
            pass

    from app.agent import suggestions

    if chat_id:
        from app.agent import context

        context.remember(chat_id, text, intent.name, reply)
    return reply + suggestions.render(intent.name)


def _period_label(start_section) -> str:
    """节次 → 时段。必须由代码算：模型看到「6-7 节」会自己猜成上午。"""
    try:
        start = int(start_section)
    except (TypeError, ValueError):
        return ""
    if start <= 4:
        return "上午"
    if start <= 8:
        return "下午"
    return "晚上"


def _fmt_course(item: dict) -> str:
    duration = (item.get("duration") or "").replace(" - ", "-")
    period = _period_label(item.get("startSection"))
    tag = f"（{period}）" if period else ""
    head = f"{item.get('startSection')}-{item.get('endSection')} 节{tag} {duration}".strip()
    line = f"• {head} {item.get('courseName', '')}"
    detail = " · ".join(x for x in (item.get("classroom"), item.get("teacher")) if x)
    return f"{line}\n  {detail}" if detail else line


def handle_schedule(only: str | None = None, want_comment: bool = False) -> str:
    data = get_schedule_now()
    now = datetime.now()
    # 接口不知道本机改过课表，今日/明日也要把自定义叠上去
    today = schedule_store.apply_custom_to_items(data["today"], now.date())
    tomorrow = schedule_store.apply_custom_to_items(
        data["tomorrow"], now.date() + timedelta(days=1)
    )
    # 课表接口不给晚自习，这里按日期补上（第 1-20 周、周日至周四、节假日除外）
    today_study = schedule_store.study_for_day(now.date())
    if today_study:
        today = list(today) + [today_study]
    tomorrow_study = schedule_store.study_for_day(now.date() + timedelta(days=1))
    if tomorrow_study:
        tomorrow = list(tomorrow) + [tomorrow_study]
    weekday_cn = "一二三四五六日"[now.weekday()]
    if only == "today":
        lines = [f"今天 周{weekday_cn}（{now:%m-%d}）"]
        lines += [_fmt_course(c) for c in today] or ["• 今天没课"]
        main = "\n".join(lines)
        if want_comment:
            return _with_tip(main, f"今天共 {len(today)} 门课：" + "、".join(c.get("courseName", "") for c in today), "点评今天的节奏")
        return main
    if only == "tomorrow":
        lines = [f"明天 周{'一二三四五六日'[(now.weekday() + 1) % 7]}"]
        lines += [_fmt_course(c) for c in tomorrow] or ["• 明天没课"]
        main = "\n".join(lines)
        if want_comment:
            return _with_tip(main, f"明天共 {len(tomorrow)} 门课：" + "、".join(c.get("courseName", "") for c in tomorrow), "点评明天的节奏")
        return main

    lines = [f"周{weekday_cn}（{now:%m-%d}）"]
    lines += [_fmt_course(c) for c in today] or ["• 今天没课"]
    lines.append("")
    lines.append("明天")
    lines += [_fmt_course(c) for c in tomorrow] or ["• 明天没课"]
    return "\n".join(lines)


def handle_balance(chat_id: str | None = None) -> str:
    balance = get_card_balance()
    main = f"校园卡余额：{balance:.2f} 元"
    # 校内一顿饭大约 10-15 元，以此为参照，别让模型凭空判断
    meals = int(balance // 12) if balance >= 0 else 0
    if chat_id:
        from app.tools import records

        records.log_snapshot(chat_id, "balance", {"balance": round(balance, 2)})
    facts = f"校园卡余额 {balance:.2f} 元，按食堂一顿 12 元算约够 {meals} 顿"
    return _with_tip(main, facts, "余额低于 50 元就说该充了，否则说够用")


def _fmt_week_item(item: dict) -> str:
    duration = ""
    if item.get("start_time") and item.get("end_time"):
        duration = f"{item['start_time']}-{item['end_time']}"
    head = f"{item['start_section']}-{item['end_section']} 节 {duration}".strip()
    detail = " · ".join(x for x in (item.get("location"), item.get("teacher")) if x)
    line = f"• {head} {item['course']}"
    return f"{line}\n  {detail}" if detail else line


def _summarize_week(items: list[dict]) -> str | None:
    """代码先算清事实（总节数、最忙/最轻松），再让 LLM 只说人话。

    不让模型自己数——实测它会算错（把 6 节的周一说成最忙，实际周二 8 节最多）。
    """
    try:
        from app.llm.client import chat

        counts: dict[int, int] = {}
        for item in items:
            span = item["end_section"] - item["start_section"] + 1
            counts[item["day"]] = counts.get(item["day"], 0) + span
        if not counts:
            return None

        total = sum(counts.values())
        busiest = max(counts, key=lambda d: counts[d])
        lightest = min(counts, key=lambda d: counts[d])
        facts = (
            f"总课时 {total} 节；最忙是{schedule_store.WEEKDAY_CN.get(busiest)}（{counts[busiest]} 节）；"
            f"最轻松是{schedule_store.WEEKDAY_CN.get(lightest)}（{counts[lightest]} 节）"
        )
        prompt = (
            f"以下是已经算好的事实，不要改动任何数字：{facts}。"
            "用一句话说给大一说，50 字以内，直接给结论，不要客套。"
        )
        return chat(prompt, max_tokens=90)
    except Exception:
        return None


def handle_next_week() -> str:
    current = schedule_store.current_week()
    if not current:
        return "课表缓存里没有周次信息，先在电脑上运行 py -3 scripts/cache_schedule.py"
    target = current + 1
    items = schedule_store.by_week(target)
    title = f"下周（第 {target} 周）"
    if not items:
        return f"{title}没有课。"

    lines = [f"{title}课表"]
    summary = _summarize_week(items)
    if summary:
        lines.append(f"\n{summary}")
    current_day = None
    for item in items:
        if item["day"] != current_day:
            current_day = item["day"]
            lines.append("")
            lines.append(schedule_store.WEEKDAY_CN.get(current_day, f"周{current_day}"))
        lines.append(_fmt_week_item(item))
    return "\n".join(lines)


def handle_compare_week() -> str:
    current = schedule_store.current_week()
    if not current:
        return "课表缓存里没有周次信息，先在电脑上运行 py -3 scripts/cache_schedule.py"

    def total(items: list[dict]) -> int:
        return sum(i["end_section"] - i["start_section"] + 1 for i in items)

    this_items = schedule_store.this_week()
    next_items = schedule_store.by_week(current + 1)
    this_count, next_count = total(this_items), total(next_items)

    if this_count > next_count:
        head = f"这周更多：本周 {this_count} 节 vs 下周 {next_count} 节"
    elif next_count > this_count:
        head = f"下周更多：下周 {next_count} 节 vs 本周 {this_count} 节"
    else:
        head = f"两周一样多：各 {this_count} 节"

    def per_day(items: list[dict]) -> str:
        counts: dict[int, int] = {}
        for item in items:
            counts[item["day"]] = counts.get(item["day"], 0) + (item["end_section"] - item["start_section"] + 1)
        return "、".join(
            f"{schedule_store.WEEKDAY_CN.get(d, f'周{d}')}{counts[d]}节" for d in sorted(counts)
        )

    lines = [head, "", f"本周（第 {current} 周）：{per_day(this_items) or '无课'}",
             f"下周（第 {current + 1} 周）：{per_day(next_items) or '无课'}"]
    return "\n".join(lines)


def handle_week() -> str:
    items = schedule_store.this_week()
    if not items:
        return "本周没有课。\n（课表还没缓存的话，先在电脑上运行 py -3 scripts/cache_schedule.py）"
    lines = ["本周课表"]
    summary = _summarize_week(items)
    if summary:
        lines.append(f"\n{summary}")
    current_day = None
    for item in items:
        if item["day"] != current_day:
            current_day = item["day"]
            lines.append("")
            lines.append(schedule_store.WEEKDAY_CN.get(current_day, f"周{current_day}"))
        lines.append(_fmt_week_item(item))
    return "\n".join(lines)


def handle_weekday(weekday: int | None) -> str:
    if not weekday:
        return "想问哪天？比如「周三有啥课」"
    items = schedule_store.by_weekday(weekday)
    name = schedule_store.WEEKDAY_CN.get(weekday, f"周{weekday}")
    if not items:
        return f"本周{name}没有课。"
    lines = [f"本周{name}"]
    lines += [_fmt_week_item(i) for i in items]
    return "\n".join(lines)


def handle_schedule_image() -> str:
    """命令行用；飞书里由 bot 直接发图片。"""
    from app.tools import schedule_image

    path = schedule_image.render_week()
    return f"课表图片已生成：{path}"


def handle_all_courses() -> str:
    items = schedule_store.all_courses()
    if not items:
        return "课表还没缓存。先在电脑上运行 py -3 scripts/cache_schedule.py"
    lines = [f"这学期共 {len(items)} 门课："]
    for item in items:
        weeks = item["weeks"]
        week_text = ""
        if weeks:
            week_text = f"第 {weeks[0]}-{weeks[-1]} 周" if weeks == list(range(weeks[0], weeks[-1] + 1)) else f"第 {','.join(map(str, weeks))} 周"
        meta = " · ".join(x for x in (week_text, item.get("credit") and f"{item['credit']} 学分", item.get("teacher")) if x)
        lines.append(f"• {item['course']}" + (f"\n  {meta}" if meta else ""))
    return "\n".join(lines)


def _describe_sections(sections: list[int]) -> str:
    """[5,6,7,8] → 第 5-8 节（13:30-16:00）"""
    if not sections:
        return "今天已无课"
    first, last = min(sections), max(sections)
    start = classroom.SECTION_TIME.get(first, ("", ""))[0]
    end = classroom.SECTION_TIME.get(last, ("", ""))[1]
    span = f"第 {first}-{last} 节" if first != last else f"第 {first} 节"
    return f"{span}（{start}-{end}）"


def handle_empty_room(building: str | None, building_text: str | None = None,
                      floor: int | None = None, sections: list[int] | None = None) -> str:
    # 优先按楼名（正心楼）解析，其次数字楼栋（3 号楼），最后默认正心楼
    user_specified = bool(building_text or building)
    resolved = None
    if building_text:
        resolved = classroom.resolve_building(building_text)
        if not resolved:
            return f"没找到「{building_text}」的教室数据。\n目前缓存了：{', '.join(building_texts())}\n先运行 py -3 scripts/cache_classrooms.py 可补全。"
    if not resolved and building:
        resolved = building
    if not resolved:
        resolved = "ZX"
    building = resolved

    if sections is None:
        sections = classroom.current_sections()
        if not sections:
            return f"{classroom.building_label(building)}\n今天的课都上完了，明天早上再来问吧"
        when = classroom.describe_section(sections)
        if not classroom.is_in_session():
            when = f"接下来{when}"
        if datetime.now().hour < 6:
            when = f"今天早上的{when}"
    else:
        when = _describe_sections(sections)
    weekday = datetime.now().isoweekday()
    rooms = classroom.find_empty(building, weekday, sections)
    if floor and int(floor) > 10:
        # 教学楼没有 10 层以上；多半是把「18:30」这类时间当成了楼层
        floor = None
    if floor:
        rooms = [r for r in rooms if classroom.room_floor(r["name"]) == floor]
    label = classroom.building_label(building)
    if floor:
        label = f"{label} {floor} 楼"
    if not rooms:
        return f"{label} · {when}\n这个时段没有空闲教室，换个楼层或换节课再问？"

    shown = rooms[:12]
    lines = [f"{label} · {when}", f"空着的教室（{len(rooms)} 间）："]
    lines += [f"• {r['name']}" for r in shown]
    if len(rooms) > len(shown):
        lines.append(f"…还有 {len(rooms) - len(shown)} 间")
    if not user_specified:
        lines.append("（没指定楼，默认查正心楼；想看别的楼就说「创智楼有空教室吗」）")
    main = "\n".join(lines)
    tip_facts = f"{label}{(' ' + str(floor) + ' 楼') if floor else ''}在 {when} 有 {len(rooms)} 间空闲教室"
    return _with_tip(main, tip_facts, "给一句选教室的建议")


def building_texts() -> list[str]:
    try:
        return classroom.cached_buildings()
    except Exception:
        return []


def _weather_advice(temp: float | None, desc: str, aqi: int | None) -> str:
    """天气建议用代码判断，不让模型编——实测它总爱加"温差大""带伞"这种不存在的信息。"""
    tips: list[str] = []
    if isinstance(temp, (int, float)):
        if temp < 8:
            tips.append("挺冷的，穿厚点")
        elif temp < 16:
            tips.append("有点凉，加件外套")
        elif temp < 25:
            tips.append("温度舒服，正常穿就行")
        else:
            tips.append("偏热，穿透气一点")
    if desc and any(k in desc for k in ("雨", "雪", "阵雨")):
        tips.append("可能下雨，带把伞")
    if isinstance(aqi, int):
        if aqi > 150:
            tips.append("空气质量差，少在户外待")
        elif aqi > 100:
            tips.append("空气一般，敏感人群注意")
    return "，".join(tips)


def handle_weather(text: str = "") -> str:
    """天气：主用 Open-Meteo（有预报），拿不到时退回 HDUHelp 的当前天气。"""
    from app.tools import weather as weather_tool

    # 问了哪个城市就查哪个；内置表没有的城市走地名解析，认不出才用默认（绍兴校区）
    place = weather_tool.resolve(text)
    if place.get("unknown"):
        return f"「{place['name']}」这个地方本鱼暂时没查到，换个说法或过一会儿再试一次？"
    city = place["name"]
    prefix = "" if place["local"] else f"{city} · "

    # AQI 只有本地站有，和预报并行取，别串行多等半秒
    aqi_event = threading.Event()
    aqi_box: dict = {}
    if place["local"]:

        def _load_aqi() -> None:
            try:
                aqi_box["data"] = get_weather()
            except Exception:
                pass
            finally:
                aqi_event.set()

        threading.Thread(target=_load_aqi, daemon=True, name="weather-aqi").start()
    else:
        aqi_event.set()

    data = weather_tool.fetch(place)
    if not data.get("ok"):
        aqi_event.wait(3)
        legacy = aqi_box.get("data") or {}
        temp = legacy.get("temp")
        desc = legacy.get("description") or ""
        if isinstance(temp, (int, float)):
            head = f"今天{desc} {temp:.1f}°C"
        elif desc:
            head = f"今天{desc}"
        else:
            return prefix + "暂时拿不到天气数据。"
        # 问的是明天/后天却只拿到当前天气，得说清楚，别让人以为答非所问
        ahead = "明天" if any(k in text for k in ("明天", "明日")) else ("后天" if "后天" in text else "")
        note = f"{ahead}的预报暂时没拿到，先给你现在的天气：\n" if ahead else ""
        advice = _weather_advice(temp if isinstance(temp, (int, float)) else None, desc, None)
        return f"{prefix}{note}{head}" + (f"\n\n💡 {advice}" if advice else "")

    if any(word in text for word in ("这周", "本周", "一周", "七天", "7天")):
        week = data.get("week") or []
        if week:
            lines = [f"{prefix}未来 7 天"]
            for item in week:
                date = str(item.get("date") or "")
                label = ""
                if date:
                    try:
                        label = "周" + "一二三四五六日"[datetime.strptime(date, "%Y-%m-%d").weekday()]
                    except ValueError:
                        label = ""
                span = (
                    f"{item['min']}~{item['max']}°C"
                    if item.get("min") is not None and item.get("max") is not None
                    else ""
                )
                pop = f" 降水 {item['pop']}%" if item.get("pop") is not None else ""
                row = " ".join(
                    part for part in (date[5:], label, item.get("text") or "", span + pop) if part
                )
                lines.append(row.strip())
            return "\n".join(lines)

    for _label, _key in (("大后天", "day3"), ("后天", "day_after")):
        if _label in text:
            day = data.get(_key) or {}
            if day.get("min") is None and day.get("max") is None:
                break
            span = f"{day['min']}~{day['max']}°C" if day["min"] is not None and day["max"] is not None else ""
            pop = f"，降水 {day['pop']}%" if day.get("pop") is not None else ""
            main = f"{prefix}{_label} {day['text'] or '天气'} {span}{pop}".strip()
            mid = (day["min"] + day["max"]) / 2 if isinstance(day["min"], (int, float)) and isinstance(day["max"], (int, float)) else None
            advice = _weather_advice(mid, day.get("text") or "", None)
            return f"{main}\n\n💡 {advice}" if advice else main

    want_tomorrow = any(k in text for k in ("明天", "明日"))
    if want_tomorrow:
        day = data["tomorrow"]
        span = f"{day['min']}~{day['max']}°C" if day["min"] is not None and day["max"] is not None else ""
        pop = f"，降水 {day['pop']}%" if day.get("pop") is not None else ""
        main = f"{prefix}明天 {day['text'] or '天气'} {span}{pop}".strip()
        if isinstance(day["min"], (int, float)) and isinstance(day["max"], (int, float)):
            mid = (day["min"] + day["max"]) / 2
        else:
            mid = None
        advice = _weather_advice(mid, day.get("text") or "", None)
        return f"{main}\n\n💡 {advice}" if advice else main

    today = data["today"]
    temp = data["temp"]
    span = f"{today['min']}~{today['max']}°C" if today["min"] is not None and today["max"] is not None else ""
    pop = f"，降水 {today['pop']}%" if today.get("pop") is not None else ""
    head = f"{prefix}今天 {data['text'] or ''} {temp:.1f}°C".strip()
    if span:
        head += f"（{span}{pop}）"
    lines = [head]

    hours = [h for h in data.get("hours") or [] if h.get("temp") is not None][:5]
    if hours:
        lines.append("接下来：" + "、".join(f"{h['time']} {h['temp']}°" for h in hours))

    aqi = None
    if place["local"]:
        aqi_event.wait(3)
        legacy = aqi_box.get("data") or {}
        if isinstance(legacy.get("aqi"), int):
            aqi = legacy["aqi"]
            level = "优" if aqi <= 50 else ("良" if aqi <= 100 else "需要戴口罩")
            lines.append(f"空气 {aqi}（{level}）")

    main = "\n".join(lines)
    advice = _weather_advice(temp, data["text"], aqi)
    return f"{main}\n\n💡 {advice}" if advice else main


def handle_plan_day(text: str, chat_id: str | None = None) -> str:
    """日程规划：代码先把课表、天气、空教室都查好，再让 AI 组织成一段建议。

    不依赖 Agent 自主调工具——实测它只调课表就交卷了。
    """
    target = "tomorrow" if any(k in text for k in ("明天", "明日")) else "today"
    parts = []
    try:
        parts.append("【课表】\n" + handle_schedule(target))
    except Exception:
        pass
    try:
        weather = handle_weather("")
        if "暂时" not in weather:
            parts.append("【天气】\n" + weather)
    except Exception:
        pass
    try:
        rooms = handle_empty_room("ZX", "正心楼", None)
        if "空着" in rooms:
            parts.append("【空教室（正心楼）】\n" + rooms[:400])
    except Exception:
        pass

    if not parts:
        return "本鱼暂时查不到今天的安排数据。"

    has_rooms = any("【空教室" in part for part in parts)
    tail = "适合去哪自习" if has_rooms else "（没有空教室数据，不要推荐具体楼栋或教室）"
    prompt = (
        f"以下是杭电学生{'明天' if target == 'tomorrow' else '今天'}的真实数据：\n\n" + "\n\n".join(parts) +
        f"\n\n用户问：{text}\n\n"
        "用一段自然的话给他建议：什么时候有课、哪段时间空着、" + tail + "、天气适不适合出门。"
        "150 字以内。\n"
        "**严格限制：只能使用上面给出的信息。天气是晴就别说带伞，没提到的事一律不许编。**"
    )
    try:
        from app.llm.client import chat

        advice = chat(prompt, max_tokens=300)
    except Exception:
        advice = None
    return advice or "\n\n".join(parts)


def _to_minutes(text: str) -> int | None:
    """「13:30」「2026-10-28 13:30:00」→ 当天分钟数。"""
    if not text:
        return None
    match = re.search(r"(\d{1,2}):(\d{2})", str(text))
    if not match:
        return None
    return int(match.group(1)) * 60 + int(match.group(2))


def _overlap(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    return a_start < b_end and b_start < a_end


def handle_activity_conflict(text: str) -> str:
    """判断活动跟课表冲不冲突。时间比较由代码算，AI 只负责说人话。"""
    try:
        raw = list_activities(status=8, page_size=15)
        items = (raw.get("data") or {}).get("items") or []
    except Exception:
        items = []
    if not items:
        return "暂时拿不到活动列表。"

    # 找用户说的那个活动：用文本里的关键词匹配活动名
    matched = None
    for item in items:
        name = item.get("activityName") or ""
        # 用「AI」「讲座」这类词做匹配，任一词命中就算候选
        for token in ("AI", "讲座", "话剧", "夜跑", "竞赛", "分享", "宣讲", "九点半"):
            if token in text and token in name:
                matched = item
                break
        if matched:
            break

    if not matched:
        listing = "、".join((it.get("activityName") or "")[:14] for it in items[:6])
        return f"没找到你说的那个活动。当前报名中的有：{listing}…\n说全一点的名字本鱼再查。"

    name = matched.get("activityName") or ""
    a_start = _to_minutes(matched.get("activityStartTime"))
    a_end = _to_minutes(matched.get("activityEndTime")) or ((a_start or 0) + 120)

    # 活动日期落在周几
    weekday = None
    start_text = str(matched.get("activityStartTime") or "")
    match = re.search(r"\d{4}-\d{2}-\d{2}", start_text)
    if match:
        try:
            weekday = datetime.strptime(match.group(0), "%Y-%m-%d").isoweekday()
        except ValueError:
            weekday = None

    conflicts = []
    if weekday:
        try:
            courses = schedule_store.by_weekday(weekday)
        except Exception:
            courses = []
        for course in courses:
            c_start = _to_minutes(course.get("start_time"))
            c_end = _to_minutes(course.get("end_time"))
            if None in (c_start, c_end, a_start):
                continue
            if _overlap(a_start, a_end, c_start, c_end):
                conflicts.append(course)

    start_hhmm = start_text.split(" ")[-1][:5] if " " in start_text else start_text[:5]
    if conflicts:
        names = "、".join(f"{c['course']}（{c['start_time']}-{c['end_time']}）" for c in conflicts)
        return f"冲突。**{name}** {start_hhmm} 开始，那天这个时段你有课：{names}。"

    day_cn = schedule_store.WEEKDAY_CN.get(weekday, "") if weekday else ""
    if weekday:
        course_hint = "、".join(
            f"{c['course']}（{c['start_time']}-{c['end_time']}）" for c in (courses or [])[:4]
        )
        tail = f"\n那天你的课：{course_hint}" if course_hint else "\n那天没课"
    else:
        tail = ""
    return f"不冲突。**{name}** {start_hhmm} 开始，{day_cn}这个时间你没有课。{tail}"


def handle_activity_advice(user_text: str = "") -> str:
    """让 AI 从活动目录里挑出跟用户相关的几个，而不是把 10 个全倒出来。"""
    try:
        raw = list_activities(status=8, page_size=15)
    except HduhelpError as exc:
        return f"活动列表本鱼查不到：{exc}"

    data = raw.get("data") or {}
    items = data.get("items") or []
    if not items:
        return "现在没有报名中的活动。"

    want = _wanted_campus(user_text)
    if want:
        items = [item for item in items if _activity_campus(item) == want]
        if not items:
            return f"报名中的活动里没有地点在「{want}校区」的。"

    lines = []
    for item in items[:12]:
        name = item.get("activityName") or "未命名"
        start = item.get("activityStartTime") or ""
        host = item.get("hostName") or ""
        place = item.get("position") or ""
        desc = (item.get("description") or "").replace("\n", " ")[:60]
        lines.append(f"- {name} | {start} | {host} | {place} | {desc}")

    prompt = (
        "当前杭电开放报名的活动（名称 | 开始时间 | 主办方 | 地点 | 简介）：\n" + "\n".join(lines) +
        "\n\n用户是杭电电子信息类大一新生，在绍兴校区，关注技术、竞赛、AI。"
        f"他问：{user_text}\n\n"
        "**只从上面列出的活动里挑，不要提用户没问的校区，不要编造没出现的活动。**\n"
        "挑出最值得他参加的 2-3 个。每个一行，格式严格为：\n"
        "**活动名**（时间）—— 一句话为什么适合他\n\n"
        "要求：不要带主办方、不要复制上面的竖线格式、不要罗列全部、不要编造没出现的活动。"
    )
    try:
        from app.llm.client import chat

        advice = chat(prompt, max_tokens=300)
    except Exception:
        advice = None

    if not advice:
        # LLM 不可用时退回原始列表，但不能假装做了筛选
        return "AI 筛选暂时不可用，当前报名中的活动：\n" + "\n".join(lines[:5])
    return advice


def _short_source(source: str) -> str:
    first = (source or "").split("；")[0].strip()
    if not first:
        return ""
    first = first.replace("QQ群·3号楼 / ", "").replace("QQ群公告", "群公告")
    return f"—— {first}"


_NUM_RE = re.compile(r"\d+")
_TODAY_WD_RE = re.compile(r"今[天日][^。，,\n]{0,4}?周([一二三四五六日天])")
_Q_HARD = re.compile(r"[？?！!。；;\n]+")
_Q_SOFT = re.compile(r"[，,、]|\s+|以及|还有|另外|顺便|和")
# 这些是「怎么办」类问法，意图常被误判成查数据，先让本地题库接一次
_HOW_HINTS = ("怎么", "如何", "在哪", "去哪", "流程", "步骤", "怎么办", "找谁")


def _numbers_stay_put(reply: str, facts: str) -> bool:
    """回复里出现的数字必须全部能在事实里找到，防止模型改数字或编电话。"""
    return set(_NUM_RE.findall(reply)) <= set(_NUM_RE.findall(facts))


def _today_weekday_matches(reply: str, weekday_cn: str) -> bool:
    """说了「今天周X」就必须和真实星期一致，模型自己推的星期一律不可信。"""
    match = _TODAY_WD_RE.search(reply)
    if not match:
        return True
    return match.group(1).replace("天", "日") == weekday_cn


def _split_questions(text: str) -> list[str]:
    """把一句话拆成多个问题。

    先按问号/感叹号/句号/分号/换行硬切，再按逗号、顿号、空格和
    「和/以及/还有/另外/顺便」软切；软切要求每段都不少于 3 个字，
    否则当作一句话内部的停顿，保留整句。
    """
    segments: list[str] = []
    for seg in _Q_HARD.split(text or ""):
        seg = seg.strip()
        if not seg:
            continue
        parts = [part.strip() for part in _Q_SOFT.split(seg) if part.strip()]
        if len(parts) >= 2 and all(len(part) >= 3 for part in parts):
            segments.extend(parts)
        else:
            segments.append(seg)
    return [seg for seg in segments if len(seg) >= 2]


def _is_source_line(text: str) -> bool:
    value = (text or "").strip()
    if not value or len(value) > 100:
        return False
    return value.startswith("——") or "http" in value


def _faq_reply_blocks(question: str, items: list[dict]) -> list[str]:
    """多问题：模型被要求「每条一段」，代码按空行切；切不出来就退回整段。"""
    whole = _faq_reply(question, items)
    parts = [part.strip() for part in re.split(r"\n\s*\n", whole) if part.strip()]
    if len(parts) < 2:
        return [whole]
    if _is_source_line(parts[-1]):
        merged = parts.pop()
        parts[-1] = f"{parts[-1]}\n{merged}"
    return parts


def _strip_tail(reply: str) -> str:
    """去掉单段里的「你可能还想问」——分段后每段都挂一遍太啰嗦。"""
    marker = "—— 你可能还想问"
    if marker in reply:
        reply = reply.split(marker, 1)[0].rstrip()
    return reply


def split_multi_intents(text: str, chat_id: str | None = None) -> list[str]:
    """能当成多个独立问题来答的段；分不出来返回空列表。

    每段都必须能认出明确意图，否则退回整句——宁可少分段，也别把话拆碎。
    飞书、微信、卡片动作三条入口都走这一个判断。
    """
    # 写操作指令整句处理：拆开会把「修改木鱼：」前缀丢掉，后半句过不了门禁。
    # 但一条消息里每行都各自带前缀时，就是多条指令，按行拆开逐条执行。
    if "修改木鱼" in (text or ""):
        lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
        if len(lines) >= 2 and all("修改木鱼" in line for line in lines):
            return lines
        return []
    parts = _split_questions(text)
    if len(parts) < 2:
        return []
    for part in parts:
        if router.detect(part, chat_id).name == router.INTENT_UNKNOWN:
            return []
    return parts


_TEXT_INSTEAD = {
    # 发图/画图在分段里改成文本版，别把本地文件路径甩给用户
    router.INTENT_SCHEDULE_IMAGE: router.INTENT_SCHEDULE_WEEK,
    router.INTENT_EXPENSE_CHART: router.INTENT_CONSUMPTION,
}


def _answer_each(text: str, chat_id: str | None) -> list[str]:
    """一句话里几个问题都能单独认出来，就各自回答。"""
    parts = split_multi_intents(text, chat_id)
    if not parts:
        return []
    blocks: list[str] = []
    for part in parts:
        try:
            part_intent = router.detect(part, chat_id)
            text_name = _TEXT_INSTEAD.get(part_intent.name)
            if text_name:
                part_intent = router.Intent(text_name, raw=part)
            blocks.append(_strip_tail(answer(part, chat_id, intent=part_intent)))
        except Exception:
            return []
    return blocks if len(blocks) >= 2 else []


def answer_blocks(text: str, chat_id: str | None = None, intent: router.Intent | None = None) -> list[str]:
    """分段版回答：一句话问了多个问题就返回多段，其余情况只有一段。

    飞书把每段渲染成一个 markdown element；answer() 拼回整串给微信/桥接用。
    单问题时这里直接回落到 answer()，保证和原来一字不差。
    """
    if intent is None:
        intent = router.detect(text, chat_id)

    # 食堂推荐天然是一件事：「中午吃什么呢，推荐3个」别被尾巴「推荐」拆成活动推荐
    if intent.name == router.INTENT_CANTEEN:
        return [answer(text, chat_id, intent)]

    if _faq_worthy(text, intent):
        hits = _faq_multi(text)
        if len(hits) >= 2:
            blocks = _faq_reply_blocks(text, hits)
            if len(blocks) >= 2:
                if chat_id:
                    from app.agent import context

                    context.remember(chat_id, text, "faq", "\n\n".join(blocks))
                return blocks

    # 多个问题各自认得出来就分开答（今日天气 + 明日杭州天气）
    blocks = _answer_each(text, chat_id)
    if blocks:
        if chat_id:
            from app.agent import context

            context.remember(chat_id, text, "multi", "\n\n".join(blocks))
        return blocks

    return [answer(text, chat_id, intent)]


_CORRECTION_PAT = re.compile(
    r"^(不是|不对|错了|不是这样|不是的|你理解错了|答非所问|听错了|搞错了|"
    r"我不是问|我不是想问|理解偏了|不是这个意思|这不是我要的)"
)


_AUDIT_CONFIRM_PAT = re.compile(r"^确认\s*(\d+)$")
_AUDIT_IGNORE_PAT = re.compile(r"^忽略\s*(\d+)$")


def _is_audit_confirm(text: str) -> bool:
    return bool(_AUDIT_CONFIRM_PAT.match(text.strip()))


def _is_audit_ignore(text: str) -> bool:
    return bool(_AUDIT_IGNORE_PAT.match(text.strip()))


def _handle_audit_confirm(text: str, chat_id: str | None) -> str:
    index = int(_AUDIT_CONFIRM_PAT.match(text.strip()).group(1)) - 1
    try:
        from app.tools import intent_logs

        items = intent_logs.pending_audit(threshold=3)
        if index < 0 or index >= len(items):
            return "没找到这条待审核记录，先发「看纠正记录」核对一下。"
        item = items[index]
        intent_logs.resolve_audit(item["question"], item["wrong_intent"], 1)
        return f"已记住：「{item['question']}」以后按「{item['correction']}」理解。"
    except Exception:
        logger.exception("确认纠正失败")
        return "确认没成功，稍后再试一次。"


def _handle_audit_ignore(text: str, chat_id: str | None) -> str:
    index = int(_AUDIT_IGNORE_PAT.match(text.strip()).group(1)) - 1
    try:
        from app.tools import intent_logs

        items = intent_logs.pending_audit(threshold=3)
        if index < 0 or index >= len(items):
            return "没找到这条待审核记录，先发「看纠正记录」核对一下。"
        item = items[index]
        intent_logs.resolve_audit(item["question"], item["wrong_intent"], -2)
        return f"已丢弃：「{item['question']}」这条纠正不再参与判断。"
    except Exception:
        logger.exception("忽略纠正失败")
        return "忽略没成功，稍后再试一次。"


_RETRACT_PAT = re.compile(
    r"^(撤销纠正|取消纠正|撤销刚才的纠正|撤销刚才那条|撤销那条纠正|撤销刚才|刚才那条纠正|刚才说错了不算|这条不算|撤销上一条|纠正作废)"
)
_LIST_CORRECTIONS_PAT = re.compile(r"^(看纠正|纠正记录|我的纠正|看下纠正|查纠正)")
_MODIFY_CORRECTION_PAT = re.compile(
    r"^(修改纠正|改纠正|纠正改成|把纠正)\s*(\d+)\s*[，,,:：]?\s*(.+)$"
)


def _is_modify_correction(text: str) -> bool:
    return bool(_MODIFY_CORRECTION_PAT.match(text.strip()))


def _handle_modify_correction(text: str, chat_id: str | None) -> str:
    match = _MODIFY_CORRECTION_PAT.match(text.strip())
    if not match:
        return "格式是「修改纠正 1，改成的新说法」。"
    index = int(match.group(2))
    new_text = match.group(3).strip()
    try:
        from app.tools import intent_logs

        result = intent_logs.update_correction(chat_id, index, new_text)
    except Exception:
        logger.exception("修改纠正失败")
        return "改没成功，稍后再试一次。"
    if not result:
        return f"没找到第 {index} 条纠正。先发「看纠正记录」核对编号。"
    question, _ = result
    return f"第 {index} 条已改成「{new_text}」，对应问题「{question}」。"


def _is_retract(text: str) -> bool:
    return bool(_RETRACT_PAT.match(text.strip()))


def _is_list_corrections(text: str) -> bool:
    return bool(_LIST_CORRECTIONS_PAT.match(text.strip()))


def _handle_list_corrections(chat_id: str | None) -> str:
    """列出最近的纠正记录，编号从 1（最新）开始，方便按编号撤销。"""
    try:
        from app.tools import intent_logs

        conn = intent_logs.connect()
        try:
            rows = conn.execute(
                "SELECT id, question, correction, confirmed FROM intent_corrections "
                "WHERE chat_id = ? AND confirmed >= 0 ORDER BY id DESC LIMIT 10",
                (chat_id or "",),
            ).fetchall()
        finally:
            conn.close()
        if not rows:
            return "还没有纠正记录。"
        lines = ["最近的纠正记录（1 是最新）：", ""]
        for index, row in enumerate(rows, 1):
            state = "" if row["confirmed"] == 0 else "（已进规则）"
            lines.append(f"{index}. 问「{row['question']}」→ {row['correction'][:40]}{state}")
        lines += ["", "要撤哪条就说「撤销纠正 1」，全部撤就用「撤销纠正」。"]
        return "\n".join(lines)
    except Exception:
        logger.exception("查纠正记录失败")
        return "查不到纠正记录，稍后再试。"


def _handle_retract(text: str, chat_id: str | None) -> str:
    """撤销纠正。带数字就按编号撤（1 是最新），不带就撤最近一条。标记作废，不真删。"""
    try:
        from app.tools import intent_logs

        match = re.search(r"(\d+)", text)
        offset = (int(match.group(1)) - 1) if match else 0

        conn = intent_logs.connect()
        try:
            row = conn.execute(
                "SELECT id, question FROM intent_corrections "
                "WHERE chat_id = ? AND confirmed >= 0 ORDER BY id DESC LIMIT 1 OFFSET ?",
                (chat_id or "", offset),
            ).fetchone()
            if not row:
                return "没有要撤销的纠正记录。"
            conn.execute(
                "UPDATE intent_corrections SET confirmed = -1 WHERE id = ?",
                (row["id"],),
            )
            conn.commit()
        finally:
            conn.close()
        return f"撤销了对「{row['question']}」的纠正，之后不再按它判断。"
    except Exception:
        logger.exception("撤销纠正失败")
        return "撤销没成功，稍后再试一次。"


def _is_correction(text: str) -> bool:
    return bool(_CORRECTION_PAT.match(text.strip()))


def _handle_correction(text: str, chat_id: str | None) -> None:
    """把「上一条理解错了」记下来，供下次同类问法注入提示。"""
    try:
        from app.tools import intent_logs

        conn = intent_logs.connect()
        try:
            row = conn.execute(
                "SELECT question, intent FROM intent_logs WHERE chat_id = ? "
                "ORDER BY id DESC LIMIT 1",
                (chat_id or "",),
            ).fetchone()
        finally:
            conn.close()
        question = row["question"] if row else ""
        wrong = row["intent"] if row else ""
        intent_logs.record_correction(chat_id, question, wrong, text)
    except Exception:
        logger.exception("记录纠正失败")


def _explain_schedule(text: str, chat_id: str | None) -> str:
    """解释课表里某条安排的来历：先认自定义，再说学校。"""
    try:
        from app.tools import schedule_store

        for item in schedule_store.list_custom():
            name = str(item.get("course_name") or "").strip()
            if name and name in (text or ""):
                return (
                    f"「{name}」是你自己加的自定义条目，不是学校排的。"
                    f"不想要就说「修改木鱼：删掉 {name}」。"
                )
    except Exception:
        logger.exception("查自定义课表失败")
    return (
        "你当前课表里没有这条同名安排（学校课表和自定义都没有）。"
        "之前加过的话可能已经被清掉了。学校课程为什么调整，问教务处最准。"
    )


def _handle_write_instruction(text: str, chat_id: str | None) -> str | None:
    """「修改木鱼：」开头的指令直接调工具，不经模型转述。"""
    from app.agent import tools_registry

    raw = text.strip()
    if any(k in raw for k in ("课表", "上课", "教室", "晚自习", "加课", "删课")) or (
        any(k in raw for k in ("清掉", "删掉", "删除")) and "课" in raw
    ):
        return tools_registry.execute("edit_schedule", {}, raw, chat_id)
    if any(
        k in raw
        for k in ("回答", "说话", "叫", "风格", "语气", "简短", "甜", "凶", "感叹号", "人设")
    ):
        return tools_registry.execute("edit_persona", {"action": "add"}, raw, chat_id)
    return None


_CLEAR_CTX_RE = re.compile(
    r"(?:清空|清掉|清除|忘掉|忘了|删掉|删除)\s*"
    r"(?:刚才|之前|前面|上面|这些|那些|最近|说的|讲的|聊的|咱们|我们)*\s*"
    r"(?:上下文|聊天记录|聊天|对话|历史|说过的事|聊的|说的)"
    # 「把聊天记录清掉」这种宾动倒装也要认
    r"|把?\s*(?:上下文|聊天记录|历史|对话)\s*(?:清空|清掉|清除|删掉|删除|忘掉|忘了)"
)


def is_clear_context(text: str) -> bool:
    """这句话是不是「把喂给我的上下文清掉」。只当指令用，不当查询。"""
    raw = (text or "").strip()
    if len(raw) > 24:
        return False
    return bool(_CLEAR_CTX_RE.search(raw))


def handle_clear_context(chat_id: str | None) -> str:
    """删掉这个会话的对话历史与最近两轮摘要；长期记忆、提醒、课表一律不动。"""
    if not chat_id:
        return "得先知道是哪个会话，本鱼才好清。"
    from app.agent import context

    counts = context.clear(chat_id)
    return (
        "上下文清掉了。\n"
        f"对话历史删了 {counts['conversations']} 条，最近摘要删了 {counts['turn_summaries']} 条。\n\n"
        "长期记忆、提醒、课表都没动——那些不是你让本鱼忘的东西。"
    )


def _needs_deep_agent(text: str, intent) -> bool:
    """规则和 FAQ 只能答一半的复合问题，交给 SDK 接着往下查。

    不看意图名：这类问法经常被分到 unknown 或被 FAQ 接走，
    只能按文本特征判断。
    """
    raw = text or ""
    lacks_seat = any(
        k in raw for k in ("没座", "没有座", "没位置", "没有位置", "没空位", "满座")
    )
    about_seat = any(k in raw for k in ("图书馆", "座位", "自习"))
    if lacks_seat and about_seat:
        return True
    if "食堂" in raw and any(k in raw for k in ("吃完", "然后去哪", "之后去哪")):
        return True
    return False


def _faq_worthy(text: str, intent) -> bool:
    """这句话该不该先查本地题库。"""
    # 写操作指令不查题库：否则「只剩晚自习」会被图书馆开放时间接走
    if "修改木鱼" in (text or ""):
        return False
    # 需要跨工具/解释的复合问题也不查题库：否则「为什么晚自习改自习室」会被图书馆 FAQ 接走
    if _needs_deep_agent(text, intent):
        return False
    return (
        # 校园卡补办类：意图名靠不住（「校园卡丢了」会被失物招领抢走），按文本判
        (any(k in text for k in ("校园卡", "饭卡", "一卡通"))
         and any(k in text for k in ("丢", "补办", "挂失", "消磁", "充值")))
        or (intent.name == router.INTENT_BALANCE and any(k in text for k in ("丢", "补办", "挂失", "消磁")))
        or (intent.name == router.INTENT_GRADE and any(k in text for k in ("单", "证明", "打印", "开一份")))
        or intent.name == router.INTENT_UNKNOWN
        # 「怎么/如何/在哪」只在意图不明时才当操作类问题去查题库。
        # 原先是无条件生效，结果「上周校园卡开销如何」被拉去答校园卡使用说明。
        or (intent.name == router.INTENT_UNKNOWN and any(k in text for k in _HOW_HINTS))
    )


def _faq_multi(text: str) -> list[dict]:
    """一句话里问了多个问题就分别检索，最多取 4 条。

    拆开之后如果只命中同一条，说明本来就是一句话，退回单问题路径。
    """
    parts = _split_questions(text)
    if len(parts) < 2:
        return []
    from app.tools import faq

    items: list[dict] = []
    seen: set[str] = set()
    for part in parts:
        for hit in faq.search(part, limit=1):
            if hit["title"] in seen:
                continue
            seen.add(hit["title"])
            items.append(hit)
    if len(items) < 2:
        return []
    return items[:4]


def _faq_plain(items: list[dict]) -> str:
    """模型不给力时的兜底：只给答案和来源，不摆标题和原文。"""
    blocks = []
    for item in items:
        block = item["answer"]
        if item.get("detail") and len(items) == 1:
            block += f"\n{item['detail']}"
        blocks.append(block)
    text = "\n\n".join(blocks)
    source = _short_source(items[0].get("source", ""))
    return f"{text}\n\n{source}" if source else text


def _faq_reply(question: str, items: list[dict]) -> str:
    """题库命中后让模型用自己的话讲，事实锁死在条目里。

    模型只负责措辞：数字对不上、星期对不上、超时或调用失败，一律退回精简兜底。

    带 [待补] 的条目直接给原文、不过模型——实测模型会把「还没查到」改写成
    听起来确定的说法，红线不能靠它自觉。
    """
    if any("[待补]" in (item.get("answer") or "") for item in items):
        return _faq_plain(items)

    now = datetime.now()
    weekday_cn = "一二三四五六日"[now.weekday()]
    if len(items) == 1:
        item = items[0]
        facts = (
            f"标题：{item['title']}\n"
            f"答案：{item['answer']}\n"
            f"补充：{item['detail'] or '无'}"
        )
    else:
        facts = "\n".join(
            f"第 {index} 条｜{item['title']}：{item['answer']}"
            + (f"（补充：{item['detail']}）" if item.get("detail") else "")
            for index, item in enumerate(items, 1)
        )
    prompt = (
        f"同学问：{question}\n"
        f"今天是 {now:%Y-%m-%d}，星期{weekday_cn}，现在是 {now:%H:%M}。\n\n"
        f"资料如下，这是唯一可用的事实，不能补充，不能改动任何数字和电话：\n{facts}\n\n"
        "用你自己的话回答，像同学之间说话那样自然，先说结论。"
        "不要照抄原文，不要出现「出处」「原文」这类字眼，不要写成条目式标题。"
        "数字必须原样照抄：资料写 23:00 就写 23:00，不要换成「晚上11点」；"
        "写 6:30 就不要写「6点半」；电话号码要完整写出。"
        "星期几以我上面给的为准，不要自己推算，拿不准就别提。"
        "不要在开头加招呼语（比如「咚」），不要加「专注+1」这类结尾。"
        + ("资料分了几条，按顺序分开答，每条一段。" if len(items) > 1 else "总长不超过 3 行。")
    )
    try:
        from app.llm.client import chat_fast

        reply = (chat_fast(prompt, max_tokens=260, timeout=10, purpose="faq") or "").strip()
    except Exception:
        reply = ""
    if (
        reply
        and _numbers_stay_put(reply, facts)
        and _today_weekday_matches(reply, weekday_cn)
    ):
        source = _short_source(items[0].get("source", ""))
        return f"{reply}\n\n{source}" if source else reply
    return _faq_plain(items)


def _ai_tip(facts: str, hint: str = "") -> str:
    """给一条数据配一句 AI 点评。失败返回空串，不影响主回复。"""
    try:
        from app.llm.client import chat

        prompt = (
            f"真实数据：{facts}\n"
            f"{hint}\n"
            "给用户一句简短点评或建议，25 字以内。"
            "不要重复数字，不要客套。"
            "**只能用数据里明确写出的信息：数据没说温差就别提温差，没说下雨就别提带伞，没说风就别提风。**"
        )
        tip = (chat(prompt, max_tokens=80) or "").strip()
        if not tip:
            return ""
        # 点评不许出现数据里没有的天气词（实测会编「记得带伞」）
        weather_words = ("伞", "温差", "降温", "下雨", "阵雨", "风力", "冷空气")
        if any(w in tip for w in weather_words) and not any(w in (facts or "") for w in weather_words):
            return ""
        # 点评里的数字必须能在数据里找到
        for number in re.findall(r"\d+(?:\.\d+)?", tip):
            if number not in (facts or ""):
                return ""
        return tip
    except Exception:
        return ""


def _with_tip(main: str, facts: str, hint: str = "") -> str:
    tip = _ai_tip(facts, hint)
    return f"{main}\n\n💡 {tip}" if tip else main


_CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8,
           "九": 9, "十": 10, "十一": 11, "十二": 12}
_DAILY_TIME_RE = re.compile(r"(\d{1,2}|十一|十二|[一二三四五六七八九十])\s*[点:：时]\s*(\d{1,2}|半)?")


def parse_daily_time(text: str) -> tuple[int, int] | None:
    """「每天 7 点半」「早上7:30」「晚上八点」→ (hour, minute)。"""
    match = _DAILY_TIME_RE.search(text or "")
    if not match:
        return None
    raw_hour = match.group(1)
    hour = int(raw_hour) if raw_hour.isdigit() else _CN_NUM.get(raw_hour)
    if hour is None:
        return None
    raw_minute = match.group(2)
    if raw_minute == "半":
        minute = 30
    elif raw_minute and raw_minute.isdigit():
        minute = int(raw_minute)
    else:
        minute = 0
    if any(k in text for k in ("下午", "晚上", "傍晚")) and hour < 12:
        hour += 12
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute


_REPORT_SECTIONS = {
    "课表": "schedule", "课程": "schedule",
    "天气": "weather",
    "电费": "power",
    "待办": "todo", "提醒": "todo",
    "新闻": "news",
}
_ALL_SECTIONS = ("schedule", "weather", "power", "todo", "news")
_SECTION_NAMES = {"schedule": "课表", "weather": "天气", "power": "电费", "todo": "待办", "news": "新闻"}


def build_morning_report(chat_id: str) -> str:
    """早报内容：课表 + 天气 + 电费 + 今天的待办（可按订阅配置裁剪）。"""
    from app.tools import reminders, subscriptions

    now = datetime.now()
    weekday = "一二三四五六日"[now.weekday()]
    lines = [f"早上好 ☀️ 今天 周{weekday}（{now:%m-%d}）", ""]

    try:
        raw = str((subscriptions.get(chat_id) or {}).get("sections") or "").strip()
    except Exception:
        raw = ""
    enabled = set(raw.split(",")) if raw else set(_ALL_SECTIONS)

    if "schedule" in enabled:
        lines.append("【今天课表】")
        try:
            schedule = handle_schedule("today")
            body = "\n".join(schedule.split("\n")[1:]).strip()
            lines.append(body or "• 今天没课")
        except Exception:
            lines.append("• 课表暂时查不到")
        lines.append("")

    if "weather" in enabled:
        try:
            weather = handle_weather("")
            if "暂时" not in weather:
                lines += ["【天气】", weather, ""]
        except Exception:
            pass

    if "power" in enabled:
        try:
            lines += ["【电费】", handle_electric(), ""]
        except Exception:
            pass

    if "todo" in enabled:
        try:
            today = now.strftime("%Y-%m-%d")
            pending = [r for r in reminders.pending(chat_id) if r["remind_at"].startswith(today)]
            if pending:
                lines.append("【今天待办】")
                for item in pending:
                    lines.append(f"• {item['remind_at'][11:]} {item['content']}")
        except Exception:
            pass

    return "\n".join(lines).strip()


_NEWS_STOP = re.compile(
    r"(今天|明天|最近|有什么|有啥|新闻|头条|资讯|热点|看看|看一下|来一份|来份|来点|"
    r"发一份|发个|给我|要一份|整一份|有关|关于|相关|活动|的|吗|呢|？|。|，|,|\s)"
)


def _web_news(keyword: str, official_hint: str = "", limit: int = 5) -> list[dict]:
    from app.tools import news as news_store

    try:
        from app.llm.client import web_search
    except Exception:
        return []
    seen: set[str] = set()
    official: list[dict] = []
    others: list[dict] = []
    for query in (f"{keyword} 官方", f"{keyword} 新闻"):
        for item in web_search(query, 5) or []:
            title = str(item.get("title") or "").strip()
            link = str(item.get("link") or item.get("url") or "").strip()
            if not title or title in seen:
                continue
            seen.add(title)
            ok = news_store.is_official_link(link, official_hint or keyword)
            row = {"t": title, "l": link, "s": "官方" if ok else "非官方"}
            (official if ok else others).append(row)
        if len(official) >= 2:
            break
    return (official + others)[:limit]


def news_data(text: str = "", chat_id: str | None = None) -> dict:
    """新闻统一取数：品牌优先；热搜单列；关键词优先本地，没命中走联网；否则按分类浏览。"""
    from app.tools import brand_sites, news

    raw = text or ""
    if "热搜" in raw:
        return {"mode": "hot", "items": [{"t": h, "l": ""} for h in news.baidu_hot(10)]}

    # 品牌优先于停用词：「今日头条」「来一份」这类词可能被当口语剥掉
    brand = brand_sites.find_brand(raw)
    if brand:
        items = [{"t": f"{brand['name']} 官网", "l": brand["site"], "s": "官方", "o": True}]
        if brand.get("news"):
            items.append({"t": f"{brand['name']} 新闻中心", "l": brand["news"], "s": "官方", "o": True})
        items += news.official_posts(brand["key"]) or []
        topic = raw
        for token in (brand["name"], brand["key"]):
            topic = topic.replace(token, "")
        topic = re.sub(
            r"(新闻|官网|官方|发布会|发布|看看|一下|最近|有关|关于|的|吗|呢|？|。|，|,|\s)",
            "",
            topic,
        ).strip()
        hits: list[dict] = []
        if topic:
            for it in news.search_items(topic):
                blob = str(it.get("t") or "") + str(it.get("d") or "")
                if brand["name"] in blob or brand["key"] in blob.lower():
                    hits.append(it)
        else:
            hits = news.search_items(brand["key"]) + news.search_items(brand["name"])
        items += hits
        query = f"{brand['name']} {topic}".strip()
        if not hits:
            items += _web_news(query, brand["key"])
        label = f"{brand['name']}{(' ' + topic) if topic else ''}"
        return {"mode": "official", "keyword": label, "items": items[:6]}

    only = [c for c in news.CATEGORIES if c in raw]
    kw = raw
    for c in news.CATEGORIES:
        kw = kw.replace(c, "")
    # 带「官网/官方/发布」时按专名查，不把口语词当噪声剥掉
    if any(k in raw for k in ("官网", "官方", "发布", "发布会")):
        kw = re.sub(r"(官网|官方|发布会|发布|新闻|一下|吗|呢|？|。|，|,|\s)", "", kw).strip()
    else:
        kw = _NEWS_STOP.sub("", kw).strip()

    if kw:
        posts = news.official_posts(kw)
        if posts:
            return {"mode": "official", "keyword": kw, "items": posts}
        hits = news.search_items(kw)
        if hits:
            return {"mode": "local", "keyword": kw, "items": hits}
        return {"mode": "web", "keyword": kw, "items": _web_news(kw)}

    if only and chat_id:
        for c in only:
            news.bump_like(chat_id, c)
    prefer = news.prefer_categories(chat_id)
    return {
        "mode": "browse",
        "only": only,
        "prefer": prefer,
        "groups": news.collect(per_category=2, prefer=prefer, only=only or None, blocked=news.get_blocked(chat_id)),
        "hot": [] if only else news.baidu_hot(8),
        "campus": [] if only else news.campus_notices(limit=3),
    }


def handle_news(text: str = "", chat_id: str | None = None) -> str:
    """新闻：关键词优先本地、其次联网；不指定就按分类浏览。"""
    from app.tools import news

    data = news_data(text, chat_id)
    if data["mode"] == "hot":
        if not data["items"]:
            return "热搜暂时拉不到，过一会儿再问本鱼。"
        return "\n".join(["【百度热搜】", *[it["t"] for it in data["items"]]])
    if data["mode"] in ("official", "local", "web"):
        if not data["items"]:
            return f"没找到「{data['keyword']}」相关的新闻，换个说法试试。"
        label = {
            "official": f"{data['keyword']} · 官方发布",
            "local": f"{data['keyword']} · 站内新闻",
            "web": f"{data['keyword']} · 联网搜索（非官方）",
        }[data["mode"]]
        lines = [f"【{label}】"]
        for it in data["items"]:
            mark = f"（{it['s']}）" if data["mode"] == "web" and it.get("s") else ""
            lines.append(f"· {it['t']}{mark}")
            if it.get("l"):
                lines.append(f"  {it['l']}")
            if it.get("d"):
                lines.append(f"  {it['d'][:80]}")
        return "\n".join(lines)

    lines: list[str] = []
    if data["campus"]:
        lines += ["【校内通知】", *data["campus"], ""]
    if data["hot"]:
        lines += ["【热搜】", *data["hot"], ""]
    lines += news.format_grouped(data["groups"], per_category=3 if data["prefer"] else 2)
    if not lines:
        return "新闻源暂时拉不到，过一会儿再问本鱼。"
    return "\n".join(lines).strip()


def morning_news_card(chat_id: str | None = None):
    """早报第二条：新闻卡片。news 被关掉或没内容时返回 None。"""
    from app.feishu import cards
    from app.tools import news, subscriptions

    try:
        raw = str((subscriptions.get(chat_id) or {}).get("sections") or "").strip()
    except Exception:
        raw = ""
    enabled = set(raw.split(",")) if raw else set(_ALL_SECTIONS)
    if "news" not in enabled:
        return None
    prefer = news.prefer_categories(chat_id)
    groups = news.collect(per_category=1, prefer=prefer)
    hot = news.baidu_hot(5)
    campus = news.campus_notices(limit=3)
    if not groups and not hot and not campus:
        return None
    card = cards.build_news(groups, hot, campus, title="今日新闻")
    lines: list[str] = []
    if campus:
        lines += ["【校内通知】", *campus, ""]
    if hot:
        lines += ["【热搜】", *hot, ""]
    lines += news.format_grouped(groups, per_category=1)
    return card, "\n".join(lines).strip() or "今日新闻见卡片"


def handle_news_pref(text: str = "", chat_id: str | None = None) -> str:
    if not chat_id:
        return "只在飞书里可用。"
    from app.agent import schedule_edit
    from app.tools import news

    text = schedule_edit.strip_keyword(text)
    picked = [c for c in news.CATEGORIES if c in (text or "")]
    if not picked:
        likes = news.get_prefs(chat_id)
        blocked = news.get_blocked(chat_id)
        parts = []
        if likes:
            parts.append("多推：" + "、".join(likes))
        if blocked:
            parts.append("不发：" + "、".join(blocked))
        if not parts:
            return (
                "还没设新闻偏好。可选：" + "、".join(news.CATEGORIES) + "。\n"
                "说「修改木鱼：多来点科技」或「修改木鱼：不要国内」。"
            )
        return (
            "新闻设置：" + "；".join(parts) + "。\n"
            "要改就说「修改木鱼：多来点科技」或「修改木鱼：不要国内」。"
        )
    if any(k in text for k in ("不要", "少推", "别推", "不喜欢", "去掉", "屏蔽")):
        news.block_category(chat_id, picked)
        return "好的，以后新闻里不再发：" + "、".join(picked) + "。"
    news.unblock_category(chat_id, picked)
    news.like_category(chat_id, picked)
    return "新闻偏好设成：" + "、".join(picked) + "。以后这几类会多推。"


def news_more(chat_id: str | None, category: str, limit: int = 5) -> str:
    """点「多来点X」之后真的补发几条该类新闻。"""
    from app.tools import news

    groups = news.collect(per_category=limit, only=[category], blocked=[])
    items = groups.get(category) or []
    if not items:
        return f"「{category}」类 24 小时内没有更多新闻了，过会儿再点。"
    lines = [f"【{category}】"]
    for it in items:
        src = str(it.get("s") or "")
        lines.append(f"· {it['t']}（{src}）" if src else f"· {it['t']}")
        if it.get("l"):
            lines.append(f"  {it['l']}")
    return "\n".join(lines)


def handle_morning_report(text: str, chat_id: str | None = None) -> str:
    if not chat_id:
        return "早报只在飞书里能用（需要知道把消息推回哪个会话）。"
    from app.agent import schedule_edit
    from app.tools import subscriptions

    text = schedule_edit.strip_keyword(text)

    if any(k in text for k in ("关掉", "关闭", "取消", "别发", "停止", "停掉")):
        subscriptions.disable(chat_id)
        return "早报已关掉。想再开就说「每天 7 点半发早报」。"
    if any(k in text for k in ("现在", "马上", "立即", "来一份", "发一份")):
        return build_morning_report(chat_id)

    mentioned = {key for word, key in _REPORT_SECTIONS.items() if word in text}
    if mentioned:
        current = subscriptions.get(chat_id) or {}
        raw = str(current.get("sections") or "").strip()
        current_set = set(raw.split(",")) if raw else set(_ALL_SECTIONS)
        if any(k in text for k in ("只发", "只要", "只保留", "只看", "仅")):
            new_set = mentioned
        elif any(k in text for k in ("去掉", "不要", "别发", "删掉", "取消")):
            new_set = current_set - mentioned
        else:
            new_set = current_set | mentioned
        if not new_set:
            return "早报不能什么都不发，至少留一项。比如「早报只发课表和天气」。"
        subscriptions.set_sections(chat_id, ",".join(s for s in _ALL_SECTIONS if s in new_set))
        return "早报内容改成：" + "、".join(_SECTION_NAMES[s] for s in _ALL_SECTIONS if s in new_set) + "。"

    if "内容" in text:
        current = subscriptions.get(chat_id) or {}
        raw = str(current.get("sections") or "").strip()
        cur = set(raw.split(",")) if raw else set(_ALL_SECTIONS)
        return (
            "早报现在发：" + "、".join(_SECTION_NAMES[s] for s in _ALL_SECTIONS if s in cur) + "。\n"
            "要改就说「早报只发课表和天气」或「早报去掉电费」。"
        )

    parsed = parse_daily_time(text)
    if parsed is None and any(k in text for k in ("吗", "能不能", "可以", "怎么", "如何", "几点")):
        current = subscriptions.get(chat_id)
        if current and current.get("enabled"):
            now_text = f"现在是每天 {current['hour']:02d}:{current['minute']:02d}"
        else:
            now_text = "现在还没开启"
        return (
            f"早报{now_text}。\n"
            "要改时间直接说「早报改到 6:50」，比如「修改木鱼：早报改到 6:50」。"
        )
    hour, minute = parsed or (7, 30)
    subscriptions.set_time(chat_id, hour, minute)
    return (
        f"早报已设置：每天 {hour:02d}:{minute:02d}\n"
        "内容：今日课表 + 天气 + 电费 + 当天待办\n"
        "云端 24 小时在线，到点直接推。\n"
        "想关掉说「关掉早报」。"
    )


def handle_daily_report(text: str = "", chat_id: str | None = None) -> str:
    """全天报告：把课表、天气、电费、长跑、空教室汇总成一段。"""
    parts: list[str] = []

    try:
        parts.append("【课表】\n" + handle_schedule("today"))
    except Exception:
        pass
    try:
        weather = handle_weather("")
        if "暂时" not in weather:
            parts.append("【天气】\n" + weather)
    except Exception:
        pass
    try:
        parts.append("【电费】\n" + handle_electric())
    except Exception:
        pass
    try:
        parts.append("【阳光长跑】\n" + handle_sunrun())
    except Exception:
        pass
    try:
        rooms = handle_empty_room("ZX", "正心楼", None)
        if "空着" in rooms:
            parts.append("【空教室】\n" + rooms[:300])
    except Exception:
        pass

    if not parts:
        return "今天的数据本鱼暂时都查不到。"

    has_rooms = any("【空教室】" in part for part in parts)
    tail = "有空可以去哪自习。" if has_rooms else "**不要在结尾推荐任何自习地点。**"
    prompt = (
        "以下是杭电学生今天的真实数据：\n\n" + "\n\n".join(parts) +
        "\n\n用一段话给他一份「今日速览」：有什么课、天气怎么样、电费够不够、"
        "长跑进度、" + tail + "200 字以内。\n"
        "**只能用上面给出的数据，没提到的事一律不许编。**\n"
        "**课表里每节课都标了「上午/下午/晚上」，照标好的说，不要自己判断时段。**\n"
        "**课表里的教室是他上课的地方，绝不能说成自习地点。**"
    )
    try:
        from app.llm.client import chat

        summary = chat(prompt, max_tokens=400)
    except Exception:
        summary = None
    return summary or "\n\n".join(parts)


def handle_lostfound(text: str = "") -> str:
    """查校园失物招领。有搜索词就带上关键词。"""
    from app.tools.hduhelp import run_cli

    keyword = ""
    for marker in ("找", "查", "搜"):
        if marker in text:
            tail = text.split(marker, 1)[1].strip(" 的了吗")
            if tail and len(tail) <= 12:
                keyword = tail
                break

    args = ["campuslife", "lost-found", "items", "--page", "1"]
    if keyword:
        args += ["--keyword", keyword]

    try:
        payload = run_cli(*args)
    except HduhelpError as exc:
        message = str(exc)
        if "权限" in message or "MISSING_SCOPE" in message:
            return "失物招领需要额外授权，在电脑上双击 `★3 重新授权.lnk`。"
        if "服务授权" in message or "40920" in message:
            return "失物招领需要一次「服务授权」：杭电助手 → 我的/设置 → 服务授权 → 失物招领。"
        raise

    data = payload.get("data") or {}
    items = data.get("list") or data.get("items") or []
    if not items:
        scope = f"（关键词：{keyword}）" if keyword else ""
        return f"没找到失物记录{scope}。"

    lines = [f"校园失物（{len(items)} 条）" + (f" · 关键词「{keyword}」" if keyword else "")]
    for item in items[:8]:
        name = item.get("name") or item.get("title") or "未命名物品"
        category = (item.get("category") or {}).get("name") or ""
        scene = (item.get("scene") or {}).get("name") or item.get("place") or ""
        when = str(item.get("pickedAt") or "")[:16]
        cabinet = item.get("cabinetID")
        title = f"{name}（{category}）" if category else name
        meta_parts = [x for x in (when, scene) if x]
        if cabinet:
            meta_parts.append(f"取物柜 {cabinet}")
        meta = " · ".join(meta_parts)
        lines.append(f"• {title}" + (f"\n  {meta}" if meta else ""))
    lines.append("\n丢东西的话，去「杭电助手」的失物招领发布。")
    return "\n".join(lines)


def handle_study_plan(text: str = "") -> str:
    """学习计划：基于本周课表的空档 + 课程清单，给出复习安排。

    不依赖考试接口（学校把考试查询关了），只用课表事实。
    """
    try:
        items = schedule_store.this_week()
        courses = schedule_store.all_courses()
    except Exception:
        return "课表数据取不到，先在电脑上运行 py -3 scripts/cache_schedule.py"

    if not items:
        return "本周没课，随便安排。"

    # 每天有课的节次
    busy: dict[int, set[int]] = {}
    for item in items:
        busy.setdefault(item["day"], set()).update(
            range(item["start_section"], item["end_section"] + 1)
        )

    all_sections = set(range(1, 12))
    lines = []
    for day in range(1, 8):
        used = busy.get(day, set())
        free = sorted(all_sections - used)
        if not used and not free:
            continue
        day_name = schedule_store.WEEKDAY_CN.get(day, f"周{day}")
        if not used:
            lines.append(f"{day_name}：全天没课")
            continue
        if not free:
            lines.append(f"{day_name}：满课")
            continue
        # 压缩连续节次
        blocks = []
        start = prev = free[0]
        for sec in free[1:]:
            if sec == prev + 1:
                prev = sec
                continue
            blocks.append((start, prev))
            start = prev = sec
        blocks.append((start, prev))
        free_text = "、".join(f"第{a}-{b}节" if a != b else f"第{a}节" for a, b in blocks)
        lines.append(f"{day_name}：有课 {sorted(used)}，空闲 {free_text}")

    course_names = "、".join(c["course"] for c in courses[:12])
    facts = "\n".join(lines)
    prompt = (
        "这是杭电学生本周的课程与空闲安排：\n" + facts +
        f"\n\n他这学期在修：{course_names}\n"
        f"他问：{text or '帮我做个复习计划'}\n\n"
        "给一份本周复习安排，挑 3-4 个具体空档。\n"
        "**每行格式严格为：周几第几节 —— 复习哪门 —— 具体做什么**\n"
        "（例：周四第4-5节 —— 线性代数 —— 刷两节矩阵题，把作业里卡住的那道补上）\n"
        "第三段必须写具体动作（刷题/背单词/敲代码/画图），不许写「利用空闲时间复习」这种空话。\n"
        "不要写「周一：」「安排如下：」这类前缀，不要编号，不要编造考试时间。150 字以内。"
    )
    try:
        from app.llm.client import chat

        plan = chat(prompt, max_tokens=350)
    except Exception:
        plan = None
    if plan:
        return plan
    return "本周空闲安排：\n" + facts


_RANGE_RE = re.compile(r"(\d{1,2})\s*[月./-]\s*(\d{1,2})")
_WEEKDAY_CN = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "日": 7, "天": 7}
_WEEKDAY_RE = re.compile(r"(上上|上|这|本|下)?(?:个)?(?:周|星期|礼拜)\s*([一二三四五六日天])")


def _weekday_date(raw: str, today: datetime) -> str | None:
    """「这周三」「上周三」「下周三」→ 具体某一天。"""
    match = _WEEKDAY_RE.search(raw)
    if not match:
        return None
    prefix = match.group(1) or ""
    weekday = _WEEKDAY_CN[match.group(2)]
    monday = today - timedelta(days=today.weekday())
    if prefix == "上":
        monday -= timedelta(days=7)
    elif prefix == "上上":
        monday -= timedelta(days=14)
    elif prefix == "下":
        monday += timedelta(days=7)
    return (monday + timedelta(days=weekday - 1)).strftime("%Y-%m-%d")


def parse_date_range(text: str) -> tuple[str, str] | None:
    """从「11月5号到11月10号」「今天」「上周三」「上个月」这类说法里取日期范围。

    返回 (YYYY-MM-DD, YYYY-MM-DD)；认不出返回 None，交给默认的最近 7 天。
    """
    raw = text or ""
    today = datetime.now()
    if not _RANGE_RE.search(raw):
        if "前天" in raw:
            day = (today - timedelta(days=2)).strftime("%Y-%m-%d")
            return day, day
        if "昨天" in raw:
            day = (today - timedelta(days=1)).strftime("%Y-%m-%d")
            return day, day
        if "今天" in raw:
            day = today.strftime("%Y-%m-%d")
            return day, day
        weekday_day = _weekday_date(raw, today)
        if weekday_day:
            return weekday_day, weekday_day
        monday = today - timedelta(days=today.weekday())
        if any(word in raw for word in ("上周", "上星期", "上礼拜")):
            return (
                (monday - timedelta(days=7)).strftime("%Y-%m-%d"),
                (monday - timedelta(days=1)).strftime("%Y-%m-%d"),
            )
        if any(word in raw for word in ("这周", "本周", "这星期", "这礼拜")):
            return monday.strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d")
    pairs = [(int(m.group(1)), int(m.group(2))) for m in _RANGE_RE.finditer(raw)]
    if len(pairs) >= 2:
        (month1, day1), (month2, day2) = pairs[0], pairs[1]
    elif len(pairs) == 1:
        month1, day1 = pairs[0]
        tail = re.search(r"(?:到|至|~|—)\s*(\d{1,2})\s*[日号]?", raw)
        if not tail:
            return None
        month2, day2 = month1, int(tail.group(1))
    else:
        days = re.findall(r"(\d{1,2})\s*[日号]", raw)
        if len(days) < 2:
            return None
        month1 = month2 = today.month
        day1, day2 = int(days[0]), int(days[1])
    try:
        start = datetime(today.year, month1, day1)
        end = datetime(today.year, month2, day2)
    except ValueError:
        return None
    if end < start:
        try:
            end = end.replace(year=today.year + 1)
        except ValueError:
            return None
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


def parse_month_range(text: str) -> tuple[str, str] | None:
    """「上个月/上月」取上月整月，「这个月/本月」取本月 1 号到今天。"""
    raw = text or ""
    today = datetime.now()
    if any(word in raw for word in ("上个月", "上月", "上一个月")):
        last_prev = today.replace(day=1) - timedelta(days=1)
        first_prev = last_prev.replace(day=1)
        return first_prev.strftime("%Y-%m-%d"), last_prev.strftime("%Y-%m-%d")
    if any(word in raw for word in ("这个月", "本月", "这月", "当月")):
        return today.replace(day=1).strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d")
    return None


def handle_consumption(
    chat_id: str | None = None,
    month: bool = False,
    start: str | None = None,
    end: str | None = None,
) -> str:
    """校园卡消费分析：按天汇总 + 找高频地点。month / start+end 用来限定时间。"""
    from app.tools.hduhelp import run_cli

    try:
        data = run_cli("campuslife", "card", "consume").get("data") or {}
    except HduhelpError as exc:
        return f"消费流水本鱼查不到：{exc}"

    items = data.get("list") or []
    month_start = datetime.now().strftime("%Y-%m-01") if (month and not start) else ""
    spends = []
    for item in items:
        fee = item.get("totalFee")
        if not isinstance(fee, (int, float)) or fee >= 0:
            continue
        created = str(item.get("createdAt") or "")
        day = created.split(" ")[0] if " " in created else created
        if month_start and day and day < month_start:
            continue
        if start and day and day < start:
            continue
        if end and day and day > end:
            continue
        place = str(item.get("deviceName") or "").strip()
        # 接口里混着纯数字的档口名（如 "14"），拿来当店名没法看
        if place.isdigit():
            place = f"{place} 号机"
        spends.append({
            "day": day,
            "amount": abs(float(fee)),
            "place": place,
            "time": created,
        })

    if chat_id and spends:
        from app.tools import records

        records.log_expenses(chat_id, spends)

    summary = None
    if month or (start and end):
        # 按月/按区间时优先和图表用同一份库数据，避免文字说 9 笔、图里却画了 8 天
        from app.tools import records as records_tool

        begin = start or datetime.now().strftime("%Y-%m-01")
        candidate = records_tool.range_summary(chat_id or "", begin, end)
        if candidate["count"]:
            summary = candidate

    if not summary and not (month or (start and end)) and not spends:
        return "最近没有消费记录。"

    if not summary and (month or (start and end)):
        if start and end:
            return f"{start[5:]} 到 {end[5:]} 没有消费记录。"
        return "本月还没有消费记录。"

    if summary:
        count = summary["count"]
        total = summary["total"]
        days_sorted = summary["by_day"]
        places_sorted = summary["by_place"]
        biggest_amount, biggest_place = summary["biggest"][1], summary["biggest"][0]
        scope = f"{start[5:]} 到 {end[5:]}" if start and end else "本月"
    else:
        total = sum(s["amount"] for s in spends)
        by_day: dict[str, float] = {}
        place_stat: dict[str, list] = {}
        for s in spends:
            by_day[s["day"]] = by_day.get(s["day"], 0) + s["amount"]
            if s["place"]:
                stat = place_stat.setdefault(s["place"], [0, 0.0])
                stat[0] += 1
                stat[1] += s["amount"]
        days_sorted = sorted(by_day.items(), reverse=True)
        places_sorted = sorted(
            [(name, stat[0], stat[1]) for name, stat in place_stat.items()],
            key=lambda x: (-x[1], -x[2]),
        )
        biggest = max(spends, key=lambda s: s["amount"])
        biggest_amount, biggest_place = biggest["amount"], biggest["place"]
        scope = "最近"
        count = len(spends)

    lines = [f"{scope} {count} 笔消费，共 **{total:.2f}** 元"]
    if days_sorted:
        lines.append("按天：" + "、".join(f"{d} {v:.2f} 元" for d, v in days_sorted[:31]))
    if places_sorted:
        lines.append("常去：" + "、".join(f"{p} {n} 次 {a:.2f} 元" for p, n, a in places_sorted[:3]))
    lines.append(f"最大一笔：{biggest_amount:.2f} 元（{biggest_place or '未知地点'}）")

    facts = "\n".join(lines[1:])
    prompt = (
        f"杭电学生最近的校园卡消费：\n{facts}\n\n"
        "用木鱼的口吻点评两句：先说花得多不多、集中在哪，再给一句实在的提醒。"
        "如果地点是「21 号机」这类机台编号，就说「某几台机器」，不要照抄编号，也别写成在那吃饭。"
        "45 字以内，可以有语气词但不要说教，不要编造数据里没有的金额。"
    )
    try:
        from app.llm.client import chat

        tip = chat(prompt, max_tokens=80)
    except Exception:
        tip = None
    return "\n".join(lines) + (f"\n\n💡 {tip.strip()}" if tip else "")


def _consumption_reply(text: str, chat_id: str | None) -> str:
    """文字版消费分析：认「今天/昨天」「上个月」「几号到几号」，都不认就最近。"""
    span = parse_date_range(text) or parse_month_range(text)
    start, end = span if span else (None, None)
    return handle_consumption(chat_id, start=start, end=end)


def sync_consumption() -> int:
    """把最近的校园卡流水落库，不做展示。定时任务用它补齐历史。"""
    from app.tools import records
    from app.tools.hduhelp import run_cli

    try:
        data = run_cli("campuslife", "card", "consume").get("data") or {}
    except Exception:
        return 0

    spends = []
    for item in data.get("list") or []:
        fee = item.get("totalFee")
        if not isinstance(fee, (int, float)) or fee >= 0:
            continue
        created = str(item.get("createdAt") or "")
        day = created.split(" ")[0] if " " in created else created
        place = str(item.get("deviceName") or "").strip()
        if place.isdigit():
            place = f"{place} 号机"
        spends.append({
            "day": day,
            "amount": abs(float(fee)),
            "place": place,
            "time": created,
        })
    if not spends:
        return 0

    # 公账一份：以前按会话各存一份，同一张卡微信查 9 笔、飞书查 5 笔
    return records.log_expenses("sync", spends)


def handle_electric(chat_id: str | None = None) -> str:
    from app.tools.hduhelp import run_cli
    from app.tools.hduhelp import HduhelpError

    try:
        data = run_cli("campuslife", "electric", "balance").get("data") or {}
    except HduhelpError as exc:
        message = str(exc)
        if "服务授权" in message or "40920" in message:
            return "宿舍电费需要一次「服务授权」：杭电助手 → 我的/设置 → 服务授权 → 电费。"
        if "权限" in message or "MISSING_SCOPE" in message:
            return "宿舍电费需要额外授权，在电脑上双击 `★3 重新授权.lnk`。"
        raise
    if not data:
        return "本鱼暂时查不到电费。"

    balance = data.get("balance")
    room = data.get("roomName") or ""
    lines = ["宿舍电费"]
    if balance is not None:
        lines.append(f"余额：**{float(balance):.2f}** 元")
    if room:
        lines.append(f"寝室：{room}")
    # 够不够用按阈值算，不交给模型判断——实测它看到 474 元也说「余额不足」
    if balance is not None:
        amount = float(balance)
        if amount < 20:
            lines.append("⚠️ 余额告急，尽快充值")
        elif amount < 50:
            lines.append("⚠️ 余额不多了，记得充值")
        else:
            lines.append("余额够用，不用急着充")

    # 学校按周期结算，不是每天扣钱；用真实扣费记录，别说成「近 7 天」
    try:
        history = run_cli("campuslife", "electric", "history").get("data") or {}
        payments: list[tuple[str, float]] = []
        for item in history.get("history") or []:
            try:
                change = float(item.get("change"))
            except (TypeError, ValueError):
                continue
            if change > 0:
                payments.append((str(item.get("time") or ""), change))
        payments.sort(key=lambda p: p[0], reverse=True)
        if payments:
            last_day, last_amount = payments[0]
            lines.append(f"最近一次扣费：{last_amount:.2f} 元（{last_day}）")
            cutoff = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
            window = [p for p in payments if p[0] >= cutoff]
            if len(window) > 1:
                lines.append(f"近 30 天结算 {len(window)} 次，共 {sum(amount for _, amount in window):.2f} 元")
        lines.append("学校按周期结算，不是每天扣钱，某天余额没变是正常的")
    except Exception:
        pass
    return "\n".join(lines)


def handle_mail() -> str:
    from app.tools import mail as mail_tool

    try:
        data = mail_tool.unread(limit=5)
    except RuntimeError as exc:
        return f"邮箱没配好：{exc}"
    except Exception as exc:
        return f"邮箱暂时连不上：{type(exc).__name__}"

    unread_count = data.get("unread", 0)
    total = data.get("total", 0)
    items = data.get("items") or []

    lines = [f"杭电邮箱（{total} 封邮件）"]
    if unread_count:
        lines.append(f"**{unread_count} 封未读**")
        for item in items:
            lines.append(f"• {item['from'][:30]}\n  {item['subject'][:40]}\n  {item['date'][:22]}")
    else:
        lines.append("没有未读邮件。")
        try:
            recent_data = mail_tool.recent(limit=3)
            recent_items = recent_data.get("items") or []
            if recent_items:
                lines.append("\n最近 3 封：")
                for item in recent_items:
                    flag = "● " if item.get("unread") else "  "
                    lines.append(f"{flag}{item['subject'][:32]} — {item['from'][:20]}")
        except Exception:
            pass
    return "\n".join(lines)


def handle_remind(text: str, chat_id: str | None = None) -> str:
    """解析「明天8点提醒我交作业」→ 存提醒 → 到点推送。"""
    if not chat_id:
        return "提醒功能只在飞书里可用（需要知道推送到哪个会话）。"

    from app.agent import schedule_edit
    from app.tools import memory, reminders

    text = schedule_edit.strip_keyword(text)

    now = datetime.now()
    prompt = (
        f"当前时间：{now:%Y-%m-%d %H:%M}（周{'一二三四五六日'[now.weekday()]}）\n"
        f"用户说：{text}\n\n"
        "提取提醒内容和提醒时间。只输出 JSON，不要解释：\n"
        '{"content":"要提醒的事","remind_at":"YYYY-MM-DD HH:MM"}\n\n'
        "规则：\n"
        "- 没说明确时间就返回 {\"content\":\"\",\"remind_at\":\"\"}\n"
        "- 时间必须晚于当前时间\n"
        "- 内容去掉「提醒我」这类前缀，只留核心事项"
    )
    raw = None
    try:
        from app.llm.client import chat

        raw = chat(prompt, max_tokens=120)
    except Exception:
        raw = None

    parsed = None
    if raw:
        import json

        match = re.search(r"\{.*\}", raw, re.S)
        if match:
            try:
                parsed = json.loads(match.group(0))
            except json.JSONDecodeError:
                parsed = None

    content = str((parsed or {}).get("content") or "").strip()
    if not content:
        # 模型没抽到事项时代码兜底：时间已经能自己算，剩下的剥掉「提醒我」就是要做的事
        content = re.sub(
            r"(提醒我|提醒一下|叫我一声|叫我|记得提醒|别忘了提醒|通知我|提醒)",
            "",
            memory.strip_time_phrase(text),
        ).strip(" ，,。！!")
    if not content:
        return "没听清要提醒什么。换个说法试试，比如「明天早上 8 点提醒我交作业」。"

    # 时间优先用代码算：模型没听到具体几点时会填 00:00
    relative = memory.parse_relative_at(text)
    code_time = "" if relative else memory.parse_remind_at(text)
    if relative:
        when = relative
    elif code_time:
        when = datetime.strptime(code_time, "%Y-%m-%d %H:%M")
    else:
        raw_time = str((parsed or {}).get("remind_at") or "").strip()
        if not raw_time:
            return "没听清要什么时候提醒你。换个说法试试，比如「明天早上 8 点提醒我交作业」。"
        try:
            when = datetime.strptime(raw_time, "%Y-%m-%d %H:%M")
        except ValueError:
            return "时间没解析对，换个说法试试，比如「明天早上 8 点提醒我交作业」。"
        if when.hour == 0 and when.minute == 0 and not re.search(r"(0|零|十二)\s*[点时]|半夜|凌晨|午夜", text):
            when = when.replace(hour=9)

    if when <= now:
        return f"这个时间（{when:%m-%d %H:%M}）已经过了，换一个将来的时间吧。"

    content = content[:60]
    reminder_id = reminders.add(chat_id, content, when)
    return (
        f"已设提醒（编号 {reminder_id}）\n"
        f"时间：{when:%m-%d %H:%M}\n"
        f"内容：{content}\n\n"
        "云端一直在线，到点直接推给你。"
    )


def handle_semester_report(chat_id: str | None = None) -> str:
    """学期校园生活报告：全从库里算，没数据就写暂无。"""
    from collections import Counter

    from app.tools import records

    lines = ["**这学期的校园生活**", ""]

    # 课节数（整学期课表缓存，一条 = 一节）
    try:
        conn = schedule_store.connect()
        total_sections = conn.execute("SELECT COUNT(*) FROM schedule").fetchone()[0]
        course_count = conn.execute("SELECT COUNT(DISTINCT course_name) FROM schedule").fetchone()[0]
        rows = conn.execute("SELECT location FROM schedule").fetchall()
        conn.close()
        building = Counter()
        for row in rows:
            # classroom.connect 返回的连接没有 row_factory，取下标不是键
            location = str(row[0] or "")
            match = re.match(r"([\u4e00-\u9fa5]{2,4}楼)", location)
            if match:
                building[match.group(1)] += 1
    except Exception:
        total_sections, course_count, building = 0, 0, Counter()

    if total_sections:
        lines.append(f"· 课表上共 **{total_sections}** 节，{course_count} 门课")
        if building:
            top_building, top_count = building.most_common(1)[0]
            lines.append(f"· 去得最多的楼：**{top_building}**（{top_count} 节）")

    # 饭卡花销（只有用户真查过消费才有数据）
    try:
        summary = records.expense_summary(chat_id) if chat_id else {"count": 0}
    except Exception:
        summary = {"count": 0}
    if summary.get("count"):
        lines.append(f"· 饭卡累计 **{summary['total']:.2f}** 元，{summary['count']} 笔")
        top = summary.get("top_places") or []
        if top:
            lines.append("· 常去：" + "、".join(f"{name} {amount:.0f} 元" for name, amount in top[:3]))
    else:
        lines.append("· 饭卡花销：**暂无数据**（先问本鱼一次「我这个月花了多少」，之后才有累计）")

    # 长跑
    try:
        data = get_sunrun_overview() or {}
        times = data.get("validTimes") or 0
        mileage = data.get("validMileages") or 0
        if times:
            lines.append(f"· 阳光长跑 **{times}** 次，共 **{float(mileage) / 1000:.2f}** km")
        else:
            lines.append("· 阳光长跑：还没有有效记录")
    except Exception:
        lines.append("· 阳光长跑：暂时查不到")

    if len(lines) <= 2:
        return "这学期还没有能统计的数据。"
    return "\n".join(lines)


def _hhmm(raw: str) -> str:
    """从「13:30」或「2026-10-29 13:30:00」里取 HH:MM。

    以前用 [start_time][-8:-3] 硬切，遇到只有 5 个字符的「13:30」
    会被切成「13」，输出成「你 13 要去上课」。
    """
    match = re.search(r"(\d{1,2}):(\d{2})", str(raw or ""))
    if not match:
        return ""
    return f"{int(match.group(1)):02d}:{match.group(2)}"


def handle_best_place(text: str = "", chat_id: str | None = None) -> str:
    """现在去哪自习：空教室 + 图书馆 + 课表空档 + 天气，结论由代码算。"""
    from app.tools import library

    now = datetime.now()
    weekday = now.isoweekday()
    hhmm = now.strftime("%H:%M")

    today: list[dict] = []
    try:
        today = [item for item in schedule_store.this_week() if item["day"] == weekday]
    except Exception:
        pass
    upcoming = []
    for item in today:
        start = _hhmm(item.get("start_time"))
        if start and start > hhmm:
            upcoming.append(item)
    upcoming.sort(key=lambda item: item.get("start_time") or "")

    sections = classroom.current_sections()
    rooms: list[dict] = []
    if sections:
        try:
            rooms = classroom.find_empty("ZX", weekday, sections)
        except Exception:
            rooms = []

    seats: dict[str, int] = {}
    try:
        seats = library.seat_counts()
    except Exception:
        seats = {}
    lib_total = sum(seats.values())

    weather_hint = ""
    try:
        from app.tools import weather as weather_tool

        data = weather_tool.fetch()
        if data.get("ok"):
            desc = data.get("text") or ""
            temp = data.get("temp")
        else:
            legacy = get_weather() or {}
            desc = str(legacy.get("description") or "")
            temp = legacy.get("temp")
        if any(k in desc for k in ("雨", "雪")):
            weather_hint = f"外面{desc}，室内更合适。"
        elif isinstance(temp, (int, float)) and temp >= 32:
            weather_hint = f"现在 {temp:.0f}°C，挑有空调的室内。"
    except Exception:
        pass

    if sections and classroom.is_in_session() and upcoming:
        nxt = upcoming[0]
        return (
            f"你正在上课，下节 {nxt.get('course')} 在 {nxt.get('location') or '教室'}。\n"
            f"这节结束后正心楼还有 {len(rooms)} 间空教室可用。"
        )

    if upcoming and len(upcoming) == len(today):
        nxt = upcoming[0]
        start = _hhmm(nxt.get("start_time"))
        place = nxt.get("location") or "教室"
        when = f"{start} " if start else ""
        return (
            f"你 {when}要去 {place} 上 {nxt.get('course')}，别跑远。\n"
            f"就近自习：正心楼现在 {len(rooms)} 间空教室。{weather_hint}".strip()
        )

    lib_detail = "、".join(f"{name} {count}" for name, count in seats.items()) or "没查到"
    if lib_total >= 50:
        return (
            f"去图书馆。现在 {lib_detail} 个空位，比教室稳。\n"
            f"正心楼同期 {len(rooms)} 间空教室。{weather_hint}".strip()
        )
    if len(rooms) >= 20:
        return (
            f"去正心楼。现在 {len(rooms)} 间空教室，图书馆只剩 {lib_total} 个位。\n"
            f"{weather_hint}".strip()
        )
    if len(rooms) or lib_total:
        return (
            f"两边都不宽裕：正心楼 {len(rooms)} 间空教室，图书馆 {lib_total} 个位（{lib_detail}）。\n"
            "就近选吧，别折腾。"
        )
    return "本鱼现在查不到空教室和图书馆空位，可能接口在维护，过会儿再问。"


_HW_SPLIT = re.compile(r"[;；\n]+|还有|另外|以及")


def _date_table(now: datetime, days: int = 14) -> str:
    """给模型一张未来两周的日期对照表。

    模型自己算「周五是哪天」会算错（实测把明天算成前天），
    让它照抄表里的日期，比让它推理可靠。
    """
    names = "一二三四五六日"
    lines = ["日期对照（必须从这张表里选，不要自己推算）："]
    for offset in range(days):
        day = now + timedelta(days=offset)
        tag = "（今天）" if offset == 0 else ("（明天）" if offset == 1 else ("（后天）" if offset == 2 else ""))
        lines.append(f"{day:%Y-%m-%d} 周{names[day.weekday()]}{tag}")
    return "\n".join(lines)


def _parse_due(raw: str) -> datetime | None:
    """模型给的日期格式不统一：有「2026-10-30」也有「10-30 23:59」。

    以前只认一种，格式一变整条作业就被丢掉。
    """
    text = str(raw or "").strip()
    if not text:
        return None
    now = datetime.now()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%m-%d %H:%M", "%m-%d"):
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if fmt.startswith("%m"):
            parsed = parsed.replace(year=now.year)
        if "%H" not in fmt:
            parsed = parsed.replace(hour=23, minute=59)
        return parsed
    return None


def _apply_code_date(text: str, due: datetime) -> datetime | None:
    """句子里的「周五/下周三」用代码算日期，模型只负责时刻。

    模型自己推周几会算错（实测把周五算成周三），日期一律以代码为准。
    原句里没有任何时间词时返回 None——模型爱自己编一个截止时间，
    宁可不入库，让用户补时间。
    """
    from app.tools import memory

    raw = memory.parse_remind_at(text or "")
    if not raw:
        return None
    try:
        day = datetime.strptime(raw, "%Y-%m-%d %H:%M").date()
    except ValueError:
        return None
    return datetime.combine(day, due.time())


def _split_homework(text: str) -> list[str]:
    """按分号、换行、「还有/另外/以及」切成一事一句。少于 2 字的不算一件事。"""
    parts = []
    for chunk in _HW_SPLIT.split(text or ""):
        chunk = (chunk or "").strip(" ，,。的、")
        if len(chunk) >= 2:
            parts.append(chunk)
    return parts


def _parse_homework_many(text: str) -> list[dict]:
    """一次说多件作业时批量解析。

    只有一句就复用原来的单条解析（一次模型调用）；
    多句时把编号列表一次喂给模型，让它直接给数组，避免 N 句调 N 次。
    """
    parts = _split_homework(text)
    if len(parts) <= 1:
        one = _parse_homework(text)
        return [one] if one else []

    import json

    now = datetime.now()
    numbered = "\n".join(f"{index + 1}. {part}" for index, part in enumerate(parts))
    prompt = (
        f"当前时间：{now:%Y-%m-%d %H:%M}（周{'一二三四五六日'[now.weekday()]}）\n"
        f"{_date_table(now)}\n\n"
        f"用户一次说了这几件事：\n{numbered}\n\n"
        "逐条提取作业和截止时间，只输出 JSON 数组，不要解释：\n"
        '[{"content":"作业内容","due_at":"YYYY-MM-DD HH:MM"}]\n'
        "规则：\n"
        "- 一条对应一句，不要合并\n"
        "- content 保留课程名和任务，去掉「记一下」「帮我记」这类词\n"
        "- 某句没提时间的就跳过那条，绝对不要自己编时间\n"
        "- 日期只能从上面的对照表里取；只说日期没说几点按当天 23:59"
    )
    try:
        from app.llm.client import chat

        raw = chat(prompt, max_tokens=400, purpose="homework") or ""
    except Exception:
        return []

    match = re.search(r"\[.*\]", raw, re.S)
    if not match:
        return []
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []

    out = []
    for index, item in enumerate(data):
        if not isinstance(item, dict):
            continue
        content = str(item.get("content") or "").strip()
        due = str(item.get("due_at") or "").strip()
        if not content or not due:
            continue
        when = _parse_due(due)
        if when is None:
            continue
        origin = parts[index] if index < len(parts) else text
        when = _apply_code_date(origin, when)
        if when is None or when <= now or (when - now).days > 120:
            continue
        out.append({"content": content[:80], "due_at": when})
    return out


def _parse_homework(text: str) -> dict | None:
    """模型只负责拆「作业内容 + 截止时间」，课程名和日期校验交给代码。"""
    import json

    now = datetime.now()
    prompt = (
        f"当前时间：{now:%Y-%m-%d %H:%M}（周{'一二三四五六日'[now.weekday()]}）\n"
        f"{_date_table(now)}\n\n"
        f"用户说：{text}\n\n"
        "提取作业内容和截止时间，只输出 JSON，不要解释：\n"
        '{"content":"作业内容","due_at":"YYYY-MM-DD HH:MM"}\n'
        "规则：\n"
        "- content 保留课程和任务，去掉「记一下」「帮我记」这类词\n"
        "- 没提截止时间就输出 {\"content\":\"\",\"due_at\":\"\"}\n"
        "- 日期只能从上面的对照表里取；只说日期没说几点，按当天 23:59 算"
    )
    try:
        from app.llm.client import chat

        raw = chat(prompt, max_tokens=160) or ""
    except Exception:
        return None
    match = re.search(r"\{.*\}", raw, re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not data.get("content") or not data.get("due_at"):
        return None
    when = _parse_due(data["due_at"])
    if when is None:
        return None
    when = _apply_code_date(text, when)
    if when is None or when <= now or (when - now).days > 120:
        return None
    return {"content": str(data["content"])[:80], "due_at": when}


def handle_homework(text: str, chat_id: str | None = None) -> str:
    if not chat_id:
        return "作业看板只在飞书里能用（要记住是哪个会话）。"
    from app.agent import schedule_edit
    from app.tools import homework

    # 「修改木鱼：」对作业的写操作也认，别让前缀混进关键词
    text = schedule_edit.strip_keyword(text)

    # 0) 删除（记忆线里的同一条也跟着删，两条线对得上）
    delete_hint = any(k in text for k in ("删掉作业", "删除作业", "移除作业", "作业删掉", "作业删除"))
    if not delete_hint:
        delete_hint = any(text.strip().startswith(k) for k in ("删掉", "删除", "移除", "删了")) and "作业" in text
    if delete_hint:
        keyword = re.sub(r"(删掉|删除|移除|删了|作业)", "", text).strip(" ，,。的、把")
        course = homework.match_course(text)
        if course:
            keyword = course
        if not keyword:
            return "要删哪条？说「删掉作业 高数」这样。"
        done = homework.remove(chat_id, keyword)
        try:
            from app.tools import memory

            mem = memory.remove(chat_id, keyword)
        except Exception:
            mem = 0
        if done or mem:
            bits = []
            if done:
                bits.append(f"作业 {done} 条")
            if mem:
                bits.append(f"记忆 {mem} 条")
            return f"删掉了{'和'.join(bits)}和「{keyword}」相关的。"
        return f"没找到和「{keyword}」对得上的作业或记忆。"

    # 1.5) 改期：只动截止时间，两档提醒标记清零按新时间重算
    if any(k in text for k in ("改期", "改到", "推迟", "延期")):
        from app.tools import memory

        keyword = re.sub(r"(改期|改到|推迟到|推迟|延期到|延期|作业|截止)", "", text)
        keyword = re.sub(
            r"(今天|明天|后天|大后天|下下|下|本|这)?\s*(?:周|星期|礼拜)\s*[一二三四五六日天]"
            r"|\d{1,2}\s*月\s*\d{1,2}\s*[日号]"
            r"|\d{1,2}\s*[点时](?:\s*\d{1,2}\s*分?|\s*半)?",
            "",
            keyword,
        ).strip(" ，,。的")
        course = homework.match_course(text)
        kw = course or keyword
        when_raw = memory.parse_remind_at(text)
        if not kw or not when_raw:
            return "要改哪条、改到什么时候？说「高数作业改到周六」这样。"
        when = datetime.strptime(when_raw, "%Y-%m-%d %H:%M")
        if not re.search(r"\d{1,2}\s*[点时]|点半", text):
            when = when.replace(hour=23, minute=59)
        changed = homework.reschedule(chat_id, kw, when.strftime("%Y-%m-%d %H:%M"))
        if changed:
            return (
                f"改好了：{kw} 截止时间改成 {when:%m-%d %H:%M}，提醒按新时间重新算。\n"
                "记忆里那条旧截止时间的备忘不会自动变，要清就说「修改木鱼：忘掉 关键词」。"
            )
        return f"没找到和「{kw}」对得上的待交作业。"

    # 1) 标记完成
    if any(k in text for k in ("交了", "写完了", "做完了", "完成了", "上交了", "提交了")):
        keyword = re.sub(r"(我)?(的)?(已经)?(都)?(上交了|提交了|交了|写完了|做完了|完成了)", "", text)
        keyword = keyword.replace("作业", "").replace("已经", "").replace("我的", "").strip(" ，,。的")
        course = homework.match_course(text)
        if course:
            keyword = course
        if not keyword:
            items = homework.pending(chat_id)
            if not items:
                return "你现在没有待交的作业。"
            keyword = items[0]["course"] or items[0]["content"][:4]
        done = homework.finish(chat_id, keyword)
        if done:
            return f"标好了：{keyword} 已完成（{done} 条）。"
        return f"没找到和「{keyword}」对得上的待交作业。发「我有哪些作业」看看。"

    # 2) 看清单
    if any(k in text for k in ("还差", "没交", "清单", "有哪些", "哪些作业", "那些作业", "啥作业",
                               "什么作业", "几门作业", "看看作业")):
        items = homework.pending(chat_id)
        if not items:
            return "没有待交的作业。要记就说「记一下，周五前交高数作业」。"
        now = datetime.now()
        lines = [f"待交作业（{len(items)} 条）："]
        for item in items:
            due = datetime.strptime(item["due_at"], "%Y-%m-%d %H:%M")
            left = (due - now).total_seconds()
            if left < 0:
                tag = "已过期"
            elif left <= 2 * 3600:
                tag = "不到 2 小时"
            elif left <= 24 * 3600:
                tag = "今天/明天截止"
            else:
                tag = f"还有 {int(left // 86400)} 天"
            who = f"{item['course']} · " if item["course"] else ""
            lines.append(f"· {who}{item['content']} —— {due:%m-%d %H:%M}（{tag}）")
        return "\n".join(lines)

    # 3) 录入（一句里可以有好几件事）
    parsed_list = _parse_homework_many(text)
    if not parsed_list:
        return (
            "没听清要记什么作业。这样说：\n"
            "「记一下，周五前交高数作业第三章」\n"
            "要包含课程名和截止时间。一次说几件也行，用分号隔开。"
        )

    added = []
    for parsed in parsed_list:
        course = homework.match_course(parsed["content"])
        homework.add(chat_id, course, parsed["content"], parsed["due_at"].strftime("%Y-%m-%d %H:%M"))
        who = f"{course} · " if course else ""
        added.append(f"· {who}{parsed['content']} —— {parsed['due_at']:%m-%d %H:%M}")

    if len(added) == 1:
        return (
            f"记下了：{added[0][2:]}\n\n"
            "截止前 1 天和 2 小时会各提醒一次。说「我有哪些作业」看全表。"
        )
    return (
        f"记下了 {len(added)} 条：\n" + "\n".join(added) + "\n\n"
        "截止前 1 天和 2 小时会各提醒一次。说「我有哪些作业」看全表。"
    )


def handle_push_switch(text: str, chat_id: str | None = None) -> str:
    if not chat_id:
        return "只在飞书里可用。"
    from app.agent import schedule_edit
    from app.tools import push

    text = schedule_edit.strip_keyword(text)
    if any(k in text for k in ("关", "别推", "停")):
        push.set_enabled(chat_id, False)
        return "主动推送已关。早报、每日一敲这些都不会再发。\n想恢复说「打开推送」。"
    push.set_enabled(chat_id, True)
    return "主动推送已开。会一天最多推一条，不会刷屏。"


def handle_memory_add(text: str, chat_id: str | None = None) -> str:
    if not chat_id:
        return "只在飞书里可用。"
    from app.agent import schedule_edit
    from app.tools import homework, memory

    # 「你记住了吗」「还记得吗」是在问，不是在吩咐，别拿写操作门禁去怼他
    if any(word in (text or "") for word in ("记住了吗", "记住了没", "记住没", "记不记得", "还记得")):
        return "嗯，本鱼记下了这个说法。要长期记住的话，说「修改木鱼：记住 大肥鱼就是 DeepSeek」这样。"

    if not schedule_edit.has_keyword(text):
        return "要记东西先说「修改木鱼：」，本鱼不会自己乱记。"
    body = schedule_edit.strip_keyword(text)
    content = re.sub(r"^(帮我)?(记住|记一下|记下|帮我记)", "", body).strip(" ：:，,。")
    if len(content) < 2:
        return "要记住什么？说「修改木鱼：记住 我住绍信3-1005」这样。"

    # 作业：按作业看板建，保留「提前 1 天 / 提前 2 小时」双档提醒；
    # 记忆里只留不带提醒的备忘，删除时两条线一起删
    if any(k in content for k in ("作业", "ddl", "DDL", "欠交")):
        parsed_list = _parse_homework_many(content)
        if parsed_list:
            added = []
            for parsed in parsed_list:
                course = homework.match_course(parsed["content"])
                homework.add(chat_id, course, parsed["content"], parsed["due_at"].strftime("%Y-%m-%d %H:%M"))
                memo = f"{parsed['content']}（{parsed['due_at']:%m-%d %H:%M} 截止）"
                memory.add(chat_id, memo, remind_at="")
                who = f"{course} · " if course else ""
                added.append(f"· {who}{parsed['content']} —— {parsed['due_at']:%m-%d %H:%M}")
            return (
                "记下了，这是作业，已经放进作业看板：\n"
                + "\n".join(added)
                + "\n\n截止前 1 天和前 2 小时各提醒一次。想删说「修改木鱼：忘掉 关键词」。"
            )

    remind_at = memory.parse_remind_at(content)
    memory.add(chat_id, content, remind_at=remind_at)
    tail = (
        f"\n到点在飞书提醒你（{remind_at}），并问要不要删。"
        if remind_at else ""
    )
    return (
        f"记下了：{content}\n\n以后不管哪个聊天都记得。{tail}\n"
        "想删就说「修改木鱼：忘掉 关键词」。"
    )


def handle_memory_list(chat_id: str | None = None) -> str:
    if not chat_id:
        return "只在飞书里可用。"
    from app.tools import memory

    items = memory.all_items()
    if not items:
        return "本鱼还没记住关于你的事。想让本鱼记就说「修改木鱼：记住 …」。"
    lines = [f"本鱼记着这些（{len(items)} 条，所有聊天通用）："]
    lines += [f"• {item['content']}" for item in items]
    return "\n".join(lines)


def handle_memory_delete(text: str, chat_id: str | None = None) -> str:
    if not chat_id:
        return "只在飞书里可用。"
    from app.agent import schedule_edit
    from app.tools import memory

    if not schedule_edit.has_keyword(text):
        return "要删记忆先说「修改木鱼：」，本鱼不会自己乱删。"
    body = schedule_edit.strip_keyword(text)
    keyword = re.sub(r"^(忘掉|删除记忆|别记这个|忘了我说的)", "", body).strip(" ：:，,。")
    if not keyword:
        return "要忘掉哪条？说「修改木鱼：忘掉 3-1005」这样。"
    removed = memory.remove(chat_id, keyword)
    try:
        from app.tools import homework

        homework_removed = homework.remove(chat_id, keyword)
    except Exception:
        homework_removed = 0
    if removed or homework_removed:
        parts = []
        if removed:
            parts.append(f"记忆 {removed} 条")
        if homework_removed:
            parts.append(f"作业 {homework_removed} 条")
        return f"删掉了{'和'.join(parts)}和「{keyword}」相关的。"
    return f"没找到包含「{keyword}」的记忆或作业。"


def handle_edit_remind(text: str, chat_id: str | None = None) -> str:
    """「修改提醒 3 改成明天下午3点」：只改时间，内容和编号不变。"""
    if not chat_id:
        return "只在飞书里可用。"
    from app.agent import schedule_edit
    from app.tools import memory, reminders

    text = schedule_edit.strip_keyword(text)
    match = re.search(r"(\d+)", text)
    if not match:
        return "要改哪个？说「修改提醒 3 改成明天下午3点」，编号在「提醒」里能看到。"
    reminder_id = int(match.group(1))

    content_match = re.search(r"内容\s*(?:改成|改为|换成)\s*(.+)", text)
    if content_match:
        content = content_match.group(1).strip(" ，,。")
        if not content:
            return "内容改成什么？说「修改提醒 3 内容改成 交英语作业」。"
        if reminders.retext(chat_id, reminder_id, content):
            return f"提醒 {reminder_id} 的内容改成「{content}」了。"
        return f"没找到编号 {reminder_id} 的待触发提醒。"

    when_text = re.sub(r"(修改提醒|改提醒|提醒改成|改一下提醒|提醒时间改|改成|改为|编号)", "", text)
    relative = memory.parse_relative_at(when_text) or memory.parse_relative_at(text)
    when_raw = "" if relative else (memory.parse_remind_at(when_text) or memory.parse_remind_at(text))
    if not relative and not when_raw:
        return "没听清新的时间。说「修改提醒 3 改成明天下午3点」这样。"
    when = relative or datetime.strptime(when_raw, "%Y-%m-%d %H:%M")
    if when <= datetime.now():
        return f"这个时间（{when:%m-%d %H:%M}）已经过了，换一个将来的时间。"
    if reminders.reschedule(chat_id, reminder_id, when):
        return f"提醒 {reminder_id} 改到 {when:%m-%d %H:%M} 了。"
    return f"没找到编号 {reminder_id} 的待触发提醒。"


def handle_cancel_remind(text: str, chat_id: str | None = None) -> str:
    if not chat_id:
        return "只在飞书里可用。"
    from app.agent import schedule_edit

    text = schedule_edit.strip_keyword(text)
    match = re.search(r"(\d+)", text)
    if not match:
        return "要取消哪个？说「取消提醒 3」，编号在「我的提醒」里能看到。"
    from app.tools import reminders

    reminder_id = int(match.group(1))
    if reminders.cancel(chat_id, reminder_id):
        return f"已取消提醒 {reminder_id}。"
    return f"没找到编号 {reminder_id} 的待触发提醒。发「我的提醒」看看编号。"


def handle_my_reminders(chat_id: str | None = None) -> str:
    if not chat_id:
        return "只在飞书里可用。"
    from app.tools import reminders

    items = reminders.pending(chat_id)
    if not items:
        return "你现在没有待触发的提醒。"
    lines = [f"你有 {len(items)} 个待提醒："]
    for item in items:
        lines.append(f"• [{item['id']}] {item['remind_at']} — {item['content']}")
    lines.append("\n取消某个：说「取消提醒 3」（编号）")
    return "\n".join(lines)


def handle_my_activities() -> str:
    return (
        "查不到你报了哪些：二课接口只开放公共活动目录，没有「我的报名」这一项，不是权限问题，是接口没这个能力。\n\n"
        "去哪儿看已报名的：\n"
        "• 「青春杭电」公众号 → 第二课堂 → 我的\n"
        "• 「杭电助手」App → 二课活动 → 我的报名\n\n"
        "想报新的：直接说「我要报名 XX」。"
    )


_SHAOXING_BUILDINGS = ("正心", "创智", "青越", "子衿", "博约", "礼贤", "精诚", "芯越")


_ONLINE_WORDS = ("线上", "网络", "钉钉", "腾讯会议", "直播")


def _activity_campus(item: dict) -> str:
    """接口没有校区字段，只能从地点名判断。"""
    position = str(item.get("position") or item.get("address") or "")
    if "下沙" in position:
        return "下沙"
    if "绍兴" in position or any(name in position for name in _SHAOXING_BUILDINGS):
        return "绍兴"
    if re.search(r"[1-9]\s*教", position) or "教学楼" in position:
        return "下沙"
    # 「6北104」「3南201」这类：数字+方位+教室号，是下沙的命名习惯
    if re.search(r"[1-9]\s*[北南东西]\s*\d{2,3}", position):
        return "下沙"
    # 线上判断必须放最后：「6教北104 & 钉钉群」是线下主会场 + 线上同步
    if any(word in position for word in _ONLINE_WORDS):
        return "线上"
    return "未知"


def _wanted_campus(text: str) -> str | None:
    if "绍兴" in (text or ""):
        return "绍兴"
    if "下沙" in (text or ""):
        return "下沙"
    return None


def handle_activities(text: str = "", campus: str | None = None, chat_id: str | None = None) -> str:
    try:
        raw = list_activities(status=8, page_size=30)
    except HduhelpError as exc:
        message = str(exc)
        if "服务授权" in message or "40920" in message:
            # 40920 是 HDUHelp 平台层的服务授权，跟命令行 OAuth 是两把锁
            return (
                "**查不到活动列表**，不是报名失败。\n"
                "原因：杭电助手的「二课活动」服务授权没开。\n"
                "打开「杭电助手」（App 或小程序）→ 我的/设置 → 服务授权 → 二课活动 → 同意。\n\n"
                "这是 App 里的服务授权；跟命令行那边要不要重新授权是两件事，都要开才能查。"
            )
        if "权限" in message or "MISSING_SCOPE" in message or "401" in message:
            return (
                "**查不到活动列表**：命令行这边缺活动读权限。\n"
                "在电脑上双击 `★3 重新授权.lnk`，把活动相关的权限勾上再试。"
            )
        raise
    data = raw.get("data") or {}
    items = data.get("items") or data.get("list") or data.get("records") or []
    if not items:
        return "现在没有报名中的活动。"
    remember_activity_list(chat_id, items)

    # 模型常把「绍兴校区」原样传进来，再拼一次校区就成了「绍兴校区校区」
    want = (campus or "").replace("校区", "").strip()
    if want not in ("绍兴", "下沙"):
        want = _wanted_campus(text)
    unknown_count = 0
    online_count = 0
    if want:
        kept = []
        for item in items:
            where = _activity_campus(item)
            if where == want:
                kept.append(item)
            elif where == "未知":
                unknown_count += 1
            elif where == "线上":
                online_count += 1
        items = kept
        if not items:
            extra = []
            if online_count:
                extra.append(f"线上活动 {online_count} 个")
            if unknown_count:
                extra.append(f"地点没写校区 {unknown_count} 个")
            tail = f"另有 {'，'.join(extra)}。\n" if extra else ""
            return (
                f"报名中的活动里没有地点标为「{want}校区」的。\n"
                f"{tail}想看全部就说「这周有啥活动」。"
            )

    title = f"{want}校区的报名中活动（{len(items)} 个）：" if want else f"报名中的活动（{len(items)} 个）："
    lines = [title]
    for item in items[:10]:
        name = (
            item.get("activityName")
            or item.get("name")
            or item.get("title")
            or "未命名活动"
        )
        time_text = (
            item.get("activityStartTime")
            or item.get("startTime")
            or item.get("activityTime")
            or ""
        )
        place = item.get("position") or item.get("address") or item.get("activityAddress") or ""
        host = item.get("hostName") or item.get("organizer") or ""
        meta = " · ".join(x for x in (time_text, place, host) if x)
        lines.append(f"• {name}" + (f"\n  {meta}" if meta else ""))
    if want:
        extra = []
        if online_count:
            extra.append(f"线上活动 {online_count} 个")
        if unknown_count:
            extra.append(f"地点没写校区 {unknown_count} 个")
        if extra:
            lines.append(f"\n（另有 {'，'.join(extra)}，没有列出来）")
    return "\n".join(lines)


# 注意：连字符要放字符类末尾，写成 \- 会被解析成范围，把整段字母都吃掉
_ACT_PUNCT = re.compile(r"[\s\"'“”‘’「」『』・·_,，。.、:：;；()（）!！?？—－-]")


def _norm_activity(value: str) -> str:
    """活动名归一化：全半角统一、去标点空白。

    用户手打的名字和接口返回的名字常在引号（"" vs ""）、中点（・ vs ·）
    上差一两个字符，直接比子串会匹配不上。
    """
    import unicodedata

    text = unicodedata.normalize("NFKC", value or "")
    return _ACT_PUNCT.sub("", text).lower()


_LAST_ACTIVITIES: dict[str, dict] = {}
_ACTIVITY_TTL = 1800


def remember_activity_list(chat_id: str | None, items: list[dict]) -> None:
    """记住刚列给用户的活动顺序，「报名第 2 个」才有得指。"""
    if not chat_id or not items:
        return
    _LAST_ACTIVITIES[chat_id] = {
        "at": time.time(),
        "items": [
            (str(it.get("activityID") or ""), str(it.get("activityName") or ""))
            for it in items
        ],
    }


def recent_activities(chat_id: str | None) -> list[tuple[str, str]]:
    if not chat_id:
        return []
    rec = _LAST_ACTIVITIES.get(chat_id)
    if not rec or time.time() - float(rec.get("at") or 0) > _ACTIVITY_TTL:
        return []
    return rec.get("items") or []


def _mention_index(query: str) -> int | None:
    """「第 2 个」→ 1；「这个/那个」→ -1（列表只有一条时用）；无指代返回 None。"""
    match = re.search(r"第\s*([一二三四五六七八九1-9])\s*个", query)
    if match:
        raw = match.group(1)
        number = int(raw) if raw.isdigit() else _CN_NUM.get(raw)
        return (number - 1) if number else None
    if any(k in query for k in ("这个", "那个", "刚才", "上面", "它")):
        return -1
    return None


def match_activity(text: str, chat_id: str | None = None) -> dict | None:
    """从报名中的活动里，按用户说的名字找一条；「第几个/这个」按上一轮列表解析。"""
    try:
        raw = list_activities(status=8, page_size=30)
    except HduhelpError:
        return None
    items = (raw.get("data") or {}).get("items") or []

    query = text or ""
    for noise in ("我要报名", "帮我报名", "报名参加", "报名参与", "取消报名", "我想参加", "帮我报", "报名"):
        query = query.replace(noise, "")
    query = query.strip(" ，,。的啦吧啊")

    index = _mention_index(query)
    if index is not None:
        cached = recent_activities(chat_id)
        if index >= 0 and index < len(cached):
            aid, name = cached[index]
            for item in items:
                if str(item.get("activityID") or "") == aid:
                    return item
            for item in items:
                if (item.get("activityName") or "") == name:
                    return item
        if index == -1 and len(cached) == 1:
            aid, _name = cached[0]
            for item in items:
                if str(item.get("activityID") or "") == aid:
                    return item

    if len(query) < 2:
        return None
    norm_query = _norm_activity(query)
    if len(norm_query) < 2:
        return None

    for item in items:
        name = item.get("activityName") or ""
        norm_name = _norm_activity(name)
        if norm_query in norm_name or (len(norm_name) >= 4 and norm_name in norm_query):
            return item
    return None


# ---------- 文字确认（微信没有卡片按钮，改成回一句话执行） ----------
# 飞书仍走卡片回调，这里只多一条文字入口，不影响原流程。
_PENDING_SIGNUP: dict[str, dict] = {}
_CONFIRM_KB_RE = re.compile(r"^(?:确认)?(?:入库|收录)$")
_CONFIRM_MAIL_RE = re.compile(r"^(?:确认)?发送$|^发送吧$|^发吧$")
_CANCEL_RE = re.compile(r"^(?:取消|算了|不发了|不报了|不入库了)$")
_CONFIRM_APPLY_RE = re.compile(r"^确认报名$|^报名吧$")


def _text_confirm(text: str, chat_id: str | None) -> str | None:
    if not chat_id:
        return None
    t = (text or "").strip()
    # 取消要排在最前：否则「不发送了」会被下面的发送分支先抢走
    if _CANCEL_RE.match(t):
        cleared = False
        got = take_mail_draft(chat_id)
        if got:
            drop_mail_draft(got[0])
            cleared = True
        from app.tools import kb as _kb

        staged = _kb.take_for_chat(chat_id)
        if staged:
            _kb.drop(staged[0])
            cleared = True
        if _PENDING_SIGNUP.pop(str(chat_id), None):
            cleared = True
        return "好，这次不弄了。" if cleared else None
    if _CONFIRM_KB_RE.match(t):
        from app.tools import kb

        got = kb.take_for_chat(chat_id)
        if not got:
            return "没有待入库的资料。先把链接发本鱼（说「收录 网址」）。"
        _token, candidate = got
        result = kb.save(candidate, verified=True)
        if result.get("ok"):
            return "入库好了，下次问这个本鱼就能直接答。"
        return "这条没入库：%s" % (result.get("reason") or "未知原因")
    # 有草稿时句子里带「发送」就算确认；提到「发送 + 邮件」但没草稿时，
    # 要明确说「没有草稿」，不能静默落到查收件箱（他实测过两次）
    has_draft = str(chat_id) in _MAIL_BY_CHAT
    wants_send = bool(_CONFIRM_MAIL_RE.match(t)) or bool(
        re.search(r"(发送|发出去|发掉|发吧)", t)
        and (has_draft or re.search(r"邮件", t))
    )
    if wants_send:
        got = take_mail_draft(chat_id)
        if not got:
            return "没有待发送的邮件。先说清楚发给谁、说什么。"
        token, draft = got
        from app.tools import mail as mail_tool

        result = mail_tool.send(draft["to"], draft["subject"], draft["body"])
        drop_mail_draft(token)
        if result.get("ok"):
            return "邮件发出去了\n收件人：%s\n主题：%s" % (draft["to"], draft["subject"])
        return "没发出去：%s" % (result.get("reason") or "未知原因")
    if _CONFIRM_APPLY_RE.match(t):
        item = _PENDING_SIGNUP.pop(str(chat_id), None)
        if not item:
            return "没有待报名的活动。先问「这周有啥活动」，再点名要报哪个。"
        from app.tools.hduhelp import HduhelpError, apply_activity

        name = item.get("activityName") or "这个活动"
        try:
            result = apply_activity(item["id"])
        except HduhelpError as exc:
            msg = str(exc)
            if "40920" in msg or "服务授权" in msg:
                return (
                    "报名没成：杭电助手的「二课活动」服务授权没开。"
                    "打开「杭电助手」→ 我的/设置 → 服务授权 → 二课活动 → 同意，再回一次「确认报名」。"
                )
            if "MISSING_SCOPE" in msg or "scope" in msg.lower() or "权限" in msg:
                return "报名没成：缺报名写权限，重新授权一次、把写权限勾上再来。"
            if "已报名" in msg or "重复" in msg:
                return "这个活动你已经报过了，不用重复。"
            return "报名没成：%s" % msg
        except Exception as exc:
            return "报名出错：%s" % type(exc).__name__
        data = result.get("data") or {}
        if data.get("registered") or data.get("success") or data.get("verified"):
            return f"报名成功：{name}\n\n临时去不了的话，去「青春杭电」→ 第二课堂取消。"
        return "报名没成功：%s" % (result.get("msg") or "接口没返回成功状态")
    return None


def format_activity_confirm(activity: dict) -> str:
    name = activity.get("activityName") or "未命名活动"
    lines = [f"**{name}**"]
    for label, key in (
        ("时间", "activityStartTime"),
        ("地点", "position"),
        ("主办", "hostName"),
        ("报名截止", "applyEndTime"),
    ):
        value = activity.get(key)
        if value:
            lines.append(f"{label}：{value}")
    count = activity.get("applyCount")
    if count is not None:
        lines.append(f"已报名：{count} 人")
    return "\n".join(lines)


def handle_activity_signup(text: str, chat_id: str | None = None) -> str:
    """命令行版本；飞书里由 bot 直接弹确认卡片，微信侧回一句「确认报名」执行。"""
    activity = match_activity(text)
    if not activity:
        # 只说了「二课报名」没点名报哪个：先给列表，再告诉他怎么挑
        body = handle_activities(text=text, chat_id=chat_id)
        return body + "\n\n要报哪个就回「报名：活动名」，本鱼再跟你确认一遍。"
    if chat_id and activity.get("id"):
        _PENDING_SIGNUP[str(chat_id)] = activity
    return format_activity_confirm(activity) + "\n\n要报名就回一句「确认报名」，不想报就算了。"


def handle_library() -> str:
    from app.tools import library

    try:
        main = library.describe_seats()
    except HduhelpError as exc:
        message = str(exc)
        if "服务授权" in message or "40920" in message:
            return (
                "图书馆座位需要一次「服务授权」：\n"
                "打开「杭电助手」→ 我的/设置 → 服务授权 → 找到「图书馆」→ 同意。"
            )
        if "权限" in message or "MISSING_SCOPE" in message:
            return "图书馆座位需要额外授权，在电脑上双击 `★3 重新授权.lnk`。"
        raise
    first_line = main.split("\n", 1)[0]
    return _with_tip(main, first_line + "\n" + "\n".join(main.split("\n")[1:4]), "推荐去哪个区域自习")


def handle_canteen(text: str = "") -> str:
    """食堂推荐：从校园食堂点评站取店铺和菜品，按关键词挑几家。"""
    from app.tools import canteen

    # 点评站只有绍兴校区的数据。问下沙还照样推绍兴的店，就是答非所问
    if "下沙" in (text or ""):
        return (
            "食堂点评站只收录绍兴校区，下沙的窗口本鱼没有数据。\n"
            "想查下沙可以换个问法让本鱼联网搜，但那是网上零散信息，不如点评站准。"
        )

    _cn_count = {"两": 2, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6}
    count = re.search(r"([2-6两三四五六])\s*[个家]", text or "")
    if count:
        token = count.group(1)
        limit = int(token) if token.isdigit() else _cn_count.get(token, 4)
    else:
        limit = 4
    try:
        picks = canteen.recommend(text, limit=limit)
    except Exception:
        return "食堂数据暂时拉不到，过一会儿再问本鱼。"

    if not picks:
        return "这个要求本鱼没找到对应的窗口，换个说法试试？比如「有什么面」「便宜点的」「辣的」。"

    header = "校外外卖，本鱼挑了几家：" if "外卖" in (text or "") else "绍兴校区食堂，本鱼挑了几家："
    lines = [header]
    for item in picks:
        shop = item["shop"]
        meta = " · ".join(x for x in (shop["floor"], shop["price"], shop["taste"]) if x)
        lines.append(f"**{shop['name']}**（{meta}）" if meta else f"**{shop['name']}**")
        for dish in item["picks"]:
            price = f" {dish['p']} 元" if dish.get("p") is not None else ""
            lines.append(f"• {dish['n']}{price}")
        if item["review"]:
            lines.append(f"  同学说：{item['review']}")
        lines.append("")
    lines.append("数据来自校园食堂点评站，口味因人而异。")
    return "\n".join(lines).rstrip()


def handle_sunrun(chat_id: str | None = None) -> str:
    try:
        data = get_sunrun_overview()
    except HduhelpError as exc:
        message = str(exc)
        if "服务授权" in message or "40920" in message:
            return (
                "阳光长跑需要一次「服务授权」：\n"
                "打开「杭电助手」（App 或小程序）→ 我的/设置 → 服务授权 → 找到「阳光长跑」→ 同意。"
            )
        if "权限" in message or "MISSING_SCOPE" in message:
            return "阳光长跑需要额外授权，在电脑上双击 `★3 重新授权.lnk`。"
        raise
    if not data:
        return "暂时拿不到阳光长跑的记录。"

    valid = data.get("validTimes") or data.get("finalTimes") or 0
    mileage_m = data.get("validMileages") or data.get("totalMileages") or 0
    speed = data.get("speed")
    days = data.get("days")

    lines = ["阳光长跑"]
    lines.append(f"有效记录：{valid} 次")
    if mileage_m:
        lines.append(f"总里程：{float(mileage_m) / 1000:.2f} km")
    if speed is not None:
        try:
            speed_val = float(speed)
            mark = "✅" if speed_val >= 2.1 else "⚠️ 低于 2.1"
            lines.append(f"平均速度：{speed_val:.2f} m/s（要求 ≥ 2.1）{mark}")
        except (TypeError, ValueError):
            lines.append(f"平均速度：{speed} m/s")
    if days:
        lines.append(f"已跑 {days} 天")
    if len(lines) == 1:
        lines.append(f"原始数据：{str(data)[:200]}")
    main = "\n".join(lines)
    if chat_id:
        from app.tools import records

        records.log_snapshot(chat_id, "sunrun", {
            "times": valid,
            "km": round(float(mileage_m) / 1000, 2) if mileage_m else 0,
            "speed": speed,
        })
    facts = f"阳光长跑有效 {valid} 次，总里程 {float(mileage_m)/1000:.2f} km" if mileage_m else f"阳光长跑有效 {valid} 次"
    return _with_tip(main, facts, "鼓励或提醒他继续跑步")


def handle_exam() -> str:
    return (
        "考试安排查不到：教务处还没开放查询接口（API_711000005），不是接口坏了，是学校关的。\n\n"
        "去哪儿看：\n"
        "• 教务系统网页 →「信息查询 → 考试安排」\n"
        "• 班群或任课老师的通知\n\n"
        "本鱼能做的：这学期修了哪些课、这周哪天最忙。"
    )


def handle_grade() -> str:
    try:
        data = get_grade()
        if data:
            return f"查到成绩数据：{data}"
    except HduhelpError:
        pass
    return (
        "成绩查不到：教务处还没开放查询接口（API_711000005），不是接口坏了，是学校关的。\n\n"
        "去哪儿看：\n"
        "• 教务系统网页 →「信息查询 → 成绩查询」\n"
        "• 「杭电助手」App 里也能看\n\n"
        "本鱼能做的：这学期修了哪些课、这周哪天最忙、复习怎么排。"
    )


def _summarize_college(name: str, question: str, result: dict) -> str:
    """内容页交给模型概括，但只许依据抓到的文字，并附原文链接。"""
    source = result["text"]
    answer = ""
    try:
        from app.llm.client import chat

        prompt = (
            f"以下是{name}官网「{result['section']}」页面的文字：\n{source[:1500]}\n\n"
            f"用户问：{question}\n"
            "只依据上面的文字回答，三句以内；页面上没写的信息就直接说没写，不要猜。"
        )
        answer = (chat(prompt, max_tokens=300) or "").strip()
    except Exception:
        answer = ""

    # 模型说页面上没有 → 多半这页是图片或脚本渲染，别拿它当答案
    if not answer or any(
        word in answer
        for word in ("未显示", "未提及", "未在页面", "没有提到", "页面上没有", "无法确定", "没有提供")
    ):
        return (
            f"**{name} · {result['section']}**\n"
            "这页的内容本鱼读不出文字（多半是图片或脚本渲染），直接点开看最准：\n"
            f"{result['url']}"
        )
    return f"**{name} · {result['section']}**\n{answer}\n\n原文：{result['url']}"


def handle_college(text: str, chat_id: str | None = None) -> str:
    """查学院官网：通知、专业、师资、领导、联系方式、科研。

    主站上那些学院链接是空壳，真站在独立子域，这里直接去真站。
    没指明学院就只提示，不替用户猜。
    """
    from app.tools import college

    name = college.resolve(text)
    if not name:
        return (
            "要查哪个学院？本鱼能查这些：\n"
            + "、".join(college.names())
            + "\n\n比如：电子信息学院有啥通知 / 计算机学院有哪些专业 / 会计学院院长是谁"
        )

    try:
        result = college.query(name, text, limit=30)
    except Exception as exc:
        return f"{name}官网打不开（{type(exc).__name__}），先看首页：{college.home_url(name)}"

    if result["items"]:
        from app.agent import pager

        blocks = []
        for item in result["items"]:
            date = f"{item['date']} " if item["date"] else ""
            blocks.append(f"· {date}{item['title']}\n  {item['url']}")
        if result["url"]:
            blocks.append(f"栏目（看全部）：{result['url']}")

        title = f"{name} · {result['section']}"
        # 超过一屏就分页，正文只给第一页，按钮由 bot 层按令牌挂上
        if chat_id and len(blocks) > pager.PAGE_SIZE:
            token = pager.register(chat_id, blocks, title)
            rendered = pager.page_text(token, 0)
            if rendered:
                return rendered[0]
        return f"**{title}**\n\n" + "\n".join(blocks)

    if result["text"]:
        # 师资、科研这类要的是具体名单，页面读不出条目时说套话没意义
        if result["topic"] in ("师资", "领导"):
            return (
                f"**{name} · {result['section']}**\n"
                "这页没读到具体条目（多半是图片或脚本渲染），点开看最准：\n"
                f"{result['url']}"
            )
        return _summarize_college(name, text, result)

    if result["url"]:
        return (
            f"{name}的「{result['section']}」页面读不出条目——"
            "这个站的列表是脚本动态加载的，抓到的是空壳。\n"
            f"直接点开看：{result['url']}"
        )

    if result.get("candidates"):
        lines = [f"{name}官网没有「{result['topic']}」这类栏目，本鱼找到这些可能相关的入口："]
        for label, url in result["candidates"]:
            lines.append(f"· {label}\n  {url}")
        lines.append(f"\n官网首页：{result['home']}")
        return "\n".join(lines)

    return (
        f"{name}官网首页没解析到「{result['topic']}」相关的栏目（首页导航也是脚本加载的）。\n"
        f"官网在这：{result['home']}"
    )


def handle_kb_add(text: str, chat_id: str | None = None) -> str:
    """收录一条网页到知识库。抽要点 + 逐字校验，等用户点确认才入库。"""
    from app.tools import kb

    match = _URL_RE.search(text or "")
    if not match:
        return "要收录哪个网址？这样说：收录 https://jwc.hdu.edu.cn/xxx/page.htm"
    url = match.group(0)

    result = kb.extract(url)
    if not result["ok"]:
        return (
            f"**这篇没收成**：{result['reason']}\n"
            f"链接：{url}\n\n"
            "本鱼只收能用原文核对上的条目——模型改过一个字也不入库。"
            "可以换个更「正文完整」的页面再试。"
        )

    if chat_id:
        kb.stage(result, chat_id)

    preview = (
        f"**准备收录**\n"
        f"标题：{result['title']}\n"
        f"原文：{result['source']}\n"
        f"抓取时间：{result['fetched_at']}　正文 {result['char_count']} 字\n\n"
        f"**摘要**\n{result['summary']}\n\n"
        f"**原文片段（已逐字核对）**\n{result['excerpt']}\n\n"
        "关键词：" + (result["keywords"] or "（模型没给）")
    )
    return preview


_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_MAIL_DRAFTS: dict[str, dict] = {}
_MAIL_BY_CHAT: dict[str, str] = {}
_MAIL_TTL = 600


def _draft_file():
    from app.config import DATA_DIR

    return DATA_DIR / "mail_drafts.json"


def _save_drafts() -> None:
    """草稿落盘：以前只放内存，重启一次就丢，用户回「发送」时会以为没这回事。"""
    try:
        import json as _json

        path = _draft_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            _json.dumps({"drafts": _MAIL_DRAFTS, "by_chat": _MAIL_BY_CHAT}, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError:
        pass


def _load_drafts() -> None:
    try:
        import json as _json

        data = _json.loads(_draft_file().read_text(encoding="utf-8"))
        now = time.time()
        for token, item in (data.get("drafts") or {}).items():
            if isinstance(item, dict) and now - float(item.get("created") or 0) <= _MAIL_TTL:
                _MAIL_DRAFTS[token] = item
        for chat, token in (data.get("by_chat") or {}).items():
            if token in _MAIL_DRAFTS:
                _MAIL_BY_CHAT[chat] = token
    except (OSError, ValueError):
        pass


def _parse_mail_draft(text: str) -> dict:
    """抽收件人、主题、正文。收件人用正则（模型常写错邮箱），主题正文让模型归纳。"""
    import json

    match = _EMAIL_RE.search(text or "")
    to = match.group(0) if match else ""

    prompt = (
        f"用户说：{text}\n\n"
        "提取邮件要素，只输出 JSON，不要解释：\n"
        '{"subject":"主题","body":"正文"}\n'
        "规则：\n"
        "- subject 不超过 30 字；用户没说就从正文里概括\n"
        "- body 用用户给的内容原样组织，不要额外加客气话，不许编\n"
        "- 缺的字段给空字符串"
    )
    subject = body = ""
    try:
        from app.llm.client import chat

        raw = chat(prompt, max_tokens=300, purpose="mail_draft") or ""
        found = re.search(r"\{.*\}", raw, re.S)
        if found:
            data = json.loads(found.group(0))
            subject = str(data.get("subject") or "").strip()[:60]
            body = str(data.get("body") or "").strip()
    except Exception:
        pass

    return {"to": to, "subject": subject, "body": body}


def handle_mail_send(text: str, chat_id: str | None = None) -> str:
    """准备一封邮件：解析 → 存草稿 → 出确认卡。点确认才真发。"""
    import secrets
    import time

    from app import config

    if not config.MAIL_SMTP_ENABLED:
        return "发信功能还没启用（需要在 .env 里设 MAIL_SMTP_ENABLED=1）。"

    draft = _parse_mail_draft(text)
    if not draft["to"]:
        return (
            "要发给谁？把完整邮箱写进来，比如：\n"
            "发邮件给 123456@qq.com，主题是请假，正文说我明天请假一天"
        )
    if not draft["body"]:
        return f"收件人 {draft['to']} 记下了，但正文没听清。再说一次要写什么内容。"

    if not draft["subject"]:
        draft["subject"] = draft["body"][:20]

    now = time.time()
    for key, item in list(_MAIL_DRAFTS.items()):
        if now - item["created"] > _MAIL_TTL:
            _MAIL_DRAFTS.pop(key, None)

    token = secrets.token_hex(6)
    draft["created"] = now
    _MAIL_DRAFTS[token] = draft
    if chat_id:
        _MAIL_BY_CHAT[chat_id] = token
    _save_drafts()

    body_preview = draft["body"] if len(draft["body"]) <= 300 else draft["body"][:300] + "…"
    return (
        f"**准备发邮件**\n"
        f"收件人：{draft['to']}\n"
        f"主题：{draft['subject']}\n"
        f"正文：{body_preview}\n\n"
        "确认无误就回一句「发送」，不想发就回「取消」。"
    )


def take_mail_draft(chat_id: str) -> tuple[str, dict] | None:
    """取走「会话 → 令牌」的映射，草稿本身留着。

    以前这里连草稿一起 pop，卡片刚发出来点「发送」就报过期——
    草稿要等真正发完（或用户取消）才能删。
    """
    token = _MAIL_BY_CHAT.pop(chat_id, "")
    if not token:
        return None
    draft = _MAIL_DRAFTS.get(token)
    return (token, draft) if draft else None


def get_mail_draft(token: str) -> dict | None:
    import time

    item = _MAIL_DRAFTS.get(token)
    if not item:
        return None
    if time.time() - item["created"] > _MAIL_TTL:
        _MAIL_DRAFTS.pop(token, None)
        return None
    return item


def drop_mail_draft(token: str) -> None:
    _MAIL_DRAFTS.pop(token, None)
    for chat, kept in list(_MAIL_BY_CHAT.items()):
        if kept == token:
            _MAIL_BY_CHAT.pop(chat, None)
    _save_drafts()


# 启动时把上次没发的草稿捡回来（10 分钟内有效），重启不用重说一遍
try:
    _load_drafts()
except Exception:
    pass


def handle_usage() -> str:
    """模型用量周报。只生成文本，不主动推。"""
    from app.ops import usage

    try:
        return usage.render_weekly()
    except Exception as exc:
        return f"用量统计没跑起来：{type(exc).__name__}: {exc}"


def handle_self_check() -> str:
    """自检：CLI 授权 / 数据库 / 最近一次成功回答。不 ping 模型，不花钱。"""
    from app.ops import health

    try:
        return health.render()
    except Exception as exc:
        return f"自检没跑起来：{type(exc).__name__}: {exc}"


def _kb_score(query: str, item: dict) -> int:
    """问题跟条目有几个共同词（英文单词或中文 2 字片段，去重后计数）。"""
    hay = (
        f"{item.get('title') or ''} {item.get('snippet') or item.get('content') or ''}"
    ).lower()
    text = (query or "").lower()
    tokens = set(re.findall(r"[a-z]{2,}", text))
    for chunk in re.findall(r"[\u4e00-\u9fa5]{2,}", text):
        for index in range(len(chunk) - 1):
            tokens.add(chunk[index:index + 2])
    return sum(1 for token in tokens if token in hay)


def _kb_title_hit(query: str, item: dict) -> bool:
    """命中词出现在标题里，比只出现在正文里可信得多，不用再让模型复核。"""
    title = (item.get("title") or "").lower()
    text = (query or "").lower()
    if not title:
        return False
    if any(word in title for word in re.findall(r"[a-z]{2,}", text)):
        return True
    for chunk in re.findall(r"[\u4e00-\u9fa5]{2,}", text):
        for index in range(len(chunk) - 1):
            if chunk[index:index + 2] in title:
                return True
    return False


def _llm_relevant(query: str, item: dict) -> bool:
    """只有一个共同词时拿不准，让模型判一下这条能不能回答问题。"""
    try:
        from app.llm.client import chat

        body = (item.get("snippet") or item.get("content") or "")[:200]
        text = chat(
            f"问题：{query}\n资料标题：{item.get('title') or ''}\n资料内容：{body}\n\n"
            "这段资料能回答这个问题吗？只回「是」或「否」。",
            max_tokens=5,
            purpose="kb_check",
        )
        return (text or "").strip().startswith("是")
    except Exception:
        return True


def _bare_followup(query: str) -> bool:
    """「哪一家」「这个呢」这类纯指代，没有实际主题词，别拿去查知识库。"""
    text = (query or "").strip()
    if len(text) > 10:
        return False
    return any(word in text for word in ("哪一家", "哪家", "这个", "那个", "它呢", "那呢", "哪个"))


def handle_knowledge(query: str) -> str:
    # 纯指代（「哪一家」「这个呢」）没有主题词，查也是乱命中，让它回去问清楚
    if _bare_followup(query):
        return "本鱼不确定你指的是哪件事，再说具体一点？比如直接说东西的名字。"

    # 先查本地 FAQ（每条带官网出处、零 LLM），命中就不用等知识接口
    from app.tools import faq

    hits = faq.search(query)
    if hits:
        return faq.render(hits[0])

    try:
        items = knowledge_retrieve(query)
    except HduhelpError as exc:
        message = str(exc)
        if "服务端" in message or "500" in message or "暂时" in message:
            return (
                "知识库暂时不可用（HDUHelp 服务端故障，不是你的问题）。\n"
                "现在能用的：今天有啥课 / 这周有啥课 / 校园卡余额 / 正心楼4楼空教室"
            )
        return f"知识库暂时查不了：{message}"
    if not items:
        return "没找到相关的规定，建议直接问辅导员或查学校官网。"

    # 共同词 ≥2 直接采用；只有 1 个说明拿不准，让模型判一下；0 个丢弃
    scored: list[tuple[int, dict]] = []
    for item in items:
        score = _kb_score(query, item)
        if score >= 2:
            scored.append((score, item))
        elif score == 1 and (_kb_title_hit(query, item) or _llm_relevant(query, item)):
            scored.append((1, item))
    scored.sort(key=lambda pair: -pair[0])
    items = [item for _, item in scored]
    if not items:
        return "没找到相关的规定，建议直接问辅导员或查学校官网。"

    lines = ["找到这些资料："]
    for item in items[:3]:
        title = item.get("title") or item.get("source") or "资料"
        snippet = (item.get("snippet") or item.get("content") or "")[:80]
        lines.append(f"• {title}\n  {snippet}")
    return "\n".join(lines)
