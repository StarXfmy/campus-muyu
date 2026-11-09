"""LLM 意图兜底：规则匹配失败时，让 GLM-4-Flash 判断用户想干什么。

只在 router 规则没命中时调用，正常问答不会增加延迟。
没有配置 LLM_API_KEY 时静默跳过，不影响主流程。
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from typing import Any

from app.config import (
    CHAT_API_KEY,
    CHAT_BASE_URL,
    CHAT_MODEL,
    LLM_API_KEY,
    LLM_BASE_URL,
    LLM_MODEL,
    LLM_TIMEOUT,
    load_persona,
)

VALID_INTENTS = {
    "schedule", "schedule_week", "schedule_weekday", "schedule_all",
    "balance", "empty_room", "activities", "knowledge", "office", "grade", "help",
}

SYSTEM_PROMPT = """你是杭电校园助手的意图路由器。判断用户想做什么，只输出 JSON，不要解释、不要 markdown。

可选 intent：
- schedule：查今天或明天的课。**用户说「明天」「今日」「明日」时用这个，不要用 schedule_weekday**
- schedule_week：查本周课表（周一到周五全部）
- schedule_weekday：只查本周某一天，且用户**明确说出了星期几**（周三/星期五）。没说星期几不要用这个
- schedule_all：查整学期有哪些课
- balance：查校园卡/饭卡余额
- empty_room：查空教室、找自习的地方（可填 building 楼名、floor 楼层数字）
- activities：查二课活动、讲座、比赛、志愿服务
- knowledge：查学校规定、政策、办事流程、奖学金、图书馆开放时间
- office：查办公地点、部门地址、联系电话
- grade：查成绩、绩点、分数
- help：打招呼、问你能干什么
- unknown：以上都不是

已知楼名：正心楼、创智楼、青越楼、子衿楼、博约楼、礼贤楼、精诚楼、芯越楼

如果用户这轮说得很短（如「明天」「周三」「查一下」），结合给出的上下文推断他延续的是哪个意图。

