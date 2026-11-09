"""用 Qoder Agent SDK 跑兜底 agent：多轮工具编排，外面套木鱼的三道护栏。

只接管「规则没命中、现有 agent 也不够用」的那条路，默认关闭。
任何失败（没装 SDK、没登录、超时、没调工具）都返回 None，由调用方回退现有 agent。
"""
from __future__ import annotations

import asyncio
import logging
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from app import config
from app.agent import agent as legacy_agent
from app.agent import sdk_tools, tools_registry

logger = logging.getLogger(__name__)

TIMEOUT_S = 60
MAX_TURNS = 5

# 必须用空目录：CLI 启动会扫描 cwd，文件一多就慢到超时
_SDK_CWD = Path(tempfile.gettempdir()) / "muyu-sdk-cwd"
_SDK_CWD.mkdir(parents=True, exist_ok=True)


def _auth():
    from qodercn_agent_sdk import access_token_from_env, qodercli_auth

    if config.SDK_AGENT_PAT:
        return access_token_from_env()
    return qodercli_auth()


async def _ask(
    text: str,
    chat_id: str | None,
    server: Any,
    allowed: list[str],
) -> tuple[str, list[str]]:
    from qodercn_agent_sdk import QoderAgentOptions, query

    # SDK 原来只吃人设文件，没有工具使用规则；补上完整提示词，
    # 「吃完去哪」这类才会去查课表，而不是列一张放之四海皆准的地点清单
    from app.agent.agent import _system_prompt as build_system_prompt

    opts = QoderAgentOptions(
        auth=_auth(),
        model=config.SDK_AGENT_MODEL,
        system_prompt=build_system_prompt(text, chat_id),
        mcp_servers={"muyu": server},
        allowed_tools=allowed,
        max_turns=MAX_TURNS,
        permission_mode="dont_ask",
        cwd=str(_SDK_CWD),
        env={"QODER_SKIP_VERSION_CHECK": "1"},
    )

    reply = ""
    used: list[str] = []
    # 系统提示里已经带了最近聊过的上下文，这里不再重复拼
    async for msg in query(prompt=text, options=opts):
        name = type(msg).__name__
        if name != "AssistantMessage":
            continue
        for block in getattr(msg, "content", []) or []:
            if type(block).__name__ == "ToolUseBlock":
                raw = getattr(block, "name", "")
                used.append(raw.replace("mcp__muyu__", ""))
            piece = getattr(block, "text", "")
            if piece:
                reply += piece
    return reply, used


async def _ask_with_timeout(
    text: str,
    chat_id: str | None,
    server: Any,
    allowed: list[str],
) -> tuple[str, list[str]]:
    return await asyncio.wait_for(_ask(text, chat_id, server, allowed), TIMEOUT_S)


def _run_in_thread(coro) -> tuple[str, list[str]]:
    """在独立线程里跑协程。

    飞书长连接本身跑在 asyncio 事件循环里，直接 asyncio.run 会报
    “cannot be called from a running event loop”，所以另起线程建自己的循环。
    """
    box: dict[str, Any] = {}

    def worker() -> None:
        try:
            box["value"] = asyncio.run(coro)
        except Exception as exc:  # noqa: BLE001
            box["error"] = exc

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join(TIMEOUT_S + 5)
    if thread.is_alive():
        raise TimeoutError(f"SDK agent 超时 {TIMEOUT_S}s")
    if "error" in box:
        raise box["error"]
    return box.get("value", ("", []))


def run(text: str, chat_id: str | None = None) -> str | None:
    """跑一轮 SDK agent。返回 None 表示让调用方降级到现有 agent。"""
    if not config.SDK_AGENT_ENABLED or not text:
        return None

    tool_names = sdk_tools.pick_tool_names(text)
    logger.info("SDK agent 尝试：%s | 工具=%s", text[:40], tool_names)
    sink: list[str] = []
    t0 = time.monotonic()
    try:
        server = sdk_tools.build_server(chat_id, tool_names, sink)
        allowed = sdk_tools.allowed_tool_names(tool_names)
        reply, used = _run_in_thread(
            _ask_with_timeout(text, chat_id, server, allowed)
        )
    except Exception:
        logger.exception("SDK agent 失败，回退现有 agent")
        return None

    reply = (reply or "").strip()
    if not reply or not used:
        logger.info(
            "SDK agent 放弃：%.1fs 回复=%d字 工具=%s",
            time.monotonic() - t0, len(reply), used,
        )
        return None
    logger.info(
        "SDK agent 完成：%.1fs 工具=%s 回复=%d字",
        time.monotonic() - t0, used, len(reply),
    )

    # 护栏一：工具拒绝执行时，别让模型把拒绝粉饰成成功
    for item in sink:
        if "要先说「修改木鱼：」" in item:
            return item.split("] ", 1)[-1]

    # 护栏二：回复里的数字/事实必须能在工具返回里找到证据
    try:
        reply = legacy_agent._strip_hallucination(reply, sink)
    except Exception:
        logger.exception("幻觉过滤失败，保留原回复")

    # 护栏三：模型漏调的工具由代码补上
    try:
        missing = legacy_agent._missing_tools(text, used)
        extra: list[str] = []
        for name in missing:
            extra.append(tools_registry.execute(name, {}, text, chat_id))
        if extra:
            reply = reply + "\n\n" + "\n".join(x for x in extra if x)
    except Exception:
        logger.exception("补调工具失败")

    # 联网搜过就在回复末尾标出来，跟普通 agent 的「已查询」保持一致
    if "web_search" in used:
        reply = reply.rstrip() + "\n\n（已查询：联网搜索）"

    return reply or None
