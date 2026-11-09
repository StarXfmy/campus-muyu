"""意图识别：纯规则匹配，稳定可测；LLM 作为可选兜底留待后续接入。"""
from __future__ import annotations

import re
from dataclasses import dataclass

INTENT_SCHEDULE = "schedule"
INTENT_SCHEDULE_WEEK = "schedule_week"
INTENT_SCHEDULE_NEXT_WEEK = "schedule_next_week"
INTENT_COMPARE_WEEK = "compare_week"
INTENT_SCHEDULE_WEEKDAY = "schedule_weekday"
INTENT_SCHEDULE_ALL = "schedule_all"
INTENT_SCHEDULE_IMAGE = "schedule_image"
INTENT_BALANCE = "balance"
INTENT_EMPTY_ROOM = "empty_room"
INTENT_KNOWLEDGE = "knowledge"
INTENT_OFFICE = "office"
INTENT_ACTIVITIES = "activities"
INTENT_CANTEEN = "canteen"
INTENT_MY_ACTIVITIES = "my_activities"
INTENT_ACTIVITY_SIGNUP = "activity_signup"
INTENT_ACTIVITY_ADVICE = "activity_advice"
INTENT_PLAN_DAY = "plan_day"
INTENT_REMIND = "remind"
INTENT_MY_REMINDERS = "my_reminders"
INTENT_CANCEL_REMIND = "cancel_remind"
INTENT_EDIT_REMIND = "edit_remind"
INTENT_MEMORY_ADD = "memory_add"
INTENT_MEMORY_LIST = "memory_list"
INTENT_MEMORY_DELETE = "memory_delete"
INTENT_PUSH_SET = "push_set"
INTENT_EXAM = "exam"
INTENT_HOMEWORK = "homework"
INTENT_BEST_PLACE = "best_place"
INTENT_SEMESTER_REPORT = "semester_report"
INTENT_COLLEGE = "college"
INTENT_SELF_CHECK = "self_check"
INTENT_USAGE = "usage"
INTENT_KB_ADD = "kb_add"
INTENT_EXPENSE_CHART = "expense_chart"
INTENT_MAIL = "mail"
INTENT_MAIL_SEND = "mail_send"
INTENT_ELECTRIC = "electric"
INTENT_DAILY_REPORT = "daily_report"
INTENT_DAILY_REPORT_SET = "daily_report_set"
INTENT_CONSUMPTION = "consumption"
INTENT_STUDY_PLAN = "study_plan"
INTENT_LOSTFOUND = "lostfound"
INTENT_ACTIVITY_CONFLICT = "activity_conflict"
INTENT_SUNRUN = "sunrun"
INTENT_LIBRARY = "library"
INTENT_WEATHER = "weather"
INTENT_NEWS = "news"
INTENT_NEWS_PREF = "news_pref"
INTENT_GRADE = "grade"
INTENT_HELP = "help"
INTENT_UNKNOWN = "unknown"

