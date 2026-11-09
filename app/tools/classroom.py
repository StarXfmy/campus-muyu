"""空教室：楼栋索引 + 直接调接口查实时空闲。

不再本地缓存占用数据——不同楼栋的周次数据不一致（正心楼是第10周、教研楼是第1周），
本地算会错。接口支持 `--status unused`，服务端直接给空闲教室，用它最准。
"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime
from typing import Any

from app.config import DB_PATH
from app.tools.hduhelp import list_classrooms as fetch_classrooms, list_empty_rooms


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    return conn

WEEKDAY_CN = ["", "周一", "周二", "周三", "周四", "周五", "周六", "周日"]
SECTION_TIME = {
    1: ("08:40", "09:25"), 2: ("09:30", "10:15"), 3: ("10:25", "11:10"),
    4: ("11:15", "12:00"), 5: ("13:30", "14:15"), 6: ("14:20", "15:05"),
    7: ("15:15", "16:00"), 8: ("16:05", "16:50"), 9: ("18:30", "19:15"),
    10: ("19:20", "20:05"), 11: ("20:10", "20:55"),
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS building_index (
    building_id TEXT NOT NULL,
    name TEXT NOT NULL,
    PRIMARY KEY (building_id, name)
);
"""


def _ensure(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def refresh_buildings() -> dict[str, Any]:
    """拉一次教室列表重建楼栋索引（只有楼名 → buildingID 的映射，几百行）。"""
    rooms = fetch_classrooms()
    conn = connect()
    _ensure(conn)
    try:
        conn.execute("DELETE FROM building_index")
        seen = set()
        for room in rooms:
            building_id = room.get("buildingID") or ""
            name = room.get("locationName") or ""
            if not building_id or not name:
                continue
            key = (building_id, name)
            if key in seen:
                continue
            seen.add(key)
            conn.execute("INSERT OR IGNORE INTO building_index (building_id, name) VALUES (?,?)", key)
        conn.commit()
        count = conn.execute("SELECT COUNT(DISTINCT building_id) FROM building_index").fetchone()[0]
    finally:
        conn.close()
    return {"ok": True, "buildings": count, "names": len(seen)}


def _name_prefix(name: str) -> str:
    """从「青越201」「正心楼503」「第3教研楼101」里取楼名前缀。"""
    match = re.match(r"^(第\d+教研楼)", name or "")
    if match:
        return match.group(1)
    match = re.match(r"^([\u4e00-\u9fa5]+)", name or "")
    return match.group(1) if match else ""


def resolve_building(text: str) -> str | None:
    """把「正心楼」「创智」「青越」这类说法映射成 buildingID。"""
    if not text:
        return None
    conn = connect()
    _ensure(conn)
    try:
        rows = conn.execute("SELECT building_id, name FROM building_index").fetchall()
    finally:
        conn.close()
    for building_id, _ in rows:
        if building_id and building_id in text:
            return building_id
    for building_id, name in rows:
        prefix = _name_prefix(name)
        if prefix and prefix in text:
            return building_id
    return None


def building_label(building: str) -> str:
    conn = connect()
    _ensure(conn)
    try:
        row = conn.execute(
            "SELECT name FROM building_index WHERE building_id = ? ORDER BY name LIMIT 1",
            (building,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return f"{building} 楼"
    prefix = _name_prefix(row[0])
    if prefix:
        return prefix if prefix.endswith("楼") else f"{prefix}楼"
    return f"{building} 楼"


def cached_buildings() -> list[str]:
    conn = connect()
    _ensure(conn)
    try:
        rows = conn.execute("SELECT DISTINCT building_id FROM building_index ORDER BY building_id").fetchall()
    finally:
        conn.close()
    return [r[0] for r in rows]


def find_empty(building: str, weekday: int, sections: list[int]) -> list[dict[str, Any]]:
    """直接问接口要空闲教室。"""
    if not sections:
        return []
    rooms = list_empty_rooms(building, weekday=weekday, sections=sections)
    return [{"location_id": r.get("locationID", ""), "name": r.get("locationName", ""), "self_study": bool(r.get("selfStudy"))} for r in rooms]


def room_floor(name: str) -> int | None:
    """从房间号推断楼层：「正心楼402」→ 4，「正心楼1201」→ 12。"""
    nums = re.findall(r"\d+", name or "")
    if not nums:
        return None
    num = nums[-1]
    if len(num) >= 3:
        return int(num[:-2])
    return None


def is_in_session(now: datetime | None = None) -> bool:
    """当前是否落在某个节次的时间段内。"""
    now = now or datetime.now()
    minutes = now.hour * 60 + now.minute
    for start, end in SECTION_TIME.values():
        s_h, s_m = map(int, start.split(":"))
        e_h, e_m = map(int, end.split(":"))
        if s_h * 60 + s_m <= minutes <= e_h * 60 + e_m:
            return True
    return False


def current_sections(now: datetime | None = None) -> list[int]:
    """按当前时间推断正在进行的节次；不在上课时段则返回下一节。"""
    now = now or datetime.now()
    minutes = now.hour * 60 + now.minute
    for section, (start, end) in SECTION_TIME.items():
        s_h, s_m = map(int, start.split(":"))
        e_h, e_m = map(int, end.split(":"))
        if s_h * 60 + s_m <= minutes <= e_h * 60 + e_m:
            return [section]
    for section, (start, _) in SECTION_TIME.items():
        s_h, s_m = map(int, start.split(":"))
        if minutes < s_h * 60 + s_m:
            return [section]
    return []


def describe_section(sections: list[int]) -> str:
    if not sections:
        return "今天已无课"
    s = sections[0]
    start, end = SECTION_TIME.get(s, ("", ""))
    return f"第 {s} 节（{start}-{end}）"
