"""早报/查询新闻：固定源 + 校内通知 + 百度热搜兜底。

源都实测过：中新网（国内/国际）、IT之家（科技）、少数派（数码）时间新鲜；
人民网 RSS 停在 2025-06、36氪反爬、环球网/央视 RSS 404，都不要加回来。
只看 24 小时内的内容，早报要的是昨晚到今早的新闻。
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from app.config import DB_PATH

logger = logging.getLogger("campus-bot.news")

FEEDS = (
    ("国内", "中新网·官方", "https://www.chinanews.com.cn/rss/scroll-news.xml", True),
    ("国际", "中新网·官方", "https://www.chinanews.com.cn/rss/world.xml", True),
    ("科技", "IT之家·媒体", "https://www.ithome.com/rss/", False),
    ("科技", "爱范儿·媒体", "https://www.ifanr.com/feed", False),
    ("科技", "雷锋网·媒体", "https://www.leiphone.com/feed", False),
    ("科技", "Solidot·媒体", "https://www.solidot.org/index.rss", False),
    ("数码", "少数派·媒体", "https://sspai.com/feed", False),
)
CATEGORIES = ("国内", "国际", "科技", "数码")
FRESH_HOURS = 24
CACHE_TTL = 1800
CACHE_VERSION = 4  # 多源 + 来源标签，旧缓存自动失效
# 品牌 → 官网：问某个产品时先看官方发布，而不是媒体二手报道
BRANDS = {
    "qoder": "https://qoder.com",
    "qoderwork": "https://qoder.com",
}
OFFICIAL_HOSTS = (
    "gov.cn", "people.com.cn", "xinhuanet.com", "news.cn", "chinanews.com.cn",
    "cctv.com", "cnr.cn", "gmw.cn", "cyol.com", "hdu.edu.cn",
)


def is_official_link(link: str, keyword: str = "") -> bool:
    try:
        host = urllib.parse.urlparse(link).netloc.lower()
    except Exception:
        return False
    if any(host == h or host.endswith("." + h) for h in OFFICIAL_HOSTS):
        return True
    return bool(keyword) and keyword.lower() in host
CACHE_PATH = DB_PATH.parent / "news_cache.json"
_TIMEOUT = 8
CAMPUS_URL = "https://jwc.hdu.edu.cn/13022/list.htm"
CAMPUS_PATTERN = re.compile(
    r'<div class="jzlb[^"]*">\s*'
    r'<div class="btt3"><a href="([^"]+)"[^>]*>([^<]+)</a></div>\s*'
    r'<div class="fbsj4">([\d-]+)</div>'
)


def _parse_ts(raw: str) -> float:
    if not raw:
        return 0
    try:
        dt = parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        try:
            return datetime.strptime(raw[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
        except Exception:
            return 0


def _clean_desc(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = re.sub(r"\s+", " ", text)
    return text.strip()[:240]


def _fetch_feed(category: str, source: str, url: str, official: bool) -> list[dict]:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 campus-muyu/1.0"})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        raw = resp.read(1024 * 1024)
    # 外部 feed 一律不解析 DTD，防实体膨胀/外部实体注入
    if b"<!DOCTYPE" in raw[:2048]:
        raise ValueError("带 DTD 的 feed 不解析")
    root = ET.fromstring(raw)
    out: list[dict] = []
    for node in root.iter():
        if not (node.tag.endswith("item") or node.tag.endswith("entry")):
            continue
        title = link = when = desc = ""
        for child in node:
            tag = child.tag.split("}")[-1]
            if tag == "title" and not title:
                title = (child.text or "").strip()
            elif tag == "link" and not link:
                link = child.get("href") or (child.text or "").strip()
            elif tag in ("pubDate", "published", "updated") and not when:
                when = (child.text or "").strip()
            elif tag in ("description", "summary", "content", "encoded") and not desc:
                desc = _clean_desc(child.text or "")
        if title and link:
            out.append(
                {"t": title, "l": link, "s": source, "c": category, "ts": _parse_ts(when), "d": desc, "o": official}
            )
    return out


def _load_cache() -> list[dict] | None:
    try:
        data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        if int(data.get("v") or 0) != CACHE_VERSION:
            return None
        if time.time() - float(data.get("at") or 0) > CACHE_TTL:
            return None
        items = data.get("items")
        if isinstance(items, list) and items and isinstance(items[0], dict):
            return items
    except Exception:
        pass
    return None


def _all_items() -> list[dict]:
    cached = _load_cache()
    if cached is not None:
        return cached
    items: list[dict] = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(_fetch_feed, c, s, u, o) for c, s, u, o in FEEDS]
        for fut in futures:
            try:
                items += fut.result()
            except Exception as exc:
                logger.warning("新闻源拉取失败：%s", exc)
    if not items:
        return []
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(
            json.dumps({"v": CACHE_VERSION, "at": time.time(), "items": items}, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        pass
    return items


def _likes_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS news_likes ("
        "chat_id TEXT NOT NULL, category TEXT NOT NULL, "
        "count INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (chat_id, category))"
    )
    return conn


def bump_like(chat_id: str, category: str) -> None:
    conn = _likes_conn()
    try:
        conn.execute(
            """INSERT INTO news_likes (chat_id, category, count) VALUES (?, ?, 1)
               ON CONFLICT(chat_id, category) DO UPDATE SET count = count + 1""",
            (chat_id, category),
        )
        conn.commit()
    finally:
        conn.close()


def get_likes(chat_id: str | None) -> dict[str, int]:
    if not chat_id:
        return {}
    conn = _likes_conn()
    try:
        rows = conn.execute("SELECT category, count FROM news_likes WHERE chat_id = ?", (chat_id,)).fetchall()
        return {str(c): int(n) for c, n in rows if c in CATEGORIES}
    finally:
        conn.close()


def prefer_categories(chat_id: str | None) -> list[str]:
    """手动偏好 + 点赞最多的两类，合成推送偏好。"""
    prefer = set(get_prefs(chat_id))
    for cat, _ in sorted(get_likes(chat_id).items(), key=lambda x: -x[1])[:2]:
        prefer.add(cat)
    return [c for c in CATEGORIES if c in prefer]


def collect(
    per_category: int = 2,
    prefer: list[str] | None = None,
    only: list[str] | None = None,
    blocked: list[str] | None = None,
) -> dict[str, list[dict]]:
    """按分类收集 24 小时内的新闻；blocked 里的分类直接不取。"""
    prefer = prefer or []
    blocked = blocked or []
    cutoff = time.time() - FRESH_HOURS * 3600
    fresh = [it for it in _all_items() if it.get("ts", 0) >= cutoff]
    if only:
        cats = [c for c in CATEGORIES if c in only]
    else:
        cats = [c for c in CATEGORIES if c not in blocked]
    out: dict[str, list[dict]] = {}
    for cat in cats:
        picks = sorted((it for it in fresh if it.get("c") == cat), key=lambda x: x.get("ts", 0), reverse=True)
        if not picks:
            continue
        n = per_category + (1 if cat in prefer else 0)
        picked: list[dict] = []
        rest: list[dict] = []
        used: set[str] = set()
        for it in picks:
            src = str(it.get("s") or "")
            if src in used:
                rest.append(it)
                continue
            picked.append(it)
            used.add(src)
            if len(picked) >= n:
                break
        if len(picked) < n:
            picked += rest[: n - len(picked)]
        out[cat] = picked
    return out


def format_grouped(data: dict[str, list[dict]], per_category: int = 2) -> list[str]:
    lines: list[str] = []
    for cat, items in data.items():
        if not items:
            continue
        lines.append(f"【{cat}】")
        for it in items[:per_category]:
            src = str(it.get("s") or "")
            lines.append(f"· {it['t']}（{src}）" if src else f"· {it['t']}")
            if it.get("l"):
                lines.append(f"  {it['l']}")
        lines.append("")
    return lines


HOT_CACHE = DB_PATH.parent / "hot_cache.json"


def baidu_hot(limit: int = 5) -> list[str]:
    now = time.time()
    try:
        cached = json.loads(HOT_CACHE.read_text(encoding="utf-8"))
        if now - float(cached.get("at") or 0) < CACHE_TTL:
            return (cached.get("items") or [])[:limit]
    except Exception:
        pass
    try:
        req = urllib.request.Request(
            "https://top.baidu.com/board?tab=realtime",
            headers={"User-Agent": "Mozilla/5.0 campus-muyu/1.0"},
        )
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            page = resp.read(1024 * 1024).decode("utf-8", "ignore")
        match = re.search(r"<!--s-data:(.*?)-->", page, re.S)
        if not match:
            return []
        data = json.loads(match.group(1))
        cards = (data.get("data") or {}).get("cards") or []
        if not cards:
            return []
        items = [f"· {it['word']}" for it in (cards[0].get("content") or []) if it.get("word")]
        try:
            HOT_CACHE.parent.mkdir(parents=True, exist_ok=True)
            HOT_CACHE.write_text(json.dumps({"at": now, "items": items}, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
        return items[:limit]
    except Exception as exc:
        logger.warning("百度热搜拉取失败：%s", exc)
        return []


def campus_notices(limit: int = 3) -> list[str]:
    """教务处通知公告：直接解析列表页，最贴近本校的事。"""
    try:
        req = urllib.request.Request(CAMPUS_URL, headers={"User-Agent": "Mozilla/5.0 campus-muyu/1.0"})
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            page = resp.read(1024 * 1024).decode("utf-8", "ignore")
    except Exception as exc:
        logger.warning("校内通知拉取失败：%s", exc)
        return []
    out: list[str] = []
    for _href, title, day in CAMPUS_PATTERN.findall(page)[:limit]:
        title = re.sub(r"\s+", " ", title).strip()
        if title:
            out.append(f"· {day[5:]} {title}")
    return out


def _prefs_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS news_prefs ("
        "chat_id TEXT PRIMARY KEY, topics TEXT NOT NULL DEFAULT '', "
        "blocked TEXT NOT NULL DEFAULT '')"
    )
    try:
        conn.execute("ALTER TABLE news_prefs ADD COLUMN blocked TEXT NOT NULL DEFAULT ''")
    except sqlite3.OperationalError:
        pass
    return conn


def get_blocked(chat_id: str | None) -> list[str]:
    if not chat_id:
        return []
    conn = _prefs_conn()
    try:
        row = conn.execute("SELECT blocked FROM news_prefs WHERE chat_id = ?", (chat_id,)).fetchone()
        if not row:
            return []
        return [t for t in str(row[0] or "").split(",") if t in CATEGORIES]
    finally:
        conn.close()


def _set_blocked(chat_id: str, topics: list[str]) -> None:
    conn = _prefs_conn()
    try:
        conn.execute(
            """INSERT INTO news_prefs (chat_id, blocked) VALUES (?, ?)
               ON CONFLICT(chat_id) DO UPDATE SET blocked = ?""",
            (chat_id, ",".join(topics), ",".join(topics)),
        )
        conn.commit()
    finally:
        conn.close()


def block_category(chat_id: str, topics: list[str]) -> None:
    merged = [c for c in CATEGORIES if c in set(get_blocked(chat_id)) | set(topics)]
    _set_blocked(chat_id, merged)


def unblock_category(chat_id: str, topics: list[str]) -> None:
    keep = [c for c in get_blocked(chat_id) if c not in topics]
    _set_blocked(chat_id, keep)


def like_category(chat_id: str, topics: list[str]) -> None:
    for topic in topics:
        bump_like(chat_id, topic)


def get_prefs(chat_id: str | None) -> list[str]:
    if not chat_id:
        return []
    conn = _prefs_conn()
    try:
        row = conn.execute("SELECT topics FROM news_prefs WHERE chat_id = ?", (chat_id,)).fetchone()
        if not row:
            return []
        return [t for t in str(row[0] or "").split(",") if t in CATEGORIES]
    finally:
        conn.close()


def set_prefs(chat_id: str, topics: list[str]) -> None:
    conn = _prefs_conn()
    try:
        conn.execute(
            """INSERT INTO news_prefs (chat_id, topics) VALUES (?, ?)
               ON CONFLICT(chat_id) DO UPDATE SET topics = ?""",
            (chat_id, ",".join(topics), ",".join(topics)),
        )
        conn.commit()
    finally:
        conn.close()


def _fetch_text(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 campus-muyu/1.0"})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        return resp.read(1024 * 1024).decode("utf-8", "ignore")


def official_posts(brand: str, limit: int = 3) -> list[dict]:
    """品牌官网的博客/发布列表：问产品时优先给官方原文。"""
    base = BRANDS.get((brand or "").strip().lower())
    if not base:
        return []
    try:
        page = _fetch_text(base + "/blog")
    except Exception:
        return []
    paths = list(dict.fromkeys(re.findall(r'href="(/blog/[^"]+)"', page)))[:limit]
    out: list[dict] = []
    for path in paths:
        url = base + path
        try:
            article = _fetch_text(url)
            match = re.search(r"<title>([^<]+)", article)
            title = (match.group(1) if match else path.rsplit("/", 1)[-1]).split("|")[0].strip()
        except Exception:
            title = path.rsplit("/", 1)[-1]
        out.append({"t": title, "l": url, "s": "官方发布", "o": True})
    return out


def search_items(keyword: str, limit: int = 8) -> list[dict]:
    """在 24 小时条目的标题和 RSS 摘要里找关键词，零额外请求。"""
    kw = (keyword or "").strip().lower()
    if not kw:
        return []
    cutoff = time.time() - FRESH_HOURS * 3600
    hits = [
        it
        for it in _all_items()
        if it.get("ts", 0) >= cutoff
        and (kw in str(it.get("t") or "").lower() or kw in str(it.get("d") or "").lower())
    ]
    hits.sort(key=lambda x: x.get("ts", 0), reverse=True)
    return hits[:limit]


def morning_lines(chat_id: str | None = None, only: list[str] | None = None) -> list[str]:
    """早报用：热搜固定一段，时事分类各一条、偏好类两条。"""
    prefer = prefer_categories(chat_id)
    lines: list[str] = []
    hot = baidu_hot(5)
    if hot:
        lines += ["【热搜】", *hot, ""]
    data = collect(per_category=1, prefer=prefer, only=only, blocked=get_blocked(chat_id))
    for cat, items in data.items():
        if not items:
            continue
        lines.append(f"【{cat}】")
        for it in items[: 2 if cat in prefer else 1]:
            src = str(it.get("s") or "")
            lines.append(f"· {it['t']}（{src}）" if src else f"· {it['t']}")
        lines.append("")
    return lines


def query_lines(chat_id: str | None = None, only: list[str] | None = None) -> list[str]:
    """对话查询用：热搜一段，时事每类两条、偏好类三条。"""
    prefer = prefer_categories(chat_id)
    lines: list[str] = []
    hot = baidu_hot(8)
    if hot:
        lines += ["【热搜】", *hot, ""]
    data = collect(per_category=2, prefer=prefer, only=only, blocked=get_blocked(chat_id))
    lines += format_grouped(data, per_category=3 if prefer else 2)
    return lines