# 顺序即优先级：先判断更具体的意图
_RULES: list[tuple[str, tuple[str, ...]]] = [
    (INTENT_BEST_PLACE, ("去哪自习", "哪里自习", "去哪看书", "现在去哪", "找个地方自习", "适合自习")),
    (INTENT_EMPTY_ROOM, ("空教室", "空着的教室", "空着", "哪有教室", "哪间教室", "有教室", "有空", "自习", "找不到地方看书")),
    (INTENT_BALANCE, ("余额", "校园卡", "饭卡", "一卡通", "还剩多少", "卡里还有")),
    (INTENT_GRADE, ("成绩", "绩点", "gpa", "分数", "考了多少")),
    (INTENT_EXAM, ("考试安排", "期末考", "几号考", "考试时间", "补考", "什么时候考试")),
    (INTENT_WEATHER, ("天气", "气温", "下雨", "带伞", "冷不冷", "热不热", "多少度")),
    (INTENT_NEWS_PREF, ("新闻偏好", "新闻口味", "多推新闻", "少推新闻")),
    (INTENT_NEWS, ("新闻", "头条", "资讯", "热点", "热搜", "官网", "发布会")),
    (INTENT_SUNRUN, ("阳光长跑", "长跑", "跑步", "跑了几次", "跑了几公里", "还差几次")),
    (INTENT_LIBRARY, ("图书馆座位", "图书馆有座", "图书馆空位", "座位预约", "有座位吗", "自习位", "抢座")),
    (INTENT_LOSTFOUND, ("失物", "丢了", "捡到", "失物招领", "丢东西")),
    (INTENT_STUDY_PLAN, ("复习计划", "学习计划", "怎么复习", "复习安排", "学习安排")),
    # 用量查询放在消费分析之前：「花了多少 token」不能被「花了多少」抢走
    (INTENT_USAGE, ("token", "用量", "调用次数", "模型花费", "周报")),
    (INTENT_EXPENSE_CHART, ("消费图", "花销图", "消费图表", "消费条形图")),
    (INTENT_CONSUMPTION, ("消费", "花了多少", "花销", "消费记录", "流水", "吃饭花了")),
    (INTENT_DAILY_REPORT_SET, ("早报", "每日简报", "每日一敲")),
    (INTENT_DAILY_REPORT, ("今日速览", "今天怎么样", "今日报告", "今天概况", "全部情况", "今天情况")),
    (INTENT_ELECTRIC, ("电费", "宿舍电", "寝室电", "还有电吗", "充多少电")),
    # 发信必须排在「邮件」查询之前，否则「发邮件给…」会被当成查未读
    (INTENT_MAIL_SEND, ("发邮件", "寄邮件", "写邮件", "发封邮件", "发个邮件")),
    (INTENT_MAIL, ("邮箱", "未读邮件", "收邮件", "邮件", "mail")),
    (INTENT_SEMESTER_REPORT, ("学期报告", "学期总结", "这学期总结", "这学期都干了",
                              "这学期过得", "这学期我过得")),
    (INTENT_PUSH_SET, ("关闭推送", "关掉推送", "打开推送", "开启推送", "推送开关", "别推了", "别给我推")),
    (INTENT_MEMORY_DELETE, ("忘掉", "删除记忆", "别记这个", "忘了我说的")),
    (INTENT_MEMORY_LIST, ("你记得什么", "记忆里有什么", "你记住了什么", "你有哪些记忆", "我的记忆")),
    # 三套各认各的词：「提醒」→ 提醒看板，「记录作业」→ 作业 DDL，
    # 「修改木鱼：记住」→ 长期记忆。提醒必须排在作业前面：
    # 「提醒我明天交作业」是提醒，不该被作业看板抢走。
    (INTENT_CANCEL_REMIND, ("取消提醒", "删掉提醒", "删除提醒", "撤销提醒")),
    (INTENT_EDIT_REMIND, ("修改提醒", "改提醒", "提醒改成", "改一下提醒", "提醒时间改")),
    (INTENT_MY_REMINDERS, ("我的提醒", "有哪些提醒", "提醒有哪些", "有什么提醒", "提醒列表", "待提醒")),
    (INTENT_REMIND, ("提醒", "叫我", "别忘了提醒", "记得提醒")),
    (INTENT_HOMEWORK, ("记录作业", "作业", "ddl", "DDL", "欠交")),
    (INTENT_MEMORY_ADD, ("记住", "记一下", "帮我记")),
    (INTENT_ACTIVITY_CONFLICT, ("我能去吗", "冲突吗", "去得了吗", "时间冲突", "能参加吗", "有空去吗")),
    (INTENT_PLAN_DAY, ("怎么安排", "安排一下", "有什么安排", "啥安排", "什么计划", "啥计划",
                       "今天的安排", "明天的安排", "今天安排", "明天安排",
                       "行程", "计划一下", "今天干什么", "明天干什么", "今天干嘛", "明天干嘛",
                       "规划一下", "帮我规划")),
    # 食堂放「推荐」前面：问「推荐吃啥」不该被活动推荐抢走；但也别收「食堂」这种宽词，
    # 「食堂几点开门」是 FAQ，不能被这里截胡
    (INTENT_CANTEEN, ("吃什么", "吃啥", "吃点什么", "有什么好吃", "推荐吃", "吃哪家",
                      "食堂有啥", "食堂有什么", "食堂推荐", "食堂菜单", "哪个窗口", "哪个食堂",
                      "校外", "外卖", "美食")),
    (INTENT_ACTIVITY_ADVICE, ("值得去", "适合我", "好活动", "哪个活动", "活动建议", "有什么活动值得",
                              "推荐活动", "活动推荐")),
    (INTENT_ACTIVITY_SIGNUP, ("我要报名", "帮我报名", "报名参加", "报名活动", "报个名", "我想报名")),
    (INTENT_MY_ACTIVITIES, ("我报名", "我参加", "我报的", "我的活动", "我选的活动", "我报了",
                            "报名了哪些", "报名了啥", "报了哪些", "我的报名", "报名过")),
    (INTENT_ACTIVITIES, ("活动", "二课", "讲座", "比赛", "志愿", "社团招新")),
    (INTENT_COMPARE_WEEK, ("哪周课多", "哪周多", "哪周少", "这周还是下周", "下周和这周")),
    (INTENT_SCHEDULE_IMAGE, ("课表图片", "课表图", "课表截图", "画课表", "课表画出来", "课表的图")),
    (INTENT_SCHEDULE_ALL, ("这学期", "学期课表", "有哪些课", "全部课程", "所有课", "都上什么")),
    (INTENT_SCHEDULE_NEXT_WEEK, ("下周", "下个星期", "下星期")),
    (INTENT_SCHEDULE_WEEK, ("这周", "本周", "这个星期", "下星期几节课")),
    (INTENT_SCHEDULE, ("课表", "有什么课", "有啥课", "上什么课", "上课", "几点上", "明天有课", "今天有课")),
    # 学院信息放在办公地点之前：问「机械学院在哪办公」该去学院站找联系方式，
    # 而不是走通用办公地点知识库
    (INTENT_COLLEGE, ("学院",)),
    (INTENT_OFFICE, ("办公地点", "办公室", "教务处在", "学院在哪", "部门电话", "办公电话", "在哪办", "地址在哪")),
    (INTENT_KNOWLEDGE, (
        "奖学金", "助学金", "规定", "政策", "流程", "怎么申请", "怎么办理", "需要什么材料",
        "图书馆开放", "开放时间", "校历", "什么时候放假", "补办", "报销", "转专业",
    )),
    (INTENT_KB_ADD, ("收录",)),
    (INTENT_SELF_CHECK, ("自检", "自查", "体检一下", "检查一下系统", "系统状态")),
    (INTENT_HELP, ("你好", "hi", "hello", "帮助", "能干什么", "会什么", "菜单", "按钮", "怎么用", "在吗")),
]