只输出这一种格式：
{"intent":"...","weekday":null,"building":null,"floor":null}"""


def _log_usage(model: str, purpose: str, usage: dict | None, started: float, ok: bool) -> None:
    """记一次调用。失败或没返回用量都记，只是 token 记 0——失败次数也要能看见。"""
    try:
        from app.ops import usage as usage_store

        info = usage or {}
        usage_store.record(
            model=model,
            purpose=purpose,
            prompt_tokens=int(info.get("prompt_tokens") or 0),
            completion_tokens=int(info.get("completion_tokens") or 0),
            latency_ms=int((time.time() - started) * 1000),
            ok=ok,
        )
    except Exception:
        pass


def _format_context(history: list[dict] | None) -> str:
    if not history:
        return ""
    lines = []
    for item in history[-8:]:
        role = "用户" if item.get("role") == "user" else "助手"
        lines.append(f"{role}：{item.get('text', '')[:80]}")
    return "最近对话：\n" + "\n".join(lines) + "\n\n"


def _extract_json(text: str) -> dict[str, Any] | None:
    """模型可能把 JSON 包在代码块里，做一次宽松提取。"""
    if not text:
        return None
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def chat_with_tools(
    messages: list[dict],
    tools: list[dict],
    timeout: int | None = None,
    tool_choice: str = "auto",
    purpose: str = "tools",
) -> dict[str, Any] | None:
    """带工具调用的对话。返回 assistant message（可能含 tool_calls），失败返回 None。

    tool_choice="required" 会强制模型第一轮必须调工具——实测 auto 模式下它
    遇到「哪周课多」这类问题会凭记忆直接回答，不查数据。
    """
    if not LLM_API_KEY:
        return None
    started = time.time()

    payload = {
        "model": LLM_MODEL,
        "messages": messages,
        "tools": tools,
        "tool_choice": tool_choice,
        "temperature": 0.2,
    }
    request = urllib.request.Request(
        f"{LLM_BASE_URL}/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {LLM_API_KEY}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout or LLM_TIMEOUT) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        _log_usage(LLM_MODEL, purpose, None, started, False)
        return None

    _log_usage(LLM_MODEL, purpose, data.get("usage"), started, True)
    choices = data.get("choices") or []
    if not choices:
        return None
    return choices[0].get("message") or {}


def chat_conversation(
    messages: list[dict],
    tools: list[dict] | None = None,
    tool_choice: str = "auto",
    timeout: int | None = None,
    max_tokens: int = 900,
    purpose: str = "chat",
) -> dict[str, Any] | None:
    """闲聊/自由对话：用独立的聊天模型（可配 DeepSeek），支持工具。"""
    if not CHAT_API_KEY or not messages:
        return None
    started = time.time()

    payload: dict[str, Any] = {
        "model": CHAT_MODEL,
        "messages": messages,
        "temperature": 0.7,
        "max_tokens": max_tokens,
    }
    if "deepseek" in (CHAT_BASE_URL or ""):
        # DeepSeek 默认开思考链，闲聊场景太慢，显式关掉（实测 1.1s vs 超时）
        payload["thinking"] = {"type": "disabled"}
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = tool_choice
    request = urllib.request.Request(
        f"{CHAT_BASE_URL}/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {CHAT_API_KEY}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout or LLM_TIMEOUT) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        _log_usage(CHAT_MODEL, purpose, None, started, False)
        return None

    _log_usage(CHAT_MODEL, purpose, data.get("usage"), started, True)
    choices = data.get("choices") or []
    if not choices:
        return None
    return choices[0].get("message") or {}


def web_search(query: str, count: int = 5) -> list[dict[str, Any]]:
    """调智谱搜索接口，给聊天模型当联网工具用。"""
    if not LLM_API_KEY or not query:
        return []
    payload = {"search_query": query, "search_engine": "search_std", "count": count}
    request = urllib.request.Request(
        f"{LLM_BASE_URL}/web_search",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {LLM_API_KEY}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return []
    return data.get("search_result") or []


def chat(
    prompt: str,
    max_tokens: int = 160,
    purpose: str = "phrase",
    timeout: float | None = None,
) -> str | None:
    """通用对话调用，用于让模型解读校园数据。失败返回 None。"""
    if not LLM_API_KEY or not prompt:
        return None
    started = time.time()

    from app.agent import tone

    persona = load_persona()
    system = f"{persona}\n\n{tone.prompt_hint()}" if persona else tone.prompt_hint()
    messages = [{"role": "system", "content": system}]
    messages.append({"role": "user", "content": prompt})

    payload = {
        "model": LLM_MODEL,
        "messages": messages,
        "temperature": 0.3,
        "max_tokens": max_tokens,
    }
    request = urllib.request.Request(
        f"{LLM_BASE_URL}/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {LLM_API_KEY}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout or LLM_TIMEOUT) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        _log_usage(LLM_MODEL, purpose, None, started, False)
        return None

    _log_usage(LLM_MODEL, purpose, data.get("usage"), started, True)
    choices = data.get("choices") or []
    if not choices:
        return None
    content = (choices[0].get("message") or {}).get("content") or ""
    return content.strip() or None


def chat_fast(
    prompt: str,
    max_tokens: int = 220,
    timeout: int = 8,
    purpose: str = "faq",
) -> str | None:
    """单轮短任务：走聊天模型（更会说话、更快），失败返回 None。"""
    if not CHAT_API_KEY or not prompt:
        return None

    from app.agent import tone

    persona = load_persona()
    system = f"{persona}\n\n{tone.prompt_hint()}" if persona else tone.prompt_hint()
    message = chat_conversation(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        timeout=timeout,
        max_tokens=max_tokens,
        purpose=purpose,
    )
    if not message:
        return None
    return (message.get("content") or "").strip() or None


def classify_intent(text: str, history: list[dict] | None = None) -> dict[str, Any] | None:
    """调用 LLM 判断意图。失败一律返回 None，由调用方降级处理。"""
    if not LLM_API_KEY or not text:
        return None

    user_content = _format_context(history) + f"本轮用户说：{text}"
    payload = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.0,
        "max_tokens": 120,
    }
    request = urllib.request.Request(
        f"{LLM_BASE_URL}/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {LLM_API_KEY}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=LLM_TIMEOUT) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None

    choices = data.get("choices") or []
    if not choices:
        return None
    content = (choices[0].get("message") or {}).get("content") or ""
    parsed = _extract_json(content)
    if not parsed:
        return None

    intent = str(parsed.get("intent") or "").strip()
    if intent not in VALID_INTENTS:
        return None

    weekday = parsed.get("weekday")
    floor = parsed.get("floor")
    building = parsed.get("building")
    return {
        "intent": intent,
        "weekday": int(weekday) if isinstance(weekday, (int, float)) and 1 <= int(weekday) <= 7 else None,
        "floor": int(floor) if isinstance(floor, (int, float)) and 1 <= int(floor) <= 20 else None,
        "building": str(building).strip() if building else None,
    }
