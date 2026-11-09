"""把木鱼现有工具桥接成 Qoder Agent SDK 的进程内 MCP 工具。

工具定义不在这里重写：直接读 tools_registry.TOOLS 的 schema，
执行也复用 tools_registry.execute，保证规则路径和 Agent 路径行为一致。

SDK 的成本大头是工具 schema 和工具返回的数据量，所以每次只暴露
和当前问题相关的几个工具（pick_tool_names），不要 22 个全塞进去。

注意：本模块只在真正构造 MCP 工具时才 import qodercn_agent_sdk，
没装 SDK 的环境导入本模块不会报错。
"""
from __future__ import annotations

from typing import Any

from app.agent import tools_registry

_DEFAULT_LIMIT = 5
_MAX_LIMIT = 8

# 每个工具的触发词。命中越多越靠前；一个都不命中时用默认组。
TOOL_KEYWORDS: dict[str, tuple[str, ...]] = {
    "query_schedule": ("课", "上课", "课表", "老师", "教室", "几点上",
                       "明天", "今天", "后天", "这周", "下周", "有空"),
    "edit_schedule": ("加课", "删课", "改课表", "补课", "不上了"),
    "query_empty_rooms": ("空教室", "自习", "空着", "哪有教室", "有座", "没座", "满座", "没位置"),
    "query_balance": ("余额", "饭卡", "校园卡", "一卡通", "还剩多少"),
    "query_electricity": ("电费", "电量", "宿舍电", "充电"),
    "query_weather": ("天气", "气温", "下雨", "带伞", "多少度"),
    "query_activities": ("活动", "讲座", "二课", "志愿", "比赛"),
    "query_my_activities": ("我报名", "我的活动", "我报的"),
    "query_canteen": ("食堂", "吃啥", "吃什么", "好吃", "窗口"),
    "query_sunrun": ("长跑", "跑步", "阳光长跑", "跑了几次"),
    "query_library": ("图书馆", "座位", "自习位"),
    "query_college": ("学院", "办公地点", "教务处"),
    "query_knowledge": ("怎么申请", "怎么办理", "怎么办", "规定", "流程", "政策", "材料", "开放时间"),
    "query_mail": ("邮件", "邮箱", "收件"),
    "query_consumption": ("消费", "花销", "花了多少", "流水"),
    "query_semester_report": ("学期报告", "学期总结", "这学期", "学期过得"),
    "web_search": ("新闻", "最新", "热搜", "搜一下"),
    "web_fetch": ("网址", "链接", "网页", "http"),
    "remember": ("记住", "记一下"),
    "forget": ("忘掉", "删除记忆"),
    "list_memory": ("你记得什么", "我的记忆"),
    "edit_persona": ("修改木鱼",),
}

# 一个词都没命中时给这组，都是最高频的查询。
DEFAULT_TOOL_NAMES: tuple[str, ...] = (
    "query_schedule",
    "query_balance",
    "query_weather",
    "query_empty_rooms",
    "web_search",
    "query_knowledge",
)

# 写操作：只有问题里明确出现对应动词才放出来，避免模型顺手改数据。
_WRITE_TOOLS = frozenset({"edit_schedule", "remember", "forget", "edit_persona"})


def pick_tool_names(text: str, limit: int = _DEFAULT_LIMIT) -> list[str]:
    """按问题挑相关工具。

    单个问题通常 1-2 个；一句话问好几件事时按实际命中数放宽，
    最多 _MAX_LIMIT 个，避免问到第 6 件事时工具被截掉。
    """
    raw = text or ""
    scored: list[tuple[int, str]] = []
    for name, keywords in TOOL_KEYWORDS.items():
        hits = sum(1 for kw in keywords if kw in raw)
        if hits:
            scored.append((hits, name))
    scored.sort(key=lambda item: (-item[0], item[1]))

    read_hits = [name for _, name in scored if name not in _WRITE_TOOLS]
    effective = max(limit, min(len(read_hits), _MAX_LIMIT))
    picked: list[str] = read_hits[:effective]

    if not picked:
        picked = list(DEFAULT_TOOL_NAMES[:limit])

    # 写操作只在明确提到时追加，不占常规名额
    for _, name in scored:
        if name in _WRITE_TOOLS and name not in picked:
            picked.append(name)

    return picked


def _make_handler(tool_name: str, chat_id: str | None, sink: list[str] | None = None):
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        try:
            text = tools_registry.execute(tool_name, args or {}, "", chat_id)
        except Exception as exc:
            text = f"工具执行失败：{type(exc).__name__}: {exc}"
        if sink is not None:
            sink.append(f"[{tool_name}] {text}")
        return {"content": [{"type": "text", "text": str(text)[:2000]}]}

    return handler


def build_mcp_tools(
    chat_id: str | None = None,
    tool_names: list[str] | None = None,
    sink: list[str] | None = None,
) -> list[Any]:
    """构造 MCP 工具。tool_names 为空时用全部 22 个。sink 收集工具返回，给护栏用。"""
    from qodercn_agent_sdk import tool

    names = set(tool_names) if tool_names else None
    return [
        tool(
            entry["function"]["name"],
            entry["function"]["description"],
            entry["function"]["parameters"],
        )(_make_handler(entry["function"]["name"], chat_id, sink))
        for entry in tools_registry.TOOLS
        if names is None or entry["function"]["name"] in names
    ]


def build_server(
    chat_id: str | None = None,
    tool_names: list[str] | None = None,
    sink: list[str] | None = None,
) -> Any:
    """构造进程内 MCP server，传给 QoderAgentOptions(mcp_servers={"muyu": ...})。"""
    from qodercn_agent_sdk import create_sdk_mcp_server

    return create_sdk_mcp_server(
        "muyu", tools=build_mcp_tools(chat_id, tool_names, sink)
    )


def allowed_tool_names(tool_names: list[str] | None = None) -> list[str]:
    """MCP 工具在 SDK 里的全名，用于 allowed_tools 放行。"""
    names = tool_names or [entry["function"]["name"] for entry in tools_registry.TOOLS]
    return [f"mcp__muyu__{name}" for name in names]
