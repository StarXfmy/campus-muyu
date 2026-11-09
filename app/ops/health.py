"""自检：CLI 授权、数据库完整性、最近一次成功回答。

刻意不 ping 模型——那个要花钱。想确认模型活着，问一句真问题就行；
这里只看「上一次成功是什么时候」，够判断是不是坏了。
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
from datetime import datetime
from pathlib import Path

from app.config import DB_PATH, HDUHELP_CLI


def _json_blocks(raw: str):
    """CLI 会把多行 JSON 和更新提示混在一起输出，按花括号配对切块。

    hduhelp-cli 的 auth status 输出形如：
        {
          "command": "auth.status",
          "scopes": [...]
        }
        {"code":"UPDATE_AVAILABLE", ...}
    逐行 json.loads 只能拿到最后那条更新提示。
    """
    buf: list[str] = []
    depth = 0
    for line in (raw or "").splitlines():
        stripped = line.strip()
        if not depth and not stripped.startswith("{"):
            continue
        buf.append(line)
        depth += line.count("{") - line.count("}")
        if depth <= 0 and buf:
            try:
                yield json.loads("\n".join(buf))
            except json.JSONDecodeError:
                pass
            buf = []
            depth = 0


def _check_cli() -> dict:
    try:
        result = subprocess.run(
            [HDUHELP_CLI, "auth", "status", "--json"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
        )
    except FileNotFoundError:
        return {"ok": False, "detail": "找不到 hduhelp-cli"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "detail": "CLI 超时（20s）"}

    raw = (result.stdout or "").strip()
    payload = None
    for block in _json_blocks(raw):
        if block.get("command") == "auth.status" or "scopes" in block:
            payload = block
            break

    if not payload:
        tail = (result.stderr or raw or "")[-120:]
        return {"ok": False, "detail": f"读不到授权状态：{tail}"}

    # auth status 的字段是 authenticated / expired / scopes，没有 status
    if not payload.get("authenticated"):
        return {"ok": False, "detail": "未登录，需要 auth login"}
    if payload.get("expired"):
        return {"ok": False, "detail": "授权已过期，需要重新授权"}

    scopes = payload.get("scopes") or []
    if not scopes:
        return {"ok": False, "detail": "没有任何权限，需要重新授权"}

    pending = payload.get("pending") or {}
    suffix = f"，有 1 个未完成的授权（{pending.get('user_code')}）" if pending.get("user_code") else ""
    return {"ok": True, "detail": f"{len(scopes)} 个权限{suffix}"}


def _check_db(path: Path | None = None) -> dict:
    target = Path(path or DB_PATH)
    if not target.exists():
        return {"ok": False, "detail": "数据库文件不存在"}
    conn = None
    try:
        conn = sqlite3.connect(target, timeout=10)
        row = conn.execute("PRAGMA quick_check").fetchone()
        verdict = str(row[0]) if row else "无返回"
        size_mb = target.stat().st_size / 1024 / 1024
        if verdict.lower() != "ok":
            return {"ok": False, "detail": f"完整性检查：{verdict}"}
        return {"ok": True, "detail": f"{size_mb:.1f} MB，完整性 {verdict}"}
    except Exception as exc:
        return {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}
    finally:
        if conn is not None:
            conn.close()


def _check_last_ok(path: Path | None = None) -> dict:
    """最近一次成功回答的时间。不 ping 模型，只看审计表。"""
    target = Path(path or DB_PATH)
    conn = None
    try:
        conn = sqlite3.connect(target, timeout=10)
        row = conn.execute(
            "SELECT ts FROM audit WHERE ok = 1 ORDER BY id DESC LIMIT 1"
        ).fetchone()
    except sqlite3.OperationalError:
        return {"ok": True, "detail": "还没有审计记录（新装或从未回答过）"}
    except Exception as exc:
        return {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}
    finally:
        if conn is not None:
            conn.close()

    if not row:
        return {"ok": True, "detail": "还没有成功记录"}
    return {"ok": True, "detail": str(row[0])}


def check_all(db_path: Path | None = None) -> dict:
    return {
        "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "cli": _check_cli(),
        "db": _check_db(db_path),
        "model": _check_last_ok(db_path),
    }


def render(result: dict | None = None) -> str:
    data = result or check_all()
    marks = {True: "正常", False: "异常"}
    lines = [
        f"**自检 · {data['checked_at']}**",
        "",
        f"· CLI 授权：{marks[data['cli']['ok']]}（{data['cli']['detail']}）",
        f"· 数据库：{marks[data['db']['ok']]}（{data['db']['detail']}）",
        f"· 最近成功回答：{data['model']['detail']}",
    ]
    bad = [name for name in ("cli", "db") if not data[name]["ok"]]
    if bad:
        lines.append("")
        lines.append("需要处理：" + "、".join(bad))
    else:
        lines.append("")
        lines.append("三项都正常，可以照常用。")
    return "\n".join(lines)
