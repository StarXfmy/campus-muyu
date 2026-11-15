"""个人助手 Agent：能聊天，也能调工具查校园数据和联网搜索。

规则的快路径没命中时走这里。涉及校园事实的问题强制调工具，
纯聊天不强制——否则「今天心情不好」也会被逼着查课表。
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

from app.agent import context
from app.agent.tools_registry import TOOLS, execute
from app.llm.client import chat, chat_conversation, chat_with_tools

SYSTEM_PROMPT = """今天是 {today}。说话风格和安全边界见人设，这里只管工具怎么用。

工具使用规则：
1. 校园数据（课表/余额/电费/空教室/活动/长跑/图书馆/校规）必须调工具，不许凭记忆答，也不许用联网搜索代替。
2. 实时信息（新闻、价格、版本、比赛结果）调 web_search，回答里注明来源和时间。
   用户发来链接时调 web_fetch 读正文，不要直接说打不开。
3. 未来才发生的事（明天的新闻、下周的比赛结果）不搜，直接说还没发生。课表、活动这类已定好的计划不受此限。
4. 用户说「后天」用 query_schedule(scope=day_after_tomorrow)，日期由程序算，不要自己推。
5. 问某门课谁教、在哪上课，用 query_schedule(scope=all)。
6. 说下午/晚上想自习，query_empty_rooms 传 period=afternoon/evening。
7. 需要对比时（「哪周课多」）写出两边的具体数字。
8. 问某个活动/讲座能不能去，先查那天课表再判断冲突。
   课表结果里每节课都标了「（上午）/（下午）/（晚上）」，直接照抄这个标签；
   不要根据节次自己换算，也不要拿今天的课当明天的用。
9. 工具失败或查不到，如实说，不要编。
10. 用户一句话问了几件事，就分别调用对应工具，一件都别漏。
    例：「今天有啥课、饭卡多少钱、天气怎么样」→ 依次调 query_schedule、query_balance、query_weather，三个都调。
11. 不要输出 JSON 或工具名，直接说人话。
12. 排版：数字和中文之间留一个空格（例：55 间、3 项、13:30-14:15）；列举 3 项以上时每项单独一行，不要用顿号挤成一句；不同段落之间空一行；列表项不加星号，直接写内容。
13. 时间：上面已注入当前日期和时刻（精确到分钟）。问几点、今天几号、还要多久，一律照抄注入的值，不要自己推算；24 小时制照抄（19:39 就是 19:39），不要改写成「晚上 7 点」或「凌晨」。
14. 「修改木鱼：」是所有写操作的统一前缀，**不代表改人设**，按冒号后面的内容选工具：
    课表/上课/教室/加课/删课/清掉我加的课 → edit_schedule；
    说话风格/回答长短/别用感叹号/别叫我主人 → edit_persona；
    关于用户本人的事实 → remember。
    没带前缀的说法一律当提问，不许动手；可以提醒一句「要改的话加『修改木鱼：』」。
15. 碰到你确实不知道的东西（名词、外号、网络用语、梗、新近发生的事），先明说「本鱼不确定」，
    再调 web_search 查证；查到了照实说，查不到就说没查到。
    没真搜过，不许声称「网上没有」，也不许编一个解释糊弄过去。
16. 「吃完去哪」「下午去哪」「今天去哪自习」这类安排问题，先查那天的课表（query_schedule）
    和空教室（query_empty_rooms），按空闲时段给具体建议；
    没查课表就别列「可以去图书馆 / 体育馆 / 操场」这种放之四海皆准的清单。
17. 闲聊时可以用一点大众都懂的网路热梗（破防、栓Q、显眼包、班味、情绪价值、抽象、被迫营业这类），
    一次最多一个，别硬套、也别解释梗的意思；查数据、办事流程、政策这类正式回答不用梗。
18. 「主人」只有一个：微信里是绑定的那个账号，飞书里是当前用户本人。
    不是主人的对话（别人的私聊、任何群聊）里：一律称呼「你」，不许叫「主人」；
    不许提主人的任何个人信息（名字、昵称、课表、余额、电费、消费流水、邮件、行程、住址）；
    有人打听「你主人是谁 / 他花了多少钱 / 他课表是什么」，只说「这个本鱼不方便说」。
