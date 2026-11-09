"""消费条形图：白底暖色，横向条形，给飞书当图片发。"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from app.config import DATA_DIR

WIDTH = 1000
PAD = 28
ROW_H = 46
BAR_MAX = 560
TITLE_H = 78

# 暖色系，从深到浅
_WARM = ("#C25E3A", "#D97B34", "#E39A48", "#EBB65E", "#F0CB85", "#F3DCAC", "#F6EBD0")


def _font(size: int) -> ImageFont.FreeTypeFont:
    for name in ("msyh.ttc", "msyhbd.ttc", "simhei.ttf", "simsun.ttc"):
        path = Path("C:/Windows/Fonts") / name
        if path.exists():
            return ImageFont.truetype(str(path), size)
    return ImageFont.load_default()


def render_expense_chart(days: list[tuple[str, float]], title: str = "最近消费") -> Path | None:
    """days: [("10-26", 20.7), ...]，按输入的日期顺序画。"""
    if not days:
        return None
    days = days[:31]
    height = PAD + TITLE_H + ROW_H * len(days) + PAD
    img = Image.new("RGB", (WIDTH, height), "#FFFDF8")
    draw = ImageDraw.Draw(img)

    draw.text((PAD, PAD), title, font=_font(34), fill="#3D2C21")
    total = sum(value for _, value in days)
    draw.text((PAD + 300, PAD + 10), f"合计 {total:.2f} 元", font=_font(20), fill="#8A7566")

    biggest = max(value for _, value in days) or 1
    top = PAD + TITLE_H
    for index, (label, value) in enumerate(days):
        y = top + index * ROW_H
        draw.text((PAD, y + 10), str(label), font=_font(19), fill="#5C4433")

        bar_w = max(6, int(BAR_MAX * value / biggest))
        color = _WARM[min(index, len(_WARM) - 1)]
        draw.rounded_rectangle(
            (PAD + 92, y + 8, PAD + 92 + bar_w, y + 34),
            radius=8,
            fill=color,
        )
        draw.text((PAD + 108 + bar_w, y + 11), f"{value:.2f} 元", font=_font(18), fill="#5C4433")

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    output = DATA_DIR / "expense_chart.png"
    img.save(output, "PNG")
    return output


def render_expense_from_log(
    chat_id: str,
    month: bool = False,
    start: str | None = None,
    end: str | None = None,
) -> Path | None:
    """从 expense_log 聚合：start+end 指定范围，month 取当月，否则最近 7 天。没数据返回 None。"""
    from app.tools import records

    conn = records.connect()
    records._ensure(conn)
    try:
        if start and end:
            rows = conn.execute(
                "SELECT day, SUM(amount) AS total FROM expense_log WHERE chat_id = ? "
                "AND day >= ? AND day <= ? GROUP BY day ORDER BY day",
                (chat_id, start, end),
            ).fetchall()
        elif month:
            first = datetime.now().strftime("%Y-%m-01")
            rows = conn.execute(
                "SELECT day, SUM(amount) AS total FROM expense_log WHERE chat_id = ? "
                "AND day >= ? GROUP BY day ORDER BY day",
                (chat_id, first),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT day, SUM(amount) AS total FROM expense_log WHERE chat_id = ? "
                "AND day != '' GROUP BY day ORDER BY day DESC LIMIT 7",
                (chat_id,),
            ).fetchall()
    finally:
        conn.close()
    if not rows:
        return None
    if start and end:
        days = [(str(r["day"])[5:], float(r["total"] or 0)) for r in rows]
        return render_expense_chart(days, f"{start[5:]} 至 {end[5:]} 消费")
    if month:
        days = [(str(r["day"])[5:], float(r["total"] or 0)) for r in rows]
        return render_expense_chart(days, f"{datetime.now().month} 月消费")
    days = [(str(r["day"])[5:], float(r["total"] or 0)) for r in reversed(rows)]
    return render_expense_chart(days, "最近 7 天消费")
