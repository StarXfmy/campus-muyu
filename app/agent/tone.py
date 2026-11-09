"""称呼规则：飞书一律「主人」；微信只有绑定的主人才叫主人，其他人叫「你」。

用 contextvar 而不是全局变量：同进程里可能同时处理飞书和微信两条消息，
全局变量会串台（A 的消息用上 B 的称呼）。
"""
from __future__ import annotations

import contextvars

from app.config import WECHAT_OWNER_ID

_channel: contextvars.ContextVar[str] = contextvars.ContextVar("channel", default="feishu")
_user: contextvars.ContextVar[str] = contextvars.ContextVar("user", default="")


def set_context(channel: str, user_id: str = "") -> None:
    _channel.set(channel or "feishu")
    _user.set(user_id or "")


def current_channel() -> str:
    return _channel.get()


def address() -> str:
    """该叫用户什么。"""
    if _channel.get() == "feishu":
        return "主人"
    owner = (WECHAT_OWNER_ID or "").strip()
    if owner and _user.get() == owner:
        return "主人"
    return "你"


def prompt_hint() -> str:
    """注入到 system prompt 里的一句话。"""
    who = address()
    if who == "主人":
        return "称呼对方为「主人」，但不要每句话都加，自然带出即可。"
    return "称呼对方为「你」，不要用「主人」这类称呼。"