# 数字楼栋必须是「3号楼」「3号」「第3教研楼」「3教」；光说「3楼」是楼层，不是楼栋
_BUILDING_PATTERN = re.compile(r"(?:第)?\s*([0-9]{1,2})\s*(?:号\s*楼|号|教研楼|教学楼|教)")
_BUILDING_NAME_PATTERN = re.compile(r"([\u4e00-\u9fa5]{2,4}楼)")
_FLOOR_PATTERN = re.compile(r"([1-9一二三四五六七八九])\s*(?:楼|层)")
_CN_DIGITS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
# 常见楼名，用户经常省略「楼」字（「青越有空教室吗」）
_COMMON_BUILDINGS = (
    "正心楼", "创智楼", "青越楼", "子衿楼", "博约楼", "礼贤楼", "精诚楼", "芯越楼",
    "正心", "创智", "青越", "子衿", "博约", "礼贤", "精诚", "芯越",
)
_WEEKDAY_PATTERN = re.compile(r"(?:周|星期|礼拜)([一二三四五六日天1-7])")
_WEEKDAY_MAP = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "日": 7, "天": 7,
                "1": 1, "2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "7": 7}


@dataclass
class Intent:
    name: str
    building: str | None = None
    building_text: str | None = None
    floor: int | None = None
    weekday: int | None = None
    raw: str = ""


