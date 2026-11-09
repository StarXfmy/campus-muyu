"""长列表分页：状态放内存，10 分钟过期，不落库。

条目本身已经在缓存里，分页只是「这一页显示哪几条」，
没必要为它建表——重启丢了顶多重问一次。
"""
from __future__ import annotations

import secrets
import threading
import time

PAGE_SIZE = 12
TOKEN_TTL = 600

_store: dict[str, dict] = {}
_last: dict[str, str] = {}
_lock = threading.Lock()


def _gc(now: float | None = None) -> None:
    stamp = time.time() if now is None else now
    expired = [key for key, item in _store.items() if stamp - item["created"] > TOKEN_TTL]
    for key in expired:
        _store.pop(key, None)


def register(chat_id: str, blocks: list[str], title: str, now: float | None = None) -> str:
    """登记一份长列表，返回令牌。同一会话只留最新一份，避免按钮点串。"""
    stamp = time.time() if now is None else now
    token = secrets.token_hex(8)
    with _lock:
        _gc(stamp)
        _store[token] = {
            "blocks": list(blocks),
            "title": title,
            "created": stamp,
            "chat_id": chat_id,
        }
        _last[chat_id] = token
    return token


def take_last(chat_id: str) -> str:
    """取走这个会话最近登记的令牌（一次性）。"""
    with _lock:
        return _last.pop(chat_id, "")


def get(token: str, now: float | None = None) -> dict | None:
    """取令牌，过期或不存在返回 None。"""
    stamp = time.time() if now is None else now
    with _lock:
        item = _store.get(token)
        if not item:
            return None
        if stamp - item["created"] > TOKEN_TTL:
            _store.pop(token, None)
            return None
        return item


def page_text(token: str, page: int, now: float | None = None) -> tuple[str, bool, int] | None:
    """返回 (这一页的文本, 还有下一页吗, 总条数)。令牌失效返回 None。"""
    item = get(token, now)
    if item is None:
        return None
    blocks = item["blocks"]
    total = len(blocks)
    start = max(0, int(page)) * PAGE_SIZE
    chunk = blocks[start:start + PAGE_SIZE]
    has_next = start + PAGE_SIZE < total
    text = f"**{item['title']}**\n\n" + "\n".join(chunk)
    if total > PAGE_SIZE:
        text += f"\n\n（第 {start // PAGE_SIZE + 1} 页，共 {total} 条）"
    return text, has_next, total


def reset() -> None:
    with _lock:
        _store.clear()
        _last.clear()
