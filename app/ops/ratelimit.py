"""按 chat_id 的滑动窗口限流。

纯内存、不落库：重启后额度清零可以接受，反正目的是挡住刷屏和烧模型。
只拦「超频」，不拦正常连问——20 条/分钟对一个人用足够了。
"""
from __future__ import annotations

import threading
import time

LIMIT = 20
WINDOW = 60.0

_hits: dict[str, list[float]] = {}
_lock = threading.Lock()

REPLY = "问得太快啦，本鱼缓一缓。一分钟后再问，或者把问题攒成一条发。"


def check(chat_id: str, now: float | None = None, limit: int = LIMIT, window: float = WINDOW) -> bool:
    """放行返回 True；超限返回 False。"""
    if not chat_id:
        return True
    stamp = time.time() if now is None else now
    with _lock:
        hits = _hits.setdefault(chat_id, [])
        cutoff = stamp - window
        hits[:] = [item for item in hits if item > cutoff]
        if len(hits) >= limit:
            return False
        hits.append(stamp)
        return True


def reset(chat_id: str | None = None) -> None:
    with _lock:
        if chat_id is None:
            _hits.clear()
        else:
            _hits.pop(chat_id, None)


def status(chat_id: str, now: float | None = None, window: float = WINDOW) -> dict:
    stamp = time.time() if now is None else now
    with _lock:
        hits = [item for item in _hits.get(chat_id, []) if item > stamp - window]
    return {"used": len(hits), "limit": LIMIT, "window": window}
