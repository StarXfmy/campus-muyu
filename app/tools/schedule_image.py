"""把本周课表画成一张 PNG，发到飞书。"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from app.config import DATA_DIR
from app.tools import schedule_store
from app.tools.classroom import SECTION_TIME

WIDTH = 1400
PAD = 24
LEFT_W = 110
TITLE_H = 84
HEADER_H = 52
ROW_H = 74
SECTIONS = 12
WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

_PALETTE = (
    ("#4C7DF0", "#FFFFFF"),
    ("#E88A3C", "#FFFFFF"),
    ("#3FA372", "#FFFFFF"),
    ("#B65FC4", "#FFFFFF"),
    ("#D9534F", "#FFFFFF"),
    ("#2E9AA8", "#FFFFFF"),
    ("#8A6D3B", "#FFFFFF"),
    ("#5C6BC0", "#FFFFFF"),
)


_FONT_PATHS = (
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "C:/Windows/Fonts/simsun.ttc",
    # Linux 服务器（阿里云 Ubuntu 装了 fonts-wqy-zenhei）
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
)


def _font(size: int) -> ImageFont.FreeTypeFont:
    for path in _FONT_PATHS:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def _color(course: str) -> tuple[str, str]:
    return _PALETTE[sum(ord(ch) for ch in course) % len(_PALETTE)]


def _clip(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _hhmm(value: str) -> str:
    """「08:40」「2026-10-27 08:40:00」→「08:40」。"""
    import re

    match = re.search(r"(\d{1,2}):(\d{2})", str(value or ""))
    return f"{int(match.group(1)):02d}:{match.group(2)}" if match else ""


def render_week() -> Path:
    items = schedule_store.this_week()
    week = schedule_store.current_week()

    grid_x = PAD + LEFT_W
    col_w = (WIDTH - PAD * 2 - LEFT_W) / 7
    grid_y = PAD + TITLE_H + HEADER_H
    height = grid_y + ROW_H * SECTIONS + PAD

    img = Image.new("RGB", (WIDTH, height), "#FFFFFF")
    draw = ImageDraw.Draw(img)

    # 标题
    title = f"第 {week} 周课表" if week else "本周课表"
    monday = datetime.now() - timedelta(days=datetime.now().weekday())
    sunday = monday + timedelta(days=6)
    draw.text((PAD, PAD), title, font=_font(36), fill="#1F2329")
    draw.text((PAD + 250, PAD + 10), f"{monday:%m-%d} ~ {sunday:%m-%d}", font=_font(20), fill="#646A73")
    draw.text((WIDTH - PAD - 160, PAD + 12), "杭电校园助手", font=_font(18), fill="#BBBFBF")

    # 表头
    today = datetime.now().weekday() + 1
    for index, label in enumerate(WEEKDAYS):
        x1 = grid_x + index * col_w
        x2 = x1 + col_w
        day_num = index + 1
        fill = "#E8F0FE" if day_num == today else "#F5F6F7"
        draw.rounded_rectangle((x1 + 3, grid_y - HEADER_H + 6, x2 - 3, grid_y - 8), radius=8, fill=fill)
        color = "#3370FF" if day_num == today else "#1F2329"
        draw.text((x1 + col_w / 2 - 26, grid_y - HEADER_H + 14), label, font=_font(22), fill=color)

    # 左侧节次
    for section in range(1, SECTIONS + 1):
        y1 = grid_y + (section - 1) * ROW_H
        start, end = SECTION_TIME.get(section, ("", ""))
        draw.text((PAD + 8, y1 + 8), f"{section}", font=_font(20), fill="#1F2329")
        if start:
            draw.text((PAD + 34, y1 + 10), start, font=_font(15), fill="#8F959E")
        if end:
            draw.text((PAD + 34, y1 + 32), end, font=_font(15), fill="#8F959E")

    # 网格线
    for section in range(SECTIONS + 1):
        y = grid_y + section * ROW_H
        draw.line((grid_x, y, WIDTH - PAD, y), fill="#DEE0E3", width=1)
    for index in range(8):
        x = grid_x + index * col_w
        draw.line((x, grid_y - HEADER_H + 4, x, grid_y + ROW_H * SECTIONS), fill="#DEE0E3", width=1)

    # 课程块
    for item in items:
        day = int(item.get("day") or 0)
        if not (1 <= day <= 7):
            continue
        start = int(item.get("start_section") or 1)
        end = int(item.get("end_section") or start)
        start = max(1, min(start, SECTIONS))
        end = max(start, min(end, SECTIONS))

        x1 = grid_x + (day - 1) * col_w + 5
        x2 = grid_x + day * col_w - 5
        y1 = grid_y + (start - 1) * ROW_H + 4
        y2 = grid_y + end * ROW_H - 4

        bg, fg = _color(item.get("course", ""))
        draw.rounded_rectangle((x1, y1, x2, y2), radius=9, fill=bg)

        text_x = x1 + 10
        name = _clip(item.get("course", ""), 9)
        draw.text((text_x, y1 + 8), name, font=_font(19), fill=fg)

        room = _clip(item.get("location", ""), 9)
        y = y1 + 36
        if room:
            draw.text((text_x, y), room, font=_font(15), fill=fg)
            y += 22
        start_time = _hhmm(item.get("start_time"))
        end_time = _hhmm(item.get("end_time"))
        if start_time and end_time:
            draw.text((text_x, y), f"{start_time}-{end_time}", font=_font(14), fill=fg)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    output = DATA_DIR / "schedule_week.png"
    img.save(output, "PNG")
    return output
