"""模型用量记账与周报。

单价按「元 / 百万 token」算，单位写清楚，免得看到 0.02 以为算错了。
智谱 GLM-4-Flash 是免费额度，DeepSeek V4.1 Flash 按 token 计费。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from app.config import DB_PATH

# 表名不能叫 usage：那个名字已经被空教室缓存占了（location_id/week/weekday/section）
SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day TEXT NOT NULL,
    model TEXT NOT NULL DEFAULT '',
    purpose TEXT NOT NULL DEFAULT '',
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    latency_ms INTEGER NOT NULL DEFAULT 0,
    ok INTEGER NOT NULL DEFAULT 1
)
"""

# 元 / 百万 token（输入价, 输出价）。涨价了改这里。
PRICING: dict[str, tuple[float, float]] = {
    "glm-4-flash": (0.0, 0.0),      # 免费额度，不限量但限速
    "deepseek-flash": (1.0, 4.0),   # 官网价：输入 ¥1/M、输出 ¥4/M
}
DEFAULT_PRICE = (0.0, 0.0)


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    target = Path(db_path or DB_PATH)
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target, timeout=10)
    conn.executescript(SCHEMA)
    return conn


def record(
    model: str,
    purpose: str = "",
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    latency_ms: int = 0,
    ok: bool = True,
    day: str = "",
    db_path: Path | str | None = None,
) -> bool:
    try:
        conn = connect(db_path)
        try:
            conn.execute(
                "INSERT INTO llm_usage (day, model, purpose, prompt_tokens, completion_tokens, latency_ms, ok)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    day or datetime.now().strftime("%Y-%m-%d"),
                    model or "",
                    purpose or "",
                    int(prompt_tokens or 0),
                    int(completion_tokens or 0),
                    int(latency_ms or 0),
                    1 if ok else 0,
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception:
        return False


def cost_of(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    price_in, price_out = PRICING.get(model, DEFAULT_PRICE)
    return prompt_tokens / 1_000_000 * price_in + completion_tokens / 1_000_000 * price_out


def summary(days: int = 7, db_path: Path | str | None = None) -> dict:
    since = (datetime.now() - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT model, purpose, prompt_tokens, completion_tokens, ok FROM llm_usage WHERE day >= ?",
            (since,),
        ).fetchall()
    finally:
        conn.close()

    total = len(rows)
    ok_count = sum(1 for row in rows if row[4])
    prompt_sum = sum(row[2] for row in rows)
    completion_sum = sum(row[3] for row in rows)
    by_model: dict[str, dict] = {}
    for model, purpose, prompt, completion, _ok in rows:
        bucket = by_model.setdefault(
            model or "(未知)", {"calls": 0, "prompt": 0, "completion": 0, "cost": 0.0}
        )
        bucket["calls"] += 1
        bucket["prompt"] += prompt
        bucket["completion"] += completion
        bucket["cost"] += cost_of(model, prompt, completion)

    return {
        "days": days,
        "since": since,
        "calls": total,
        "ok_calls": ok_count,
        "prompt_tokens": prompt_sum,
        "completion_tokens": completion_sum,
        "cost": round(sum(item["cost"] for item in by_model.values()), 4),
        "by_model": by_model,
    }


def top_intents(days: int = 7, limit: int = 5, db_path: Path | str | None = None) -> list[tuple[str, int]]:
    """最常问的意图，从审计表来。没有审计数据就返回空。"""
    since = (datetime.now() - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT intent, COUNT(*) AS n FROM audit WHERE ts >= ? AND intent != ''"
            " GROUP BY intent ORDER BY n DESC LIMIT ?",
            (since, int(limit)),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()
    return [(row[0], row[1]) for row in rows]


def render_weekly(days: int = 7, db_path: Path | str | None = None, pricing: dict | None = None) -> str:
    data = summary(days, db_path)
    price_table = pricing or PRICING

    lines = [f"**模型用量（最近 {data['days']} 天）**", ""]
    if not data["calls"]:
        lines.append("这几天没有调用记录。")
        lines.append("")
        lines.append("（说明：只有真的问了才会记账，查课表余额这类走工具、不调模型的不会出现在这里）")
        return "\n".join(lines)

    lines.append(f"· 调用 **{data['calls']}** 次，成功 {data['ok_calls']} 次")
    lines.append(
        f"· 输入 **{data['prompt_tokens']:,}** token，输出 **{data['completion_tokens']:,}** token"
    )

    for model, bucket in sorted(data["by_model"].items(), key=lambda kv: -kv[1]["calls"]):
        price_in, price_out = price_table.get(model, DEFAULT_PRICE)
        if price_in == 0 and price_out == 0:
            note = "免费额度"
        else:
            note = f"¥{price_in}/百万输入，¥{price_out}/百万输出"
        lines.append(
            f"· {model}：{bucket['calls']} 次，约 ¥{bucket['cost']:.4f}（{note}）"
        )

    lines.append(f"· **估算合计：¥{data['cost']:.4f}**")

    tops = top_intents(days, 5, db_path)
    if tops:
        lines.append("")
        lines.append("最常问的：")
        for name, count in tops:
            lines.append(f"· {name} {count} 次")
    return "\n".join(lines)
