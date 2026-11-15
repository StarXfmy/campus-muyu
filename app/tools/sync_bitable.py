"""跨机器同步：微信端（本机）和飞书端（云端）共用一张飞书多维表格。

对话读取永远走本地 SQLite，不受网络影响；后台每 15 秒对一次表。
表里只有一条记录，payload 存三类数据的全量 JSON，updated_at 是毫秒时间戳。

冲突规则：谁最后写谁生效（LWW）。本地有未推送的改动就推；远端更新就拉。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.request

from app import config  # noqa: F401  确保 .env 已加载

DB_PATH = config.DB_PATH
APP_ID = os.getenv("FEISHU_APP_ID", "").strip()
APP_SECRET = os.getenv("FEISHU_APP_SECRET", "").strip()
# 这两个是资源 ID，不是密钥：要读写还得先拿 app_secret 换 tenant_access_token，
# 所以直接给默认值，云端不用改 .env（那边写凭证会被安全策略拦）
APP_TOKEN = os.getenv("BITABLE_APP_TOKEN", "").strip() or "TymGbnfjaaOXDMs9FUZc70CanIh"
TABLE_ID = os.getenv("BITABLE_TABLE_ID", "").strip() or "tblibVPvd1scb27a"
INTERVAL = 15

ENABLED = bool(APP_ID and APP_SECRET and APP_TOKEN and TABLE_ID)

_wake = threading.Event()
_started = False
_token = {"value": "", "expire_at": 0.0}
_record_id = {"value": ""}


def log(*args) -> None:
    print("[sync]", *args, flush=True)


# ---------- 本地状态 ----------

def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS sync_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    conn.commit()


def _get(key: str) -> str:
    conn = _connect()
    _ensure(conn)
    try:
        row = conn.execute("SELECT value FROM sync_meta WHERE key = ?", (key,)).fetchone()
    finally:
        conn.close()
    return str(row[0]) if row else ""


def _set(**kv: str) -> None:
    conn = _connect()
    _ensure(conn)
    try:
        for key, value in kv.items():
            conn.execute(
                "INSERT INTO sync_meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)),
            )
        conn.commit()
    finally:
        conn.close()


def mark_dirty() -> None:
    """本地写操作后调用：标记有未推送的改动，并叫醒后台立刻推。"""
    if not ENABLED:
        return
    try:
        _set(local_dirty="1", local_ms=str(int(time.time() * 1000)))
        _wake.set()
    except Exception as exc:  # noqa: BLE001
        log("标记失败：", repr(exc))


# ---------- 数据打包 ----------

def _ensure_all(conn: sqlite3.Connection) -> None:
    from app.tools import memory, persona_rules, schedule_store

    memory._ensure(conn)
    persona_rules._ensure(conn)
    schedule_store._ensure(conn)


def _expense_key(row: dict) -> tuple[str, str, str]:
    """流水的行级身份：同一天 + 同金额 + 同店名算同一笔。金额归一到两位小数，
    免得 5.15 和 5.150 被当成两笔。"""
    try:
        amount = "%.2f" % float(row.get("amount"))
    except (TypeError, ValueError):
        amount = str(row.get("amount") or "").strip()
    return (str(row.get("day") or "").strip(), amount, str(row.get("place") or "").strip())


def _merge_expenses(remote: list[dict]) -> int:
    """把远端流水并进本地，本地独有的不动，返回新增笔数。

    刻意不走 _apply 那种「先删表再整表插」：两端都会各自补录消费，覆盖式同步
    会把对端刚记的那笔冲掉。
    """
    if not remote:
        return 0
    conn = _connect()
    try:
        have = {_expense_key(dict(r)) for r in conn.execute(
            "SELECT day, amount, place FROM expense_log"
        ).fetchall()}
    finally:
        conn.close()
    added = 0
    conn = _connect()
    try:
        for row in remote:
            key = _expense_key(row)
            if not key[0] or key in have:
                continue
            try:
                amount = float(row.get("amount"))
            except (TypeError, ValueError):
                continue
            conn.execute(
                "INSERT INTO expense_log (chat_id, happened_at, day, amount, place) "
                "VALUES (?,?,?,?,?)",
                (
                    str(row.get("chat_id") or "sync"),
                    str(row.get("happened_at") or ""),
                    key[0],
                    amount,
                    key[2],
                ),
            )
            have.add(key)
            added += 1
        conn.commit()
    finally:
        conn.close()
    return added


def _dump() -> dict:
    conn = _connect()
    _ensure(conn)
    _ensure_all(conn)
    try:
        memories = [
            dict(r) for r in conn.execute(
                "SELECT id, chat_id, content, created_at, remind_at, reminded "
                "FROM memories ORDER BY id"
            ).fetchall()
        ]
        rules = [
            dict(r) for r in conn.execute(
                "SELECT id, content, created_at FROM persona_rules ORDER BY id"
            ).fetchall()
        ]
        customs = [
            dict(r) for r in conn.execute(
                "SELECT id, action, course_name, day, period, end_period, location, teacher, "
                "start_time, end_time, weeks, created_at FROM schedule_custom ORDER BY id"
            ).fetchall()
        ]
        expenses = [
            dict(r) for r in conn.execute(
                "SELECT id, chat_id, day, amount, place, happened_at "
                "FROM expense_log ORDER BY id"
            ).fetchall()
        ]
    finally:
        conn.close()
    return {
        "v": 1,
        "saved_at": int(time.time() * 1000),
        "memories": memories,
        "persona_rules": rules,
        "schedule_custom": customs,
        "expenses": expenses,
    }


def _apply(payload: dict) -> None:
    """用远端数据覆盖本地三张表。id 保留，方便 undo 的顺序不变。

    流水不在这里——两端都会各自补录，覆盖会冲掉对端新记的那笔，
    所以走 _merge_expenses() 做行级合并。
    """
    memories = payload.get("memories") or []
    rules = payload.get("persona_rules") or []
    customs = payload.get("schedule_custom") or []
    conn = _connect()
    _ensure(conn)
    _ensure_all(conn)
    try:
        conn.execute("DELETE FROM memories")
        conn.executemany(
            "INSERT INTO memories (id, chat_id, content, created_at, remind_at, reminded) "
            "VALUES (?,?,?,?,?,?)",
            [(int(m.get("id") or 0), str(m.get("chat_id") or "sync"),
              str(m.get("content") or ""), str(m.get("created_at") or ""),
              str(m.get("remind_at") or ""), int(m.get("reminded") or 0))
             for m in memories],
        )
        conn.execute("DELETE FROM persona_rules")
        conn.executemany(
            "INSERT INTO persona_rules (id, content, created_at) VALUES (?,?,?)",
            [(int(r.get("id") or 0), str(r.get("content") or ""),
              str(r.get("created_at") or "")) for r in rules],
        )
        conn.execute("DELETE FROM schedule_custom")
        conn.executemany(
            "INSERT INTO schedule_custom (id, action, course_name, day, period, end_period, "
            "location, teacher, start_time, end_time, weeks, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [(
                int(c.get("id") or 0), str(c.get("action") or ""),
                str(c.get("course_name") or ""), int(c.get("day") or 0),
                int(c.get("period") or 0),
                int(c["end_period"]) if c.get("end_period") not in (None, "") else None,
                str(c.get("location") or ""), str(c.get("teacher") or ""),
                str(c.get("start_time") or ""), str(c.get("end_time") or ""),
                str(c.get("weeks") or "[]"), str(c.get("created_at") or ""),
            ) for c in customs],
        )
        conn.commit()
    finally:
        conn.close()


# ---------- 飞书 API ----------

def _request(method: str, url: str, body: dict | None = None) -> dict:
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if _token["value"]:
        headers["Authorization"] = "Bearer " + _token["value"]
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return json.loads(raw)
        except ValueError:
            return {"code": exc.code, "msg": raw[:300]}


def _access_token() -> str:
    if _token["value"] and time.time() < _token["expire_at"] - 120:
        return _token["value"]
    data = _request(
        "POST",
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        {"app_id": APP_ID, "app_secret": APP_SECRET},
    )
    token = data.get("tenant_access_token") or ""
    if not token:
        raise RuntimeError(f"换 token 失败：{data.get('code')} {data.get('msg')}")
    _token["value"] = token
    _token["expire_at"] = time.time() + int(data.get("expire") or 7200)
    return token


def _text_of(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(
            seg.get("text", "") if isinstance(seg, dict) else str(seg) for seg in value
        )
    return str(value or "")


def _fetch() -> dict | None:
    """读表里最新的一条记录。没有记录返回 None。"""
    _access_token()
    url = (
        f"https://open.feishu.cn/open-apis/bitable/v1/apps/{APP_TOKEN}"
        f"/tables/{TABLE_ID}/records?page_size=20"
    )
    data = _request("GET", url)
    if data.get("code") != 0:
        raise RuntimeError(f"读表失败：{data.get('code')} {data.get('msg')}")
    items = (data.get("data") or {}).get("items") or []
    best = None
    for item in items:
        fields = item.get("fields") or {}
        try:
            updated = int(float(_text_of(fields.get("updated_at")) or 0))
        except (TypeError, ValueError):
            updated = 0
        if best is None or updated > best["updated_at"]:
            best = {
                "record_id": item.get("record_id") or "",
                "payload": _text_of(fields.get("payload")),
                "updated_at": updated,
            }
    return best


def _push(payload: dict) -> int:
    """把全量数据写进表里，返回写入的时间戳。"""
    _access_token()
    now_ms = int(time.time() * 1000)
    fields = {
        "payload": json.dumps(payload, ensure_ascii=False),
        "updated_at": now_ms,
    }
    base = (
        f"https://open.feishu.cn/open-apis/bitable/v1/apps/{APP_TOKEN}/tables/{TABLE_ID}"
    )
    record_id = _record_id["value"]
    if record_id:
        data = _request("PUT", f"{base}/records/{record_id}", {"fields": fields})
        if data.get("code") == 0:
            return now_ms
        log("更新记录失败，重试新建：", data.get("code"), data.get("msg"))
        _record_id["value"] = ""
    data = _request("POST", f"{base}/records", {"fields": fields})
    if data.get("code") != 0:
        raise RuntimeError(f"写表失败：{data.get('code')} {data.get('msg')}")
    _record_id["value"] = ((data.get("data") or {}).get("record") or {}).get("record_id") or ""
    return now_ms


def _count(payload: dict) -> int:
    return sum(len(payload.get(k) or []) for k in (
        "memories", "persona_rules", "schedule_custom", "expenses",
    ))


def _sync_once() -> None:
    remote = _fetch()
    local_ms = int(_get("local_ms") or 0)
    dirty = _get("local_dirty") == "1"
    first_time = not _get("last_remote_ms")

    if remote:
        _record_id["value"] = remote["record_id"]
        raw = _text_of(remote["payload"])
        if raw:
            try:
                payload = json.loads(raw)
            except ValueError:
                payload = None
            if isinstance(payload, dict):
                merged = _merge_expenses(payload.get("expenses") or [])
                if merged:
                    log("流水合并新增 ", merged, " 笔")
                local_count = _count(_dump())
                # 首次对表时谁的数据多用谁，避免刚开通就被空表冲掉
                if first_time and local_count > _count(payload):
                    pass
                elif remote["updated_at"] > local_ms:
                    _apply(payload)
                    _set(local_dirty="0", local_ms=str(remote["updated_at"]),
                         last_remote_ms=str(remote["updated_at"]))
                    log("已拉取远端（", remote["updated_at"], "）")
                    if not merged:
                        return
                    # 合并进了新流水就得把并集推回去，不然另一端永远看不到
                elif not dirty and not merged and local_count <= _count(payload):
                    # 「没变化」不能只看 dirty：远端快照还是旧结构（不带 expenses 字段）时，
                    # dirty=0、merged=0，本地 30 条流水就永远推不上去（实测复现过）。
                    _set(last_remote_ms=str(remote["updated_at"]))
                    return

    now_ms = _push(_dump())
    _set(local_dirty="0", local_ms=str(now_ms), last_remote_ms=str(now_ms))
    log("已推送本地（", now_ms, "）")


def _loop() -> None:
    while True:
        try:
            if ENABLED:
                _sync_once()
        except Exception as exc:  # noqa: BLE001
            log("同步失败（稍后重试）：", repr(exc))
        _wake.wait(INTERVAL)
        _wake.clear()


def start() -> None:
    global _started
    if _started:
        return
    _started = True
    if not ENABLED:
        log("未配置，跳过（需要 BITABLE_APP_TOKEN / BITABLE_TABLE_ID）")
        return
    thread = threading.Thread(target=_loop, daemon=True, name="bitable-sync")
    thread.start()
    log("已启动，每", INTERVAL, "秒同步一次")