def parse_building(text: str) -> str | None:
    """从「3号楼」「第3教研楼」里抽出楼栋号，返回两位补零形式（03/04/11）。"""
    match = _BUILDING_PATTERN.search(text)
    if match:
        return match.group(1).zfill(2)
    return None


def parse_building_name(text: str) -> str | None:
    """从「正心楼有空教室吗」「青越有空教室吗」里抽出楼名。"""
    for name in _COMMON_BUILDINGS:
        if name in text:
            return name
    match = _BUILDING_NAME_PATTERN.search(text)
    if match:
        name = match.group(1)
        # 「第3教研楼」里的「教研楼」不是楼名，交给 parse_building 按数字楼栋处理
        if name in ("教研楼", "教学楼", "栋"):
            return None
        return name
    return None


def parse_weekday(text: str) -> int | None:
    match = _WEEKDAY_PATTERN.search(text)
    if match:
        return _WEEKDAY_MAP.get(match.group(1))
    return None


def detect(text: str, chat_id: str | None = None) -> Intent:
    """只做规则识别。「明天」这类省略说法沿用上一轮意图，其余返回 unknown。

    认不出的说法不在这里让 LLM 猜意图——猜一次信息太少（「今天有什么安排」
    就被猜成查课表）。改由 handlers.answer 直接交给会调工具的 Agent。
    """
    intent = detect_by_rules(text)
    if intent.name != INTENT_UNKNOWN:
        return intent

    carried = _carry_from_context(text, chat_id)
    if carried:
        return carried
    return intent


def classify_with_llm(text: str, chat_id: str | None = None) -> Intent | None:
    """Agent 也没答上来时的最后兜底：让 LLM 分类一次。"""
    try:
        from app.agent import context
        from app.llm.client import classify_intent

        history = context.recent(chat_id, 10) if chat_id else None
        result = classify_intent(text, history)
    except Exception:
        return None
    if not result:
        return None

    building_text = result.get("building")
    building = None if building_text else parse_building((text or "").lower())
    return Intent(
        name=result["intent"],
        building=building,
        building_text=building_text,
        floor=result.get("floor"),
        weekday=result.get("weekday"),
        raw=text,
    )


_FOLLOW_UP_HINTS = ("明天", "明日", "今天", "今日", "后天", "刚才", "再查", "那")
_SCHEDULE_INTENTS = ("schedule", "schedule_week", "schedule_weekday", "schedule_all")

# 去掉这些词后如果什么都不剩，说明这句只是「那后天呢」式追问，不是在说别的事
_FOLLOW_UP_NOISE = (
    "大后天", "后天", "明天", "明日", "今天", "今日", "本周", "这周", "下周", "上周",
    "星期", "礼拜", "周", "一", "二", "三", "四", "五", "六", "日", "天",
    "那", "呢", "的", "呀", "啊", "嘛", "吧", "了", "再", "查", "看", "帮", "我",
    " ", "?", "？", ",", "，", "。", "!", "！", "~",
)


def _is_bare_follow_up(text: str) -> bool:
    """「那后天呢」「今天呢」是追问；「今天有点累」不是——后者有实质内容，不能套上一轮意图。"""
    rest = text or ""
    for word in _FOLLOW_UP_NOISE:
        rest = rest.replace(word, "")
    return len(rest.strip()) <= 1


def _carry_from_context(text: str, chat_id: str | None) -> Intent | None:
    """上一轮问课表/天气，这轮只说「明天」——沿用上一轮的意图，不重新猜。"""
    if not chat_id:
        return None
    normalized = (text or "").strip()
    if not _is_bare_follow_up(normalized):
        return None
    if not any(hint in normalized for hint in _FOLLOW_UP_HINTS):
        return None

    from app.agent import context

    previous = context.last_intent(chat_id)
    if previous in _SCHEDULE_INTENTS:
        return Intent("schedule", weekday=parse_weekday(normalized), raw=text)
    if previous == "weather":
        return Intent("weather", raw=text)

    # 上一轮走了 Agent 并且查过数据（回复里带「已查询：X」），
    # 这轮的「那后天呢」必须接着查，不能交给模型自由发挥——它会照着格式编。
    if previous == "agent":
        reply = context.last_reply(chat_id) or ""
        for label, name in _QUERY_HINT_INTENTS.items():
            if f"已查询：{label}" in reply:
                return Intent(name, weekday=parse_weekday(normalized), raw=text)
    return None


