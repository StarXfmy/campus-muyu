"""绍兴校区食堂：店铺、菜单、价格、同学点评。

数据来自校园食堂点评站（同学自建）。它一次给全量，但带菜单图片 base64，
一次 30MB 左右——所以这里剥掉图片只留文字，缓存 6 小时；
用户说「刷新」再强制拉。网断了就用旧缓存，不让对话卡死。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
import urllib.request
from datetime import datetime

from app.config import DATA_DIR, DB_PATH

logger = logging.getLogger("campus-bot.canteen")

API = "http://101.37.129.112:3000/api/shops"
CACHE_PATH = DATA_DIR / "canteen_cache.json"
CACHE_TTL = 3600  # 服务器流量不限，缓存压到 1 小时，菜单和点评更接近实时
_TIMEOUT = 25


def _slim(shop: dict) -> dict:
    menu = []
    for item in shop.get("menu") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("n") or "").strip()
        if name:
            menu.append({"n": name, "p": item.get("p")})

    reviews = []
    for item in (shop.get("reviews") or [])[:5]:
        if not isinstance(item, dict):
            continue
        content = str(item.get("content") or "").strip()[:60]
        if content:
            reviews.append({"rating": item.get("rating"), "content": content})

    return {
        "name": str(shop.get("name") or "").strip(),
        "floor": str(shop.get("floor") or "").strip(),
        "price": str(shop.get("price") or "").strip(),
        "category": str(shop.get("category") or "").strip(),
        "taste": str(shop.get("taste") or "").strip(),
        "menu": menu,
        "reviews": reviews,
    }


def _read_cache(ignore_ttl: bool = False) -> list[dict] | None:
    try:
        payload = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not ignore_ttl and time.time() - float(payload.get("at") or 0) > CACHE_TTL:
        return None
    shops = payload.get("shops")
    return shops if isinstance(shops, list) and shops else None


def _write_cache(shops: list[dict]) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(
        json.dumps({"at": time.time(), "shops": shops}, ensure_ascii=False),
        encoding="utf-8",
    )


def fetch(force: bool = False) -> list[dict]:
    """拿全部店铺。没缓存或过期才联网；拉取失败时退回旧缓存。"""
    if not force:
        cached = _read_cache()
        if cached:
            return cached

    request = urllib.request.Request(API, headers={"User-Agent": "campus-assistant/1.0"})
    try:
        started = time.time()
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
            raw = json.loads(response.read().decode("utf-8"))
        shops = [_slim(item) for item in raw if isinstance(item, dict)]
        shops = [shop for shop in shops if shop["name"]]
        _write_cache(shops)
        logger.info("食堂数据已更新：%d 家店，耗时 %.1f 秒", len(shops), time.time() - started)
        return shops
    except Exception as exc:
        cached = _read_cache(ignore_ttl=True)
        if cached:
            logger.warning("食堂数据拉取失败（%s），用旧缓存", type(exc).__name__)
            return cached
        raise


CANTEEN_SCHEMA = """
CREATE TABLE IF NOT EXISTS canteen_shops (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    floor TEXT NOT NULL DEFAULT '',
    price_level TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '',
    taste TEXT NOT NULL DEFAULT '',
    rating REAL,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS canteen_dishes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    shop TEXT NOT NULL,
    dish TEXT NOT NULL,
    price REAL,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    UNIQUE(shop, dish)
);
"""


def _db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.executescript(CANTEEN_SCHEMA)
    return conn


def sync() -> int:
    """拉一次全量并落库：新店/新菜记一行，老条目只更新价格和 last_seen。"""
    shops = fetch(force=True)
    if not shops:
        return 0
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    conn = _db()
    added = 0
    try:
        for shop in shops:
            name = shop.get("name") or ""
            if not name:
                continue
            ratings = [
                review["rating"]
                for review in shop.get("reviews") or []
                if isinstance(review.get("rating"), (int, float))
            ]
            rating = round(sum(ratings) / len(ratings), 1) if ratings else None
            row = conn.execute("SELECT id FROM canteen_shops WHERE name = ?", (name,)).fetchone()
            if row:
                conn.execute(
                    "UPDATE canteen_shops SET floor=?, price_level=?, category=?, taste=?, "
                    "rating=?, last_seen=? WHERE name=?",
                    (shop.get("floor") or "", shop.get("price") or "", shop.get("category") or "",
                     shop.get("taste") or "", rating, now, name),
                )
            else:
                conn.execute(
                    "INSERT INTO canteen_shops (name, floor, price_level, category, taste, "
                    "rating, first_seen, last_seen) VALUES (?,?,?,?,?,?,?,?)",
                    (name, shop.get("floor") or "", shop.get("price") or "",
                     shop.get("category") or "", shop.get("taste") or "", rating, now, now),
                )
                added += 1

            for dish in shop.get("menu") or []:
                dish_name = (dish.get("n") or "").strip()
                if not dish_name:
                    continue
                raw_price = dish.get("p")
                price = float(raw_price) if isinstance(raw_price, (int, float)) else None
                found = conn.execute(
                    "SELECT id FROM canteen_dishes WHERE shop = ? AND dish = ?", (name, dish_name)
                ).fetchone()
                if found:
                    conn.execute(
                        "UPDATE canteen_dishes SET price=?, last_seen=? WHERE id=?",
                        (price, now, found[0]),
                    )
                else:
                    conn.execute(
                        "INSERT INTO canteen_dishes (shop, dish, price, first_seen, last_seen) "
                        "VALUES (?,?,?,?,?)",
                        (name, dish_name, price, now, now),
                    )
                    added += 1
        conn.commit()
    finally:
        conn.close()
    return added


_PRICE_HINTS = (
    ("便宜", "低价"), ("省钱", "低价"), ("实惠", "低价"), ("低价", "低价"),
    ("贵", "高价"), ("高端", "高价"),
)
_TASTE_HINTS = ("辣", "甜", "咸")
_DISH_HINTS = ("面", "饭", "粉", "肉", "鸡", "鱼", "锅", "粥", "饼", "汤", "轻食", "麻辣", "烤肉", "饺", "包")
_FLOOR_HINTS = ("一楼", "二楼", "三楼", "四楼")


def recommend(text: str = "", limit: int = 4) -> list[dict]:
    """按一句话挑几家店，每家带几个菜和一条点评。"""
    force = any(k in (text or "") for k in ("刷新", "最新", "更新"))
    shops = fetch(force=force)

    keyword = next((word for word in _DISH_HINTS if word in (text or "")), "")
    wanted_price = next((level for word, level in _PRICE_HINTS if word in (text or "")), "")
    wanted_taste = next((word for word in _TASTE_HINTS if word in (text or "")), "")
    # 「食堂二楼」问的是楼层，不加这个会推荐一楼的店
    wanted_floor = next((word for word in _FLOOR_HINTS if word in (text or "")), "")
    # 点评站给「外卖」这个位置，但数据里标的是「校外」
    if "外卖" in (text or "") or "校外" in (text or ""):
        wanted_floor = "校外"

    scored: list[tuple[float, dict]] = []
    for shop in shops:
        score = 0.0
        dish_hit = bool(keyword) and any(keyword in dish["n"] for dish in shop["menu"])
        if keyword and (keyword in shop["name"] or dish_hit):
            score += 8
        elif keyword:
            continue
        if wanted_price:
            if shop["price"] == wanted_price:
                score += 4
            elif wanted_price == "低价":
                continue
        if wanted_taste:
            if wanted_taste in shop["taste"]:
                score += 3
            else:
                continue
        if wanted_floor and wanted_floor not in shop["floor"]:
            continue
        ratings = [r["rating"] for r in shop["reviews"] if isinstance(r.get("rating"), (int, float))]
        if ratings:
            score += sum(ratings) / len(ratings)
        if shop["menu"]:
            score += 1
        if score > 0 or not (keyword or wanted_price or wanted_taste):
            scored.append((score, shop))

    scored.sort(key=lambda item: item[0], reverse=True)

    picks = []
    for _, shop in scored[:limit]:
        dishes = shop["menu"][:3]
        if keyword:
            matched = [dish for dish in shop["menu"] if keyword in dish["n"]][:3]
            dishes = matched or dishes
        review = ""
        for item in shop["reviews"]:
            if item["content"]:
                rating = item.get("rating")
                review = f"{item['content']}（{rating} 星）" if isinstance(rating, (int, float)) else item["content"]
                break
        picks.append({"shop": shop, "picks": dishes, "review": review})
    return picks
