"""改东西的指令解析。

模型填参数不稳：会把「改成高数」当 add、漏掉「这周」、把「第 3 节」听成 3 点。
用户原话是唯一可靠的依据，所以能自己解析的字段就不信模型。
"""
from __future__ import annotations

import re

# 只有带这个前缀才允许改课表 / 记忆 / 人设，防止闲聊被误判成指令
KEYWORD_RE = re.compile(r"修改木鱼\s*[:：]?")

WEEKDAY_CN = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "日": 7, "天": 7}
_CN_NUM = {
    "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6,
    "七": 7, "八": 8, "九": 9, "十": 10, "十一": 11, "十二": 12,
}

_WEEKDAY_RE = re.compile(r"(?:周|星期|礼拜)\s*([一二三四五六日天])")
_PERIOD_RANGE_RE = re.compile(
    r"(?:第\s*)?([0-9]{1,2}|[一二三四五六七八九十]{1,2})\s*"
    r"(?:-|~|到|至|—|－)\s*([0-9]{1,2}|[一二三四五六七八九十]{1,2})\s*节"
)
_PERIOD_ONE_RE = re.compile(r"第?\s*([0-9]{1,2}|[一二三四五六七八九十]{1,2})\s*节")
_WEEK_RANGE_RE = re.compile(r"第?\s*([0-9]{1,2})\s*(?:-|~|到|至|—|－)\s*([0-9]{1,2})\s*周")
_WEEK_ONE_RE = re.compile(r"第\s*([0-9]{1,2})\s*周")

_UNDO_RE = re.compile(r"(撤销|撤回|反悔|刚才那条不算|刚才的.*不算)")
_CLEAR_RE = re.compile(r"(清空|全清|都清|全部删|恢复原样)")
_REPLACE_RE = re.compile(r"(改成|换成|改为|替换成)")
_REMOVE_RE = re.compile(r"(删掉|删了|删除|去掉|取消|不上了|不上|停了|别上|不补|没课)")
_ADD_RE = re.compile(r"(加上|加一节|加个|加课|补一节|补个|补上|补课|插入|调课|加)")

_COURSE_AFTER_REPLACE_RE = re.compile(
    r"(?:改成|换成|改为|替换成)\s*([^\s，。！？、；]{1,20})"
)
_COURSE_AFTER_ADD_RE = re.compile(
    r"(?:加上|加一节|加个|加课|补一节|补个|补上|补课|插入|调课|加)\s*([^\s，。！？、；]{1,20})"
)
_LOCATION_RE = re.compile(
    r"(?:在|去|到)\s*([\u4e00-\u9fa5A-Za-z0-9]{1,10}(?:楼|教)\s*[北南东西]?\s*\d{2,4})"
)


def has_keyword(text: str) -> bool:
    return bool(KEYWORD_RE.search(text or ""))


def strip_keyword(text: str) -> str:
    return KEYWORD_RE.sub("", text or "", count=1).strip()


def _num(value: str) -> int | None:
    value = (value or "").strip()
    if value.isdigit():
        return int(value)
    return _CN_NUM.get(value)


def _clean_course(raw: str) -> str:
    s = (raw or "").strip()
    s = re.split(r"(?:周|星期|礼拜)[一二三四五六日天]|第?\d+\s*节|在\S+", s)[0]
    s = re.sub(r"^(?:一门|一节|一个|门)", "", s)
    s = re.sub(r"(?:的)?补课$", "", s)
    s = re.sub(r"的?课$", "", s)
    s = s.strip("，。！？、；的 ")
    return s[:20]


def parse(text: str) -> dict:
    """从原话里抽字段。抽不到的就是 None，交给模型补。"""
    body = strip_keyword(text or "")

    action = None
    if _UNDO_RE.search(body):
        action = "undo"
    elif _CLEAR_RE.search(body):
        action = "clear"
    elif _REPLACE_RE.search(body):
        action = "replace"
    elif _REMOVE_RE.search(body):
        action = "remove"
    elif _ADD_RE.search(body):
        action = "add"

    day = None
    match = _WEEKDAY_RE.search(body)
    if match:
        day = WEEKDAY_CN.get(match.group(1))

    period = end_period = None
    match = _PERIOD_RANGE_RE.search(body)
    if match:
        period = _num(match.group(1))
        end_period = _num(match.group(2))
        if period and end_period and end_period < period:
            period, end_period = end_period, period
    else:
        match = _PERIOD_ONE_RE.search(body)
        if match:
            period = _num(match.group(1))

    weeks = ""
    if "这周" in body or "本周" in body or "这个星期" in body:
        weeks = "this_week"
    elif "下周" in body or "下个星期" in body:
        weeks = "next_week"
    else:
        match = _WEEK_RANGE_RE.search(body)
        if match:
            weeks = f"{match.group(1)}-{match.group(2)}"
        else:
            match = _WEEK_ONE_RE.search(body)
            if match:
                weeks = match.group(1)

    course = ""
    if action in ("add", "replace"):
        match = _COURSE_AFTER_REPLACE_RE.search(body) if action == "replace" else None
        if not match:
            match = _COURSE_AFTER_ADD_RE.search(body)
        if match:
            course = _clean_course(match.group(1))
        # 「改」的地点词不是课名（换到 6 教 201）
        if course and _LOCATION_RE.fullmatch("在" + course):
            course = ""

    location = ""
    match = _LOCATION_RE.search(body)
    if match:
        location = match.group(1).replace(" ", "")

    return {
        "action": action,
        "day": day,
        "period": period,
        "end_period": end_period,
        "course": course,
        "weeks": weeks,
        "location": location,
    }
