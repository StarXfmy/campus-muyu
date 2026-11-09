"""知识库自助收录：抓正文 → 模型抽要点 → 逐字校验 → 用户确认 → 入库。

最要紧的一条防线：模型给的 excerpt 必须在抓到的正文里逐字存在。
模型很爱顺手改标点、补全句子、把两段拼一起——那样入库的「原文」就是假的。
校验前先把空白和全半角抹平，免得正常引用被误拒。
"""
from __future__ import annotations

import json
import re
import secrets
import threading
import time
import unicodedata
from datetime import datetime
from pathlib import Path

from app.config import DB_PATH

PENDING_TTL = 600
MIN_EXCERPT = 8
MAX_EXCERPT = 200
MAX_BODY = 6000

_pending: dict[str, dict] = {}
_by_chat: dict[str, str] = {}
_lock = threading.Lock()

_PUNCT_MAP = {
    "“": '"', "”": '"', "‘": "'", "’": "'",
    "，": ",", "。": ".", "：": ":", "；": ";",
    "（": "(", "）": ")", "！": "!", "？": "?",
}


def normalize(text: str) -> str:
    """校验用的归一化：全角转半角、去空白、统一引号标点、转小写。

    只用于比对，不改写要入库的原文。
    """
    value = unicodedata.normalize("NFKC", text or "")
    for src, dst in _PUNCT_MAP.items():
        value = value.replace(src, dst)
    value = re.sub(r"\s+", "", value)
    return value.lower()


# 学校站点抓下来会带一长串导航菜单，逐字存在但没信息量
_NAV_WORDS = (
    "本站首页", "部门概况", "通知公告", "学籍管理", "考试管理", "排课选课",
    "成绩管理", "毕业管理", "实践教学", "综合管理", "工作动态", "版权所有", "Copyright",
)


def _looks_like_nav(text: str) -> bool:
    return sum(1 for word in _NAV_WORDS if word in (text or "")) >= 2


def verify_excerpt(excerpt: str, body: str) -> tuple[bool, str]:
    """excerpt 是否逐字出现在正文里。返回 (通过, 原因)。"""
    if not excerpt:
        return False, "模型没给 excerpt"
    if len(excerpt) < MIN_EXCERPT:
        return False, f"excerpt 太短（{len(excerpt)} 字，至少 {MIN_EXCERPT}）"
    if len(excerpt) > MAX_EXCERPT:
        return False, f"excerpt 太长（{len(excerpt)} 字，最多 {MAX_EXCERPT}）"
    if _looks_like_nav(excerpt):
        return False, "excerpt 选到了导航菜单，要选正文里的句子"
    if normalize(excerpt) in normalize(body):
        return True, "ok"
    return False, "excerpt 在原文里找不到（模型改过字或拼了段）"


def _prompt(body: str, retry_hint: str = "") -> str:
    return (
        "下面是一篇学校通知的正文。提取四样东西，只输出 JSON，不要解释：\n"
        '{"title":"标题","keywords":"关键词1,关键词2,关键词3",'
        '"summary":"一句话摘要，不超过50字","excerpt":"原文片段"}\n'
        "规则：\n"
        "- keywords 给 3-6 个，能用来搜到这篇的关键词\n"
        "- excerpt 必须从正文里**原样复制**一段连续文字，20-80 字\n"
        "- excerpt 一个字都不许改：不要补全、不要换标点、不要拼接两段\n"
        "- excerpt 要选**有实际信息**的句子；不要选导航菜单（本站首页/部门概况/通知公告这种罗列）、"
        "不要选页面标题、不要选页脚版权\n"
        f"{retry_hint}"
        f"\n正文：\n{body[:MAX_BODY]}"
    )


def _call_model(body: str, retry_hint: str = "") -> dict | None:
    """调模型抽 JSON。空返回或解析失败重试一次——免费模型偶尔会吐空。"""
    from app.llm.client import chat

    for _ in range(2):
        raw = chat(_prompt(body, retry_hint), max_tokens=400, purpose="kb_extract") or ""
        match = re.search(r"\{.*\}", raw, re.S)
        if not match:
            continue
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return None