_QUERY_HINT_INTENTS = {
    "课表": "schedule",
    "校园卡": "balance",
    "空教室": "empty_room",
    "天气": "weather",
    "校园活动": "activities",
    "阳光长跑": "sunrun",
    "图书馆": "library",
}


_GREETING_NOISE = (
    "你好", "您好", "hi", "hello", "在吗", "在么", "帮助", "菜单", "按钮",
    "怎么用", "能干什么", "会什么", "呀", "啊", "呢", "~",
    " ", "?", "？", ",", "，", "。", "!", "！",
)


def _is_greeting(text: str) -> bool:
    """「你好」是打招呼；「你好，5+6=?」不是——后面还有正事。"""
    rest = (text or "").lower()
    for word in _GREETING_NOISE:
        rest = rest.replace(word, "")
    return len(rest.strip()) <= 1


def detect_by_rules(text: str) -> Intent:
    normalized = (text or "").strip().lower()
    if not normalized:
        return Intent(INTENT_UNKNOWN)

    # 「修改木鱼：」是写操作指令，按里面的动词定意图；
    # 否则「记住……提醒我……」会被提醒意图抢走，绕过记忆门禁
    if "修改木鱼" in normalized:
        weekday = parse_weekday(normalized)
        if any(k in normalized for k in ("忘掉", "删除记忆", "别记这个")):
            return Intent(INTENT_MEMORY_DELETE, weekday=weekday, raw=text)
        if any(k in normalized for k in ("记住", "记一下", "帮我记")):
            return Intent(INTENT_MEMORY_ADD, weekday=weekday, raw=text)
        # 「修改木鱼：提醒 62 改成…」按编号改提醒，别被新建提醒抢走
        if re.search(r"提醒\s*\d+", normalized) and not any(
            k in normalized for k in ("取消提醒", "删掉提醒", "删除提醒", "撤销提醒")
        ):
            return Intent(INTENT_EDIT_REMIND, weekday=weekday, raw=text)
        # 「修改木鱼：高数作业交了」也归作业线，别掉给 Agent
        if any(k in normalized for k in ("交了", "写完了", "做完了", "完成了", "上交了", "提交了")):
            return Intent(INTENT_HOMEWORK, weekday=weekday, raw=text)

    # 「改课表 / 调整上课时间地点」交给 Agent（它有 edit_schedule 工具），
    # 否则会被「课」的查询规则接走，只回一句课表
    if any(k in normalized for k in ("改", "调整", "变更", "换", "调课")) and any(
        k in normalized for k in ("课", "教室", "晚自习")
    ):
        return Intent(INTENT_UNKNOWN, weekday=parse_weekday(normalized), raw=text)

    # 单说「提醒」是看板入口；「提醒我…」才走新建提醒
    if normalized.strip(" 。！？!?~") in ("提醒", "看提醒", "看看提醒", "提醒看板"):
        return Intent(INTENT_MY_REMINDERS, raw=text)

    # 「为什么晚自习改自习室」是问课表里那条自定义，不是找空教室
    if "为什么" in normalized and "改" in normalized:
        return Intent(INTENT_UNKNOWN, raw=text)

    building_text = parse_building_name(normalized)
    # 「正心楼4楼」「3楼」「二楼」里的数字都是楼层；楼栋只认「3号楼」「3教」那种
    rest = normalized.replace(building_text, "") if building_text else normalized
    floor = None
    match = _FLOOR_PATTERN.search(rest)
    if match:
        token = match.group(1)
        floor = int(token) if token.isdigit() else _CN_DIGITS.get(token)

    weekday = parse_weekday(normalized)
    # 外部话题别按校园功能答：英文/品牌词 + 活动类词，或特定误词
    if re.search(r"[a-zA-Z]{2,}", normalized) and any(
        k in normalized for k in ("活动", "讲座", "比赛", "学院", "课程")
    ):
        return Intent(INTENT_NEWS, weekday=weekday, raw=text)
    if any(k in normalized for k in ("学院风", "天气之子", "流水线", "新闻专业", "新闻学", "新闻考研")):
        return Intent(INTENT_UNKNOWN, weekday=weekday, raw=text)
    if "周报" in normalized and any(k in normalized for k in ("怎么写", "模板", "格式")):
        return Intent(INTENT_UNKNOWN, weekday=weekday, raw=text)
    # 「提醒我下周三交党史课作业」既有周几又有「课」，别让它掉进课表
    if weekday and "提醒" in normalized and not any(
        k in normalized for k in ("取消提醒", "删掉提醒", "删除提醒", "撤销提醒",
                                  "修改提醒", "改提醒", "提醒列表", "我的提醒", "待提醒")
    ):
        return Intent(INTENT_REMIND, weekday=weekday, raw=text)
    # 「周三有啥课」这类没有「课表」字样，靠星期 + 课程意图词识别
    if weekday and any(k in normalized for k in ("课", "上", "空闲", "空着")):
        if any(k in normalized for k in ("空教室", "空着", "自习", "有空", "有教室")):
            return Intent(INTENT_EMPTY_ROOM, building=None if building_text else parse_building(normalized),
                          building_text=building_text, floor=floor, weekday=weekday, raw=text)
        return Intent(INTENT_SCHEDULE_WEEKDAY, weekday=weekday, raw=text)

    # 「报名XX」「报名参加XX」：句首就是报名动作，走确认卡流程。
    # 规则表里的「我要报名」等短语盖不住这种省略说法，会被当成闲聊丢给 Agent，
    # 而 Agent 手里没有报名工具，只能回「本鱼没权限」。
    if normalized.startswith(("报名", "报名参加", "报名参与", "帮我报", "我要报")):
        if not any(k in normalized for k in ("截止", "时间", "条件", "要求", "怎么", "流程", "入口", "吗？")):
            return Intent(INTENT_ACTIVITY_SIGNUP, weekday=weekday, raw=text)

    # 「XX学院有啥活动/讲座」问的是学院自己的动态，不是二课活动；
    # 带「报名 / 二课 / 值得去」才是二课，交给下面正常规则
    if "学院" in normalized and any(k in normalized for k in ("活动", "讲座", "比赛", "动态")):
        if not any(k in normalized for k in ("二课", "报名", "我能去", "值得去", "志愿", "我报", "我参加")):
            return Intent(INTENT_COLLEGE, weekday=weekday, raw=text)

    # 「今日速览/早报」+ 修改时间或内容 → 设置，不是生成报告
    if any(k in normalized for k in ("今日速览", "早报", "每日简报", "每日一敲")) and any(
        k in normalized for k in ("修改", "改成", "改到", "调整", "设置", "改一下", "时间",
                                  "只发", "只要", "只保留", "只看", "加上", "去掉", "内容", "删掉")
    ):
        return Intent(INTENT_DAILY_REPORT_SET, weekday=weekday, raw=text)

    # 「qoder 相关活动」这类外部话题别当二课
    if "活动" in normalized and any(k in normalized for k in ("相关", "官方", "发布", "最新")):
        return Intent(INTENT_NEWS, weekday=weekday, raw=text)

    # 「多来点/不要某类新闻」是调偏好，不是查新闻
    if "新闻" in normalized and any(
        k in normalized for k in ("多来点", "不要", "少推", "多推", "别推", "不喜欢", "去掉", "屏蔽")
    ):
        return Intent(INTENT_NEWS_PREF, weekday=weekday, raw=text)

    for name, keywords in _RULES:
        if any(kw in normalized for kw in keywords):
            if name == INTENT_HELP and not _is_greeting(normalized):
                continue
            building = None if building_text else (parse_building(normalized) if name == INTENT_EMPTY_ROOM else None)
            return Intent(name, building=building, building_text=building_text if name == INTENT_EMPTY_ROOM else None,
                          floor=floor if name == INTENT_EMPTY_ROOM else None, weekday=weekday, raw=text)
    return Intent(INTENT_UNKNOWN, raw=text)
