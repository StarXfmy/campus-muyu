"""Agent 工具注册表：定义 LLM 可调用的工具 + 执行分发。

设计原则：
- 工具描述要写清「什么时候该用」，模型靠这个选工具
- 执行直接复用 handlers 的现成逻辑，行为与规则路径一致
- 返回值是给模型看的纯文本，控制长度避免撑爆上下文
"""
from __future__ import annotations

import re
from typing import Any, Callable

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "query_schedule",
            "description": "查询课表，含上课时间、教室、任课老师。用户问今天/明天/后天有什么课、本周/下周课表、某门课谁教时用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "scope": {
                        "type": "string",
                        "enum": ["today", "tomorrow", "day_after_tomorrow", "this_week", "next_week", "all"],
                        "description": "today=今天, tomorrow=明天, day_after_tomorrow=后天, this_week=本周, next_week=下周, all=整学期课程清单（含老师）",
                    },
                    "weekday": {
                        "type": "integer",
                        "description": "星期几(1=周一…7=周日)。只在用户明确说「周三」「星期五」时填",
                    },
                },
                "required": ["scope"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_schedule",
            "description": (
                "改用户自己的课表（加课、删课、清空自定义）。只改本机显示，不动学校系统。"
                "用户说「周三第3节加上高数补课」「周三第3节不上了」「把我加的课都清掉」时用。"
                "改完学校课表照常刷新，自定义会一直叠在上面。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["add", "remove", "clear", "undo"],
                        "description": "add=加课, remove=删课, clear=清空所有自定义, undo=撤销最近一次改动",
                    },
                    "course": {"type": "string", "description": "课名，add 时必填"},
                    "day": {"type": "integer", "description": "星期几（1=周一…7=周日）"},
                    "period": {"type": "integer", "description": "起始节次，如 3"},
                    "end_period": {"type": "integer", "description": "结束节次；只改一节就不填"},
                    "location": {"type": "string", "description": "教室，如 6教201"},
                    "teacher": {"type": "string", "description": "老师，可不填"},
                    "weeks": {
                        "type": "string",
                        "description": "生效周次。this_week=只改这周, next_week=只改下周, 1-20 / 1,3,5=指定周次；不填=整学期",
                    },
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_persona",
            "description": (
                "改木鱼的说话风格和行为规矩，全平台（微信+飞书）立即生效。"
                "主人说「修改木鱼：以后回答短一点」「修改木鱼：别用感叹号」「修改木鱼：别叫我主人」时用。"
                "只记「木鱼该怎么说话」；关于用户本人的事实（喜欢什么、几点睡）要用 remember。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["add", "remove", "list", "clear"],
                        "description": "add=加一条规矩, remove=删掉一条, list=看现在有哪些, clear=全清",
                    },
                    "content": {"type": "string", "description": "一句话写清楚规矩，如「回答不超过 3 行」"},
                    "keyword": {"type": "string", "description": "删除时用来匹配的关键词"},
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_empty_rooms",
            "description": "查询空闲教室（找自习的地方）。用户问「哪有空教室」「找个地方看书」时用。用户说了上午/下午/晚上要按 period 传，别用默认。",
            "parameters": {
                "type": "object",
                "properties": {
                    "building": {
                        "type": "string",
                        "description": "楼名，如 正心楼/创智楼/青越。省略时默认正心楼",
                    },
                    "floor": {"type": "integer", "description": "楼层数字，如 4。只有用户明确说「几楼」时才传；「18:30」「晚上六点半」这类是时间不是楼层，别传 floor"},
                    "period": {
                        "type": "string",
                        "enum": ["now", "morning", "afternoon", "evening"],
                        "description": "now=现在, morning=上午(1-4节), afternoon=下午(5-8节), evening=晚上(9-11节)。用户说「下午想看书」就传 afternoon",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_balance",
            "description": "查询校园卡余额（饭卡钱）。用户问饭卡、校园卡、一卡通还剩多少钱时用。注意：问「电费」时不要用这个，要用 query_electricity。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_electricity",
            "description": "查询宿舍电费余额（宿舍还剩多少电、要不要充电费）。用户问「电费」「宿舍还有多少电」「空调费」时用；这和校园卡余额是两笔钱，不要混。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_weather",
            "description": "查询天气和空气质量。用户问天气、要不要带伞、热不热、明天/后天天气时用。问的是哪一天就传 when，别忘了传——不传只有今天的。",
            "parameters": {
                "type": "object",
                "properties": {
                    "when": {
                        "type": "string",
                        "enum": ["今天", "明天", "后天", "大后天"],
                        "description": "问哪一天就传哪一天；用户没说就传「今天」",
                    },
                    "city": {
                        "type": "string",
                        "description": "城市名，照用户原话传（如 绍兴、杭州、黑龙江佳木斯、佳木斯市向阳区）；句子里出现任何地名都要传，没说才省略（默认校区所在地）",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_activities",
            "description": "查询正在报名的二课活动公共目录（不分个人）。用户问「有什么活动」「这周有啥活动」「绍兴/下沙有什么活动」「二课能报什么」时用。用户点名校区才传 campus；问自己报了哪些要用 query_my_activities，不要拿这个冒充。",
            "parameters": {
                "type": "object",
                "properties": {
                    "campus": {
                        "type": "string",
                        "enum": ["绍兴", "下沙"],
                        "description": "用户点名哪个校区就传哪个；没说校区就不传，返回全部",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_my_activities",
            "description": "查用户自己报名了哪些二课活动。接口没有「我的报名」能力，调用后如实告诉他去哪儿看，不要拿公共活动列表冒充。用户问「我报了哪些活动」「我的报名」「二课报名了啥」时用。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_canteen",
            "description": "查**绍兴校区**食堂有哪些店、卖什么菜、多少钱、同学怎么评价。只收录绍兴校区，下沙和其他校区没有数据——用户问别处的食堂时不要用这个工具，改用 web_search 搜。用户问「吃什么」「吃啥」「食堂有啥好吃的」「推荐吃的」时用；可以带上想吃的类型或偏好。",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {
                        "type": "string",
                        "description": "想吃的类型或偏好，如 面、饭、辣、便宜、轻食；用户没说就省略",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_sunrun",
            "description": "查询阳光长跑进度：已跑次数、总里程、平均速度、还差几次。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_library",
            "description": "查询图书馆实时座位空位情况。用户问图书馆有没有座位、想预约座位时用。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "联网搜索实时信息：新闻、天气、最新版本、价格、比赛结果、任何需要最新数据的问题。遇到不认识的名词、网络用语、外号、梗时也用它查证，不要凭空猜。校园内部数据不要用它。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词，尽量具体"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": "读取网页正文。用户发来链接、或需要看某个具体页面的内容时用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "完整网址，含 http(s)://"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_college",
            "description": (
                "查杭电某个学院的官网信息：通知公告、专业设置、师资队伍、学院领导、"
                "联系方式、科研平台。用户提到具体学院（如电子信息学院、计算机学院）"
                "并问该学院的任何信息时用。用户没说哪个学院就不要调，先问清楚。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "college": {
                        "type": "string",
                        "description": "学院名称，如「电子信息学院」「计算机学院」「会计学院」",
                    },
                    "keyword": {
                        "type": "string",
                        "description": "要查什么，如「通知」「专业」「师资」「院长」「联系方式」「科研」。不填默认查通知公告",
                    },
                },
                "required": ["college"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_knowledge",
            "description": "查学校规定、政策、办事流程、奖学金申请条件等。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "要查的问题原文"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remember",
            "description": (
                "记住关于用户本人的事，以后每轮都会看到。"
                "主人带「修改木鱼：」前缀说「记住…」「记一下…」时用。"
                "只记事实和偏好（口味、作息、习惯）；不要记数字、密码、学号、一次性的信息。"
                "改木鱼自己的说话方式要用 edit_persona，不是这个。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "一句话写清楚要记住什么，如「群里不要提私聊」「周五交高数」"},
                    "remind_at": {
                        "type": "string",
                        "description": "到点提醒的时间，格式 YYYY-MM-DD HH:MM。用户说了「周五」「明天下午3点」这类时间才填；没说就不填，不要自己编。",
                    },
                },
                "required": ["content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "forget",
            "description": "忘掉之前记住的关于用户的某条。主人带「修改木鱼：」前缀说「忘掉…」「删掉那条记忆」时用，按关键词匹配。",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "要忘掉的内容里的关键词"},
                },
                "required": ["keyword"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_memory",
            "description": "列出当前记住的所有条目。用户问「你记住了什么」「你记得哪些」时用。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_mail",
            "description": "查杭电邮箱的未读邮件和最近几封邮件标题。用户问「有没有新邮件」「邮箱有啥」「谁给我发邮件了」时用。只读，不发信。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_consumption",
            "description": "查校园卡消费流水：最近几笔、按天汇总、常去的店、最大一笔。用户问「我花了多少钱」「这个月消费」「饭卡钱花哪了」时用。注意：这是消费记录，不是余额，余额用 query_balance。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_semester_report",
            "description": "生成学期校园生活报告：上了多少节课、常去哪栋楼、饭卡花销、阳光跑里程。用户问「这学期过得怎么样」「学期总结」「学期报告」时用。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
]


def _clip(text: str, limit: int = 700) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + "\n…（已截断）"


def _can_write(chat_id: str | None) -> bool:
    """改课表/记忆/人设只给主人。

    微信必须是他本人的私聊，群里谁都能喊一嗓子就乱了；
    别人的私聊也不行。飞书侧沿用原来的信任范围。
    """
    from app import config

    if not chat_id:
        return False
    if chat_id.startswith("wx:"):
        owner = (config.WECHAT_OWNER_ID or "wxid_zn3sbb4pla0t12").strip()
        return chat_id == f"wx:private:{owner}"
    return True


def execute(name: str, args: dict[str, Any], user_text: str = "",
            chat_id: str | None = None) -> str:
    """执行工具，返回给模型看的纯文本。异常一律转成可读说明，不让 Agent 崩。

    user_text 是用户原话，留给模型漏填参数时兜底（比如忘传城市名）。
    """
    from app.agent import handlers

    try:
        if name == "edit_schedule":
            from app.agent import schedule_edit
            from app.tools import schedule_store

            if not _can_write(chat_id):
                return "这个本鱼不能改。"
            if not schedule_edit.has_keyword(user_text):
                return "改课表要先说「修改木鱼：」。没这句话本鱼不动课表。"

            a = args or {}
            parsed = schedule_edit.parse(user_text)

            # 原话解析出来的字段优先于模型：模型常把「改成」当加课、把「这周」漏掉
            action = parsed.get("action") or str(a.get("action") or "").strip()
            day = parsed.get("day")
            period = parsed.get("period")
            end = parsed.get("end_period")
            course = parsed.get("course") or ""
            location = parsed.get("location") or ""
            weeks = parsed.get("weeks") or ""

            if day is None:
                try:
                    day = int(a.get("day"))
                except (TypeError, ValueError):
                    day = None
            if period is None:
                try:
                    period = int(a.get("period"))
                except (TypeError, ValueError):
                    period = None
            if end is None:
                try:
                    end = int(a.get("end_period")) if a.get("end_period") not in (None, "", 0) else None
                except (TypeError, ValueError):
                    end = None
            if not course:
                course = str(a.get("course") or "").strip()
            if not location:
                location = str(a.get("location") or "").strip()
            if not weeks:
                weeks = str(a.get("weeks") or "").strip()

            if action == "clear":
                n = schedule_store.clear_custom()
                return "已清空 %d 条自定义课。学校课表原样恢复。" % n

            if action == "undo":
                last = schedule_store.undo_last()
                if not last:
                    return "没有可撤销的自定义改动。"
                return "已撤销上一条（%s：周%s 第 %s 节）。" % (
                    "加课" if last["action"] == "add" else "删课",
                    "一二三四五六日"[int(last["day"]) - 1],
                    last["period"],
                )

            if day is None or period is None:
                return "没说要改哪天第几节，本鱼下不了手。"
            if not (1 <= day <= 7 and 1 <= period <= 12):
                return "星期要在 1-7 之间、节次在 1-12 之间。"

            # 「这周 / 下周」翻译成具体周次，改动只对那周生效
            week_value = ""
            if weeks in ("this_week", "本周", "这周", "这个星期"):
                current = schedule_store.current_week()
                if not current:
                    return "本鱼这边没有周次信息（课表缓存没刷新），先刷新一次再改。"
                week_value = str(current)
            elif weeks in ("next_week", "下周", "下个星期"):
                current = schedule_store.current_week()
                if not current:
                    return "本鱼这边没有周次信息（课表缓存没刷新），先刷新一次再改。"
                week_value = str(current + 1)
            else:
                week_value = weeks

            week_cn = "一二三四五六日"[day - 1]
            span = f"第 {period} 节" if not end or end == period else f"第 {period}-{end} 节"
            scope = f"（只改第 {week_value} 周）" if week_value else ""

            if action == "remove":
                schedule_store.remove_custom(day, period, end, weeks=week_value)
                return "已把周%s %s从课表里去掉%s。不对就说「撤销」。" % (week_cn, span, scope)

            if action in ("add", "replace"):
                if not course:
                    return "要加什么课？说个课名。"
                if action == "replace":
                    schedule_store.remove_custom(day, period, end, weeks=week_value)
                schedule_store.add_custom(
                    course, day, period, end_period=end,
                    location=location, weeks=week_value,
                )
                where = f"（{location}）" if location else ""
                verb = "改成" if action == "replace" else "加上"
                return "已把周%s %s%s %s%s%s。不对就说「撤销」。" % (
                    week_cn, span, verb, course, where, scope,
                )

            return "没听明白是加课还是删课，重说一句。"

        if name == "query_schedule":
            scope = (args or {}).get("scope") or "today"
            weekday = (args or {}).get("weekday")
            if weekday and 1 <= int(weekday) <= 7:
                return _clip(handlers.handle_weekday(int(weekday)))
            if scope == "day_after_tomorrow":
                # 让代码算「后天是周几」，别让模型自己推日期
                from datetime import datetime

                target = datetime.now().isoweekday() + 2
                if target <= 7:
                    return _clip(handlers.handle_weekday(target))
                return "后天已经跨到下周了，直接说「下周一有啥课」更准。"
            mapping: dict[str, Callable[[], str]] = {
                "today": lambda: handlers.handle_schedule("today"),
                "tomorrow": lambda: handlers.handle_schedule("tomorrow"),
                "this_week": handlers.handle_week,
                "next_week": handlers.handle_next_week,
                "all": handlers.handle_all_courses,
            }
            return _clip(mapping.get(scope, handlers.handle_schedule)())

        if name == "query_empty_rooms":
            building_text = (args or {}).get("building")
            floor = (args or {}).get("floor")
            floor = int(floor) if isinstance(floor, (int, float)) else None
            period = (args or {}).get("period") or "now"
            sections = {
                "morning": [1, 2, 3, 4],
                "afternoon": [5, 6, 7, 8],
                "evening": [9, 10, 11],
            }.get(period)
            return _clip(handlers.handle_empty_room(None, building_text, floor, sections), 800)

        if name == "query_balance":
            return handlers.handle_balance()

        if name == "query_electricity":
            return _clip(handlers.handle_electric(), 800)

        if name == "query_weather":
            when = str((args or {}).get("when") or "").strip()
            city = str((args or {}).get("city") or "").strip()
            if user_text:
                # 模型漏填参数就从原话里再捞一次，别再默默回落到绍兴/今天
                if not when:
                    for word in ("大后天", "后天", "明天"):
                        if word in user_text:
                            when = word
                            break
                if not city:
                    from app.tools import weather as weather_tool

                    place = weather_tool.resolve(user_text)
                    if not place.get("local"):
                        city = place["name"]
            return _clip(handlers.handle_weather(f"{when}{city}".strip()), 800)

        if name == "query_my_activities":
            return _clip(handlers.handle_my_activities(), 800)

        if name == "query_canteen":
            keyword = str((args or {}).get("keyword") or "").strip()
            if not keyword:
                # 模型没传关键词就从原话里直接取
                keyword = user_text
            return _clip(handlers.handle_canteen(keyword), 900)

        if name == "query_activities":
            campus = (args or {}).get("campus")
            return _clip(handlers.handle_activities(campus=str(campus) if campus else None), 1800)

        if name == "query_sunrun":
            return _clip(handlers.handle_sunrun(), 800)

        if name == "query_library":
            return _clip(handlers.handle_library(), 800)

        if name == "query_college":
            college_name = str((args or {}).get("college") or "")
            keyword = str((args or {}).get("keyword") or "")
            return _clip(handlers.handle_college(f"{college_name} {keyword}".strip()), 900)

        if name == "query_knowledge":
            return _clip(handlers.handle_knowledge(str((args or {}).get("query") or "")))

        if name == "remember":
            from app.agent import schedule_edit
            from app.tools import memory

            if not _can_write(chat_id):
                return "这个本鱼不能改。"
            if not schedule_edit.has_keyword(user_text):
                return "要记东西先说「修改木鱼：」，本鱼不会自己乱记。"
            content = schedule_edit.strip_keyword(
                str((args or {}).get("content") or "")
            ).strip()
            if not content:
                content = schedule_edit.strip_keyword(user_text)
            content = re.sub(r"^(?:记住|记一下|帮我记|记着)\s*[:：]?", "", content).strip()
            if not content:
                return "要记什么？说一句话。"
            if not chat_id:
                return "这次没有会话标识，记不下来。"
            remind_at = str((args or {}).get("remind_at") or "").strip()
            if not remind_at:
                remind_at = memory.parse_remind_at(content)
            memory.add(chat_id, content, remind_at=remind_at)
            if remind_at:
                return "记下了：%s\n到点在飞书提醒你（%s），并问要不要删。" % (
                    content[:120], remind_at,
                )
            return "记下了：%s" % content[:120]

        if name == "forget":
            from app.agent import schedule_edit
            from app.tools import memory

            if not _can_write(chat_id):
                return "这个本鱼不能改。"
            if not schedule_edit.has_keyword(user_text):
                return "要删记忆先说「修改木鱼：」，本鱼不会自己乱删。"
            keyword = schedule_edit.strip_keyword(
                str((args or {}).get("keyword") or "")
            ).strip()
            if not keyword or not chat_id:
                return "要忘掉哪条？给个关键词。"
            n = memory.remove(chat_id, keyword)
            return "忘了 %d 条。" % n if n else "没找到包含「%s」的记忆。" % keyword

        if name == "edit_persona":
            from app.agent import schedule_edit
            from app.tools import persona_rules

            if not _can_write(chat_id):
                return "这个本鱼不能改。"
            if not schedule_edit.has_keyword(user_text):
                return "改人设要先说「修改木鱼：」。没这句话本鱼不动自己。"
            action = str((args or {}).get("action") or "").strip() or "add"
            if action == "add":
                content = schedule_edit.strip_keyword(
                    str((args or {}).get("content") or "")
                ).strip()
                if not content:
                    content = schedule_edit.strip_keyword(user_text)
                if not content:
                    return "要加什么规矩？说一句。"
                new_id = persona_rules.add(content)
                if new_id == -1:
                    return "规矩太多了（上限 %d 条），先删几条再加。" % persona_rules.MAX_ITEMS
                if new_id == 0:
                    return "这条已经有了，不重复加。"
                return "记下了：%s" % content[:120]
            if action == "remove":
                keyword = schedule_edit.strip_keyword(
                    str((args or {}).get("keyword") or "")
                ).strip()
                if not keyword:
                    return "要删哪条？给个关键词。"
                n = persona_rules.remove(keyword)
                return "删了 %d 条。" % n if n else "没找到包含「%s」的规矩。" % keyword
            if action == "list":
                items = persona_rules.all_items()
                if not items:
                    return "现在没有额外规矩。"
                return "额外规矩有：\n" + "\n".join("- " + i["content"] for i in items)
            if action == "clear":
                n = persona_rules.clear()
                return "已清空 %d 条额外规矩，恢复原始人设。" % n
            return "action 只能是 add / remove / list / clear。"

        if name == "list_memory":
            from app.tools import memory

            if not chat_id:
                return "这次没有会话标识，查不了。"
            items = memory.all_items(chat_id)
            if not items:
                return "现在什么都没记。"
            return "记住的有：\n" + "\n".join("- " + i["content"] for i in items)

        if name == "query_mail":
            return _clip(handlers.handle_mail(), 1200)

        if name == "query_consumption":
            return _clip(handlers.handle_consumption(), 1200)

        if name == "query_semester_report":
            return _clip(handlers.handle_semester_report(), 1200)

        if name == "web_fetch":
            from app.tools import webpage

            return webpage.fetch(str((args or {}).get("url") or ""))

        if name == "web_search":
            from app.llm.client import web_search

            results = web_search(str((args or {}).get("query") or ""), 5)
            if not results:
                return "没搜到结果，换个关键词试试。"
            blocks = []
            for item in results[:5]:
                title = item.get("title") or ""
                content = _clip(item.get("content") or "", 320)
                blocks.append(f"{title}\n{content}")
            return "\n\n".join(blocks)

        return f"未知工具：{name}"
    except Exception as exc:
        return f"工具 {name} 执行失败：{type(exc).__name__}: {exc}"