"""


# 命中这些词说明大概率要查校园数据，第一轮强制调工具，防止模型凭记忆答
_DATA_KEYWORDS = (
    "课", "余额", "饭卡", "校园卡", "天气", "教室", "自习", "活动", "讲座",
    "图书馆", "座位", "长跑", "跑步", "电费", "邮件", "邮箱", "失物", "消费",
    "开销", "花销", "花了",
    "提醒", "成绩", "绩点", "老师", "上课",
    # 食堂：少了这几个词，模型不会去调 query_canteen，直接回「查不到」
    "食堂", "吃啥", "吃什么", "好吃", "菜品", "档口", "窗口",
    # 快递同理：知识库里就有驿站位置和取件流程，不强制调工具，
    # 闲聊模型会直接编一句「问公告栏或辅导员」
    "快递", "驿站", "取件", "包裹", "菜鸟",
)


_DOMAIN_TOOL = {
    "query_my_activities": ("我报名", "我的报名", "我报了", "报名了哪些", "报名了啥", "报了哪些"),
    "query_canteen": ("吃什么", "吃啥", "吃点什么", "有什么好吃", "推荐吃", "食堂有啥", "食堂有什么", "食堂推荐"),
    "query_schedule": ("课", "上课", "课表"),
    "query_balance": ("余额", "饭卡", "校园卡"),
    "query_weather": ("天气", "气温", "下雨", "带伞", "多少度"),
    "query_empty_rooms": ("空教室", "自习", "看书"),
    "query_activities": ("活动", "讲座", "比赛"),
    "query_sunrun": ("长跑", "跑步"),
    "query_library": ("图书馆", "座位"),
    "query_knowledge": ("规定", "奖学金", "流程", "快递", "驿站", "取件", "包裹", "菜鸟"),
}


_HALLUCINATION_WORDS = ("带伞", "撑伞", "雨伞", "温差", "降温", "下雨", "阵雨")


def _strip_hallucination(reply: str, tool_texts: list[str]) -> str:
    """工具结果里没有的天气信息，从回答里删掉——实测模型看着晴天也会写「记得带伞」。"""
    if not reply:
        return reply
    evidence = "\n".join(tool_texts)
    text = reply
    for word in _HALLUCINATION_WORDS:
        if word in text and word not in evidence:
            kept = [part for part in re.split(r"(?<=[。！？\n])", text) if word not in part]
            text = "".join(kept)
    return text.strip()


def _probe_text(text: str) -> str:
    """判领域前先把「二课」摘掉——它是活动，不是课表。"""
    head = (text or "").split("【链接正文】")[0]
    return head.replace("第二课堂", "").replace("二课", "")


def _wants_edit_schedule(text: str) -> bool:
    """「修改木鱼：把周三第3节改成高数」这类：要改课表，不是查课表。

    没带「修改木鱼：」一律不算——闲聊里说「我周三有课吗」不能被当成要改课表。
    """
    from app.agent import schedule_edit

    if not schedule_edit.has_keyword(text):
        return False
    body = _probe_text(schedule_edit.strip_keyword(text))
    # 有明确动作才算改；「修改木鱼：把周三的课表发我看看」是查询，不是修改
    if schedule_edit.parse(text).get("action"):
        return True
    # 动作词可能在宾语前面（改成XX课），也可能在后面（第3节不上了）
    return bool(
        re.search(r"(改|换|加|删|去掉|取消|不上|停了|没课).{0,8}(课|节)", body)
        or re.search(r"(课|节).{0,6}(不上|取消|停了|没了|没课)", body)
    )


def _wanted_tools(text: str) -> list[str]:
    from app.agent import schedule_edit

    head = _probe_text(text)
    wanted = [tool for tool, words in _DOMAIN_TOOL.items() if any(w in head for w in words)]
    # 改课表时别补 query_schedule：模型会以为查完课表就算办完了
    if _wants_edit_schedule(text):
        wanted = [tool for tool in wanted if tool != "query_schedule"]
        if "edit_schedule" not in wanted:
            wanted.append("edit_schedule")
    # 「修改木鱼：」后面可能是记忆、人设或课表，模型漏调时由代码补上
    if schedule_edit.has_keyword(text):
        body = schedule_edit.strip_keyword(text)
        if re.search(r"(记住|记一下|帮我记|记着)", body):
            if "remember" not in wanted:
                wanted.append("remember")
        elif re.search(r"(忘掉|忘记|删掉.*记忆|那条记忆.*删)", body):
            if "forget" not in wanted:
                wanted.append("forget")
        elif not _wants_edit_schedule(text):
            # 只有像「说话方式」的才当人设，别把「把课表发我看看」也存成规矩
            if re.search(
                r"(以后|下次|说话|回答|语气|风格|称呼|叫我|别叫|别再|别说|别用|别在|"
                r"不要叫|不要提|人设|规矩|简短|短一点|长一点|字以内)",
                body,
            ):
                if "edit_persona" not in wanted:
                    wanted.append("edit_persona")
    # 「二课」在 _probe_text 里被摘掉了（防止被当成课表），但用户问的就是活动，
    # 不复原回来就会出现「这些都什么时候的」这种没有领域词的追问，模型只能干瞪眼
    if ("二课" in text or "第二课堂" in text) and "query_activities" not in wanted:
        wanted.append("query_activities")
    # 问到「我的报名」就只查这个，别再拿公共活动列表凑数
    if "query_my_activities" in wanted:
        wanted = [tool for tool in wanted if tool != "query_activities"]
    return wanted


def _missing_tools(text: str, used: list[str]) -> list[str]:
    """用户提到但模型没调的工具，由代码补齐。"""
    return [tool for tool in _wanted_tools(text) if tool not in used]


# 补调时得把用户原话塞进工具的文本参数。空着传等于白补：
# query_knowledge 的 query 为空会被知识接口直接拒（「知识库参数错误: query 必填」），
# 模型看到这条报错只能说「不确定」，反而比不调更差。
_AUTO_TEXT_ARG = {
    "query_knowledge": "query",
    "query_canteen": "keyword",
    "query_college": "keyword",
}


def _auto_args(tool: str, text: str) -> dict:
    key = _AUTO_TEXT_ARG.get(tool)
    return {key: text} if key else {}


def _domain_hint(text: str) -> str:
    """一句话提到多件事时，把该调的工具点名写进 system，防止模型漏调。"""
    hits = _wanted_tools(text)
    if len(hits) < 2:
        return ""
    return f"\n\n这句话涉及 {len(hits)} 件事，必须分别调用这些工具，一个都不能漏：{', '.join(hits)}。"


def _memory_block(chat_id: str | None) -> str:
    if not chat_id:
        return ""
    from app.tools import memory

    items = memory.all_items(chat_id)
    if not items:
        return ""
    lines = "\n".join(f"- {item['content']}" for item in items)
    return (
        "\n\n【你记住的关于用户的事】\n" + lines +
        "\n（私聊里合适时自然用上，不要逐条复述，也不要说「根据我的记忆」；"
        "群里不要主动说出来。）"
    )


def _persona_rules_block() -> str:
    from app.tools import persona_rules

    items = persona_rules.all_items()
    if not items:
        return ""
    lines = "\n".join(f"- {item['content']}" for item in items)
    return (
        "\n\n【主人给你定的额外规矩（优先级高于默认风格）】\n" + lines +
        "\n（照这些做，不要逐条复述，也不要说「根据我的设定」。）"
    )


def _correction_block(text: str) -> str:
    """用户以前纠正过的类似问法，注入提示词避免再错。"""
    if not text:
        return ""
    try:
        from app.tools import intent_logs

        rows = intent_logs.similar_corrections(text, limit=2)
    except Exception:
        return ""
    if not rows:
        return ""
    lines = ["", "【用户纠正过的历史】"]
    for row in rows:
        lines.append(
            f"- 问「{row.get('question', '')}」时，用户说的是：{row.get('correction', '')}"
        )
    return "\n".join(lines)


def _fix_numbering(text: str) -> str:
    """把模型跳号的编号列表重排成 1,2,3。"""
    nums = [int(m.group(1)) for m in re.finditer(r"^(\d+)[.、]", text, re.M)]
    if len(nums) < 2 or nums == list(range(1, len(nums) + 1)):
        return text
    counter = {"n": 0}

    def repl(match: re.Match) -> str:
        counter["n"] += 1
        return f"{counter['n']}. "

    return re.sub(r"^\d+[.、]\s*", repl, text, flags=re.M)


def _system_prompt(text: str = "", chat_id: str | None = None) -> str:
    from app.config import load_persona

    from app.agent import tone

    now = datetime.now()
    today = f"{now:%Y-%m-%d} 周{'一二三四五六日'[now.weekday()]} {now:%H:%M}"
    base = (
        SYSTEM_PROMPT.replace("{today}", today)
        + _domain_hint(text)
        + _memory_block(chat_id)
        + _persona_rules_block()
        + _correction_block(text)
        + context.recent_turns_block(chat_id)
    )
    base = base + "\n" + tone.prompt_hint()
    persona = load_persona()
    return f"{persona}\n\n{base}" if persona else base

MAX_ROUNDS = 2


def _post_write_scope(text: str) -> str:
    """改完课表后重查的范围。"""
    if "明天" in text:
        return "tomorrow"
    if "今天" in text or "今日" in text:
        return "today"
    if "下周" in text:
        return "next_week"
    return "this_week"


_DOMAIN_WORDS = {
    "schedule": ("课", "上课", "课表"),
    "weather": ("天气", "下雨", "气温", "多少度", "带伞"),
    "balance": ("余额", "饭卡", "校园卡", "一卡通"),
    "classroom": ("空教室", "自习", "看书"),
    "sunrun": ("长跑", "跑步"),
    "activities": ("活动", "讲座", "比赛"),
}
# 「还有」不算并列语气——「还有多少钱」是问余额，不是两件事
_COMPARE_HINTS = ("和", "比", "哪个", "哪周", "哪门", "哪天", "还是", "都", "顺便")


def _needs_data(text: str, chat_id: str | None) -> bool:
    """这句话要不要查校园数据。

    短追问（「那后天呢」）本身没有关键词，但上一轮刚查过数据，
    也必须继续查——否则模型会照着上下文顺手编一份课表。
    """
    head = (text or "").split("【链接正文】")[0]
    # 表情包只是一张图，不该因为描述里出现「吃」这类字就去查数据。
    # 真被问到时（这啥意思）走闲聊就行，工具帮不上忙。
    if "表情包" in head:
        return False
    # 「修改木鱼：」后面的都是写操作（改课表/记忆/人设），必须走工具
    if _wants_edit_schedule(text):
        return True
    from app.agent import schedule_edit

    if schedule_edit.has_keyword(text):
        return True
    if any(k in head for k in _DATA_KEYWORDS):
        return True
    if chat_id and len(head) <= 10:
        from app.agent.router import _is_bare_follow_up

        previous = context.last_reply(chat_id)
        if "（已查询：" in previous and _is_bare_follow_up(text):
            return True
    return False


def is_multi_intent(text: str) -> bool:
    """一句话里涉及两个以上领域，或带对比/并列语气——交给 Agent 统筹。"""
    if not text:
        return False
    text = text.lower()
    # 「二课」是活动不是课表，先把这两个词摘掉再判领域
    probe = text.replace("第二课堂", "").replace("二课", "")
    domains = {d for d, words in _DOMAIN_WORDS.items() if any(w in probe for w in words)}
    if len(domains) >= 2:
        return True
    if domains and any(h in text for h in _COMPARE_HINTS) and len(text) >= 6:
        return True
    return False


def run(text: str, chat_id: str | None = None) -> str | None:
    """跑一轮 Agent。失败或没调用工具时返回 None，由调用方降级。"""
    if not text:
        return None

    messages: list[dict[str, Any]] = [{"role": "system", "content": _system_prompt(text, chat_id)}]
    if chat_id:
        for item in context.recent(chat_id, 10):
            role = "user" if item.get("role") == "user" else "assistant"
            content = item.get("text") or ""
            if content:
                messages.append({"role": role, "content": content[:300]})
    messages.append({"role": "user", "content": text})

    needs_data = _needs_data(text, chat_id)
    # 写操作指令必须强制调工具：否则闲聊模型会自己编一句「要加前缀」或「没有删课这一项」
    if "修改木鱼" in text or (
        any(k in text for k in ("清掉", "删掉", "删除", "加课", "删课")) and "课" in text
    ):
        needs_data = True
    tools = TOOLS
    # 「修改木鱼：清掉我加的课」是改课表，不是改人设；
    # 把 edit_persona 从候选里拿掉，避免模型看到前缀就选错工具
    if "修改木鱼" in text and any(k in text for k in ("课", "教室", "课表", "上课")):
        tools = [t for t in TOOLS if t["function"]["name"] != "edit_persona"]
    # 「为什么晚自习改自习室」要查课表解释，别让它去翻图书馆 FAQ
    if "为什么" in text and any(k in text for k in ("课", "自习", "教室")):
        tools = [t for t in TOOLS if t["function"]["name"] == "query_schedule"]
    used_tools: list[str] = []
    for round_index in range(MAX_ROUNDS):
        if needs_data:
            # 查校园数据走功能模型：实测 glm-4-flash 的 required 调用最稳
            choice = "required" if round_index == 0 else "auto"
            reply = chat_with_tools(messages, tools, tool_choice=choice)
        else:
            # 闲聊和联网搜索走聊天模型
            reply = chat_conversation(messages, tools, tool_choice="auto")
        if not reply:
            return None

        tool_calls = reply.get("tool_calls") or []
        if not tool_calls:
            content = re.sub(r"（已查询：[^）]*）", "", (reply.get("content") or "")).strip()
            # 有些模型把工具调用吐成 XML 文本，当失败处理，让上层降级
            if not content or "<tool_call>" in content or "<arg_key>" in content:
                return None
            # 改课表时不让模型转述：直接用工具结果 + 改完后的课表
            if "edit_schedule" in used_tools:
                writes = [
                    m["content"] for m in messages
                    if m.get("role") == "tool" and m.get("tool_call_id") != "post_write_query"
                ]
                fresh = [
                    m["content"] for m in messages
                    if m.get("tool_call_id") == "post_write_query"
                ]
                if writes:
                    parts = [writes[0]]
                    if fresh:
                        parts.append(fresh[-1])
                    return "\n\n".join(parts)
            suffix = ""
            if used_tools:
                suffix = f"\n\n（已查询：{', '.join(_tool_label(n) for n in used_tools)}）"
            evidence = [m["content"] for m in messages if m.get("role") == "tool"]
            # 工具拒绝执行时，别让模型把拒绝粉饰成成功
            refusals = [
                m["content"]
                for m in messages
                if m.get("role") == "tool"
                and "要先说「修改木鱼：」" in (m.get("content") or "")
            ]
            if refusals:
                return refusals[0]
            return _strip_hallucination(_fix_numbering(content) + suffix, evidence)

        messages.append(reply)
        for call in tool_calls:
            fn = call.get("function") or {}
            name = fn.get("name") or ""
            raw_args = fn.get("arguments") or "{}"
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
            except json.JSONDecodeError:
                args = {}
            used_tools.append(name)
            result = execute(name, args if isinstance(args, dict) else {}, text, chat_id)
            messages.append({
                "role": "tool",
                "tool_call_id": call.get("id") or name,
                "content": result,
            })

        # 改完课表必须重新查一次：同一轮可能先查后改，回答会拿写之前的旧快照
        if "edit_schedule" in used_tools:
            wrote_at = used_tools.index("edit_schedule")
            if "query_schedule" not in used_tools[wrote_at + 1:]:
                scope = _post_write_scope(text)
                fresh = execute("query_schedule", {"scope": scope}, text, chat_id)
                fake = {
                    "id": "post_write_query",
                    "type": "function",
                    "function": {
                        "name": "query_schedule",
                        "arguments": json.dumps({"scope": scope}),
                    },
                }
                messages.append({"role": "assistant", "content": "", "tool_calls": [fake]})
                messages.append({
                    "role": "tool",
                    "tool_call_id": "post_write_query",
                    "content": fresh,
                })
                used_tools.append("query_schedule")

        # 模型漏调了用户明确问到的东西（常见：三件事只查两件），代码替它补上
        if round_index == 0:
            missing = _missing_tools(text, used_tools)
            if missing:
                fake_calls = [
                    {"id": f"auto_{index}", "type": "function",
                     "function": {"name": tool,
                                  "arguments": json.dumps(_auto_args(tool, text), ensure_ascii=False)}}
                    for index, tool in enumerate(missing)
                ]
                messages.append({"role": "assistant", "content": "", "tool_calls": fake_calls})
                for call in fake_calls:
                    tool = call["function"]["name"]
                    used_tools.append(tool)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": execute(tool, _auto_args(tool, text), text, chat_id),
                    })

    # 两轮之后还没给答案：拿最后一次工具结果，让模型做一次纯措辞
    last_results = [m["content"] for m in messages if m.get("role") == "tool"]
    if last_results:
        summary_prompt = (
            "根据下面的查询结果，用中文简洁回答用户的问题，不要编造：\n\n"
            + "\n\n".join(last_results[-3:])
            + f"\n\n用户问的是：{text}"
        )
        text_reply = chat(summary_prompt, max_tokens=200)
        if text_reply:
            suffix = f"\n\n（已查询：{', '.join(_tool_label(n) for n in used_tools)}）" if used_tools else ""
            return _strip_hallucination(text_reply + suffix, last_results)
    return None


_LABELS = {
    "query_schedule": "课表",
    "query_empty_rooms": "空教室",
    "query_balance": "校园卡",
    "query_electricity": "电费",
    "query_weather": "天气",
    "query_activities": "校园活动",
    "query_my_activities": "我的二课报名",
    "query_canteen": "食堂",
    "query_sunrun": "阳光长跑",
    "query_library": "图书馆",
    "query_college": "学院信息",
    "query_knowledge": "校园知识",
    "query_mail": "邮箱",
    "query_consumption": "消费记录",
    "query_semester_report": "学期报告",
    "web_search": "联网搜索",
    "web_fetch": "网页",
    "edit_schedule": "课表修改",
    "edit_persona": "人设",
    "remember": "记忆",
    "forget": "记忆",
    "list_memory": "记忆",
}


def _tool_label(name: str) -> str:
    return _LABELS.get(name, name)