def extract(url: str, db_path: Path | str | None = None) -> dict:
    """抓正文并抽取要点。返回 {ok, ..., reason}；ok=False 时 reason 说明为什么不行。"""
    from app.tools import webpage

    url = (url or "").strip()
    if not url.startswith(("http://", "https://")):
        return {"ok": False, "reason": "这不是一个网页链接"}

    body = webpage.fetch(url, limit=MAX_BODY)
    if body.startswith(("抓取失败", "网页返回", "页面里没抓到", "这不是一个网页")):
        return {"ok": False, "reason": body}

    result = {
        "ok": False,
        "source": url,
        "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "char_count": len(body),
        "body": body,
        "title": "",
        "keywords": "",
        "summary": "",
        "excerpt": "",
        "reason": "",
    }

    data = _call_model(body)
    if not data:
        result["reason"] = "模型没返回可解析的 JSON"
        return result

    excerpt = str(data.get("excerpt") or "").strip()
    passed, why = verify_excerpt(excerpt, body)
    if not passed:
        # 再给一次机会：把失败原因告诉它，要求逐字复制
        hint = f"\n注意：上一次给的片段「{excerpt[:40]}」在正文里找不到，这次必须原样复制。\n"
        data = _call_model(body, hint) or data
        excerpt = str(data.get("excerpt") or "").strip()
        passed, why = verify_excerpt(excerpt, body)

    if not passed:
        result["reason"] = why
        result["rejected_excerpt"] = excerpt
        return result

    result.update(
        {
            "ok": True,
            "title": str(data.get("title") or "").strip()[:80],
            "keywords": str(data.get("keywords") or "").strip()[:200],
            "summary": str(data.get("summary") or "").strip()[:200],
            "excerpt": excerpt,
        }
    )
    return result


# ---------- 预览确认 ----------

def stage(candidate: dict, chat_id: str) -> str:
    """把候选存进待确认区，返回令牌。10 分钟过期。"""
    token = secrets.token_hex(8)
    with _lock:
        now = time.time()
        for key, item in list(_pending.items()):
            if now - item["created"] > PENDING_TTL:
                _pending.pop(key, None)
        _pending[token] = {"candidate": candidate, "created": now, "chat_id": chat_id}
        _by_chat[chat_id] = token
    return token


def take_for_chat(chat_id: str) -> tuple[str, dict] | None:
    """取走「会话 → 令牌」的映射，候选本身留着。

    卡片上的「入库」按钮还要靠这个令牌回查候选；以前连候选一起 pop，
    刚弹出的预览卡点入库就报「过期」。
    """
    with _lock:
        token = _by_chat.pop(chat_id, "")
        item = _pending.get(token) if token else None
    if not item:
        return None
    return token, item["candidate"]


def get(token: str, now: float | None = None) -> dict | None:
    stamp = time.time() if now is None else now
    with _lock:
        item = _pending.get(token)
        if not item:
            return None
        if stamp - item["created"] > PENDING_TTL:
            _pending.pop(token, None)
            return None
        return item["candidate"]


def drop(token: str) -> bool:
    with _lock:
        return _pending.pop(token, None) is not None


# ---------- 入库 ----------

def save(candidate: dict, verified: bool = True, db_path: Path | str | None = None) -> dict:
    """写进 kb_items 并重建 FTS 索引。

    verified=False 表示「模型抽的，还没人点过确认」；
    只有用户点了入库才传 True。
    """
    from app.tools import faq

    title = (candidate.get("title") or "").strip()
    answer = (candidate.get("summary") or "").strip()
    if not title or not answer:
        return {"ok": False, "reason": "标题或摘要为空，不入库"}

    conn = faq.connect(db_path)
    try:
        faq.ensure(conn)
        cursor = conn.execute(
            "INSERT OR IGNORE INTO kb_items"
            " (title, keywords, answer, source, detail, fetched_at, verified_by_user, raw_excerpt, origin)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'user')",
            (
                title,
                candidate.get("keywords") or "",
                answer,
                candidate.get("source") or "",
                candidate.get("source") or "",
                candidate.get("fetched_at") or datetime.now().strftime("%Y-%m-%d %H:%M"),
                1 if verified else 0,
                candidate.get("excerpt") or "",
            ),
        )
        added = cursor.rowcount
        if added:
            # 外部内容表：改了 kb_items 必须重建索引，否则新条目搜不到
            conn.execute("INSERT INTO faq_fts(faq_fts) VALUES('rebuild')")
        conn.commit()
        row = conn.execute(
            "SELECT id FROM kb_items WHERE origin='user' AND title = ?", (title,)
        ).fetchone()
    finally:
        conn.close()

    return {
        "ok": bool(added),
        "added": added,
        "id": row[0] if row else None,
        "reason": "" if added else "这条已经收录过了（标题重复）",
    }
