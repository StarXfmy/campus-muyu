"""图书馆座位：分类 → 可选时段 → 实时空位。

三步链路：
  categories 拿 categoryId + contentId
  options    拿默认日期/开始时间/时长（有 default 字段，直接用）
  seats search 返回 availableSeatCount（空位总数）

一个分类要调三次，所以只查前两个分类，避免十几秒等待。
"""
from __future__ import annotations

from typing import Any

from app.tools.hduhelp import run_cli


def list_categories() -> list[dict[str, Any]]:
    data = run_cli("librarybooking", "categories").get("data") or {}
    return data.get("categories") or []


def get_options(category_id: str, content_id: str) -> dict[str, Any]:
    return run_cli(
        "librarybooking", "options",
        "--category-id", category_id, "--content-id", content_id,
    ).get("data") or {}


def search_seats(category_id: str, content_id: str, date: str, begin_time: str, duration: int) -> dict[str, Any]:
    return run_cli(
        "librarybooking", "seats", "search",
        "--category-id", category_id,
        "--content-id", content_id,
        "--date", date,
        "--begin-time", begin_time,
        "--duration-hours", str(duration),
    ).get("data") or {}


def seat_counts(limit: int = 2) -> dict[str, int]:
    """返回 {分类名: 空位数}，给「现在去哪自习」做判断用。"""
    counts: dict[str, int] = {}
    for category in list_categories()[:limit]:
        category_id = str(category.get("categoryId") or "")
        content_id = str(category.get("contentId") or "")
        name = category.get("name") or f"分类{category_id}"
        if not category_id or not content_id:
            continue
        try:
            options = get_options(category_id, content_id)
            date = options.get("defaultDate")
            begin = options.get("defaultBeginTime")
            duration = options.get("defaultDurationHours")
            if not (date and begin and duration):
                continue
            result = search_seats(category_id, content_id, date, begin, int(duration))
            count = result.get("availableSeatCount")
            if count is not None:
                counts[name] = int(count)
        except Exception:
            continue
    return counts


def describe_seats(limit: int = 2) -> str:
    categories = list_categories()
    if not categories:
        return "图书馆座位接口暂时没有返回数据。"

    lines = ["图书馆实时空位"]
    for category in categories[:limit]:
        category_id = str(category.get("categoryId") or "")
        content_id = str(category.get("contentId") or "")
        name = category.get("name") or f"分类{category_id}"
        if not category_id or not content_id:
            continue
        try:
            options = get_options(category_id, content_id)
            date = options.get("defaultDate")
            begin = options.get("defaultBeginTime")
            duration = options.get("defaultDurationHours")
            if not (date and begin and duration):
                lines.append(f"• {name}：当前时段不可预约")
                continue
            result = search_seats(category_id, content_id, date, begin, int(duration))
            count = result.get("availableSeatCount")
            rooms = result.get("rooms") or []
            room_hint = ""
            if rooms:
                room_hint = f"，{len(rooms)} 个区域"
            if count is None:
                lines.append(f"• {name}：没查到空位数据")
            else:
                lines.append(
                    f"• {name}：**{count}** 个空位（{date} {begin} 起 {duration} 小时{room_hint}）"
                )
        except Exception as exc:
            lines.append(f"• {name}：查询失败（{type(exc).__name__}）")

    if len(lines) == 1:
        return "图书馆暂时没有可查询的座位。"
    lines.append("\n想预约的话，去「杭电助手」里选座；我只负责查空位。")
    return "\n".join(lines)
