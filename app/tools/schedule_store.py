"""整学期课表：缓存全部条目，支持按周/按星期/全量查询。

`academic schedule now` 只给今明两天；这个模块解决「这周 / 下周 / 周三 / 整学期」。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta
from typing import Any

from app.tools.classroom import connect
from app.tools.hduhelp import run_cli
SCHEMA = """
CREATE TABLE IF NOT EXISTS schedule (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    course_name TEXT NOT NULL,
    day INTEGER NOT NULL,
    period INTEGER NOT NULL,
    sections TEXT,
    weeks TEXT,
    is_this_week INTEGER NOT NULL DEFAULT 0,
    location TEXT,
    teacher TEXT,
    start_time TEXT,
    end_time TEXT,
    credit TEXT,
    class_time TEXT
);
CREATE INDEX IF NOT EXISTS idx_schedule_day ON schedule (day, period);

-- current_week 存在这里（refresh 时写入）。以前靠别的模块建表，全新部署会炸。
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

-- 用户自己改的课表。单独存，因为 refresh() 会整表删掉 schedule；
-- 查询时把它叠在 API 数据上面（remove 先剔、add 再补）。
CREATE TABLE IF NOT EXISTS schedule_custom (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action TEXT NOT NULL,          -- add / remove
    course_name TEXT NOT NULL DEFAULT '',
    day INTEGER NOT NULL,          -- 1=周一 … 7=周日
    period INTEGER NOT NULL,       -- 起始节
    end_period INTEGER,            -- 结束节，不填=只改一节
    location TEXT NOT NULL DEFAULT '',
    teacher TEXT NOT NULL DEFAULT '',
    start_time TEXT NOT NULL DEFAULT '',
    end_time TEXT NOT NULL DEFAULT '',
    weeks TEXT NOT NULL DEFAULT '[]',   -- JSON 数组；空=整学期
    created_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_custom_day ON schedule_custom (day, period);
"""

WEEKDAY_CN = {1: "周一", 2: "周二", 3: "周三", 4: "周四", 5: "周五", 6: "周六", 7: "周日"}

# 晚自习：第一学期第 1-20 周，周日至周四，第 10-12 节 18:00-20:25，周五周六没有。
# 课表接口不给这部分，由代码补进去，所以今天/本周/某天/整学期/课表图片都会带上。
STUDY_NAME = "晚自习"
STUDY_LOCATIONS = {1: "正心楼420", 2: "正心楼423", 3: "正心楼421", 4: "正心楼425", 7: "正心楼423"}
STUDY_FIRST_WEEK, STUDY_LAST_WEEK = 1, 20
STUDY_START_SECTION, STUDY_END_SECTION = 10, 12
STUDY_START_TIME, STUDY_END_TIME = "18:00", "20:25"

# 法定节假日当天没有晚自习。跨年假期安排公布后往这里补。
HOLIDAYS = {
    "2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04",
    "2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08",
    "2027-01-01",
}


def _ensure(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def _touch() -> None:
    try:
        from app.tools import sync_bitable

        sync_bitable.mark_dirty()
    except Exception:
        pass


def refresh() -> dict[str, Any]:
    """拉整学期课表写入缓存，保留 isThisWeek 标记。"""
    raw = run_cli("academic", "schedule")
    items = raw.get("data") or []
    conn = connect()
    _ensure(conn)
    try:
        conn.execute("DELETE FROM schedule")
        for it in items:
            conn.execute(
                "INSERT INTO schedule (course_name, day, period, sections, weeks, is_this_week, "
                "location, teacher, start_time, end_time, credit, class_time) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    it.get("courseName", ""),
                    int(it.get("day") or 0),
                    int(it.get("period") or 0),
                    json.dumps(it.get("sections") or [], ensure_ascii=False),
                    json.dumps(it.get("weeks") or [], ensure_ascii=False),
                    1 if it.get("isThisWeek") else 0,
                    it.get("locationName") or it.get("location") or "",
                    it.get("teacher") or it.get("teacherName") or "",
                    it.get("startTime", ""),
                    it.get("endTime", ""),
                    it.get("credit", ""),
                    it.get("classTime", ""),
                ),
            )
        conn.commit()
        total = conn.execute("SELECT COUNT(*) FROM schedule").fetchone()[0]
        courses = conn.execute("SELECT COUNT(DISTINCT course_name) FROM schedule").fetchone()[0]
        this_week = conn.execute("SELECT COUNT(*) FROM schedule WHERE is_this_week = 1").fetchone()[0]
        current = next((int(it.get("week")) for it in items if it.get("isThisWeek")), None)
        if current:
            conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('current_week', ?)", (str(current),))
            conn.commit()
    finally:
        conn.close()
    return {"ok": True, "rows": total, "courses": courses, "this_week_rows": this_week}


def current_week() -> int | None:
    """当前教学周（刷新缓存时从 isThisWeek 的记录里取）。"""
    conn = connect()
    _ensure(conn)
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = 'current_week'").fetchone()
    finally:
        conn.close()
    return int(row[0]) if row else None


def _first_monday() -> date | None:
    """第 1 周的周一。靠「今天是第几周」反推。"""
    week = current_week()
    if week is None:
        return None
    today = date.today()
    return today - timedelta(days=(today.isoweekday() - 1) + (week - 1) * 7)


def week_of(day: date) -> int | None:
    monday = _first_monday()
    if monday is None:
        return None
    return (day - monday).days // 7 + 1


def date_of(week: int, weekday: int) -> date | None:
    monday = _first_monday()
    if monday is None:
        return None
    return monday + timedelta(days=(week - 1) * 7 + weekday - 1)


def _study_row(weekday: int) -> dict[str, Any]:
    return {
        "course": STUDY_NAME,
        "day": weekday,
        "start_section": STUDY_START_SECTION,
        "end_section": STUDY_END_SECTION,
        "location": STUDY_LOCATIONS[weekday],
        "teacher": "",
        "start_time": STUDY_START_TIME,
        "end_time": STUDY_END_TIME,
    }


def study_items(week: int) -> list[dict[str, Any]]:
    """第 week 周的晚自习（节假日自动去掉）。"""
    if week < STUDY_FIRST_WEEK or week > STUDY_LAST_WEEK:
        return []
    rows: list[dict[str, Any]] = []
    for weekday in sorted(STUDY_LOCATIONS):
        day = date_of(week, weekday)
        if day is not None and day.isoformat() in HOLIDAYS:
            continue
        rows.append(_study_row(weekday))
    return rows


def study_for_day(day: date) -> dict[str, Any] | None:
    """某天的晚自习，字段名按 hduhelp 的「今天/明天」格式，方便直接拼进列表。"""
    week = week_of(day)
    if week is None or week < STUDY_FIRST_WEEK or week > STUDY_LAST_WEEK:
        return None
    weekday = day.isoweekday()
    if weekday not in STUDY_LOCATIONS or day.isoformat() in HOLIDAYS:
        return None
    return {
        "courseName": STUDY_NAME,
        "startSection": STUDY_START_SECTION,
        "endSection": STUDY_END_SECTION,
        "classroom": STUDY_LOCATIONS[weekday],
        "teacher": "",
        "duration": f"{STUDY_START_TIME} - {STUDY_END_TIME}",
    }


def _expand_weeks(text: str) -> list[int]:
    """把「1-20」「1,3,5」「1-8,10-16」展开成周次列表。空=整学期。"""
    text = (text or "").strip()
    if not text:
        return []
    out: set[int] = set()
    for part in text.replace("，", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            left, _, right = part.partition("-")
            if left.strip().isdigit() and right.strip().isdigit():
                a, b = int(left), int(right)
                out.update(range(min(a, b), max(a, b) + 1))
        elif part.isdigit():
            out.add(int(part))
    return sorted(out)


def add_custom(course: str, day: int, period: int, end_period: int | None = None,
               location: str = "", teacher: str = "", start_time: str = "",
               end_time: str = "", weeks: str = "") -> int:
    """加一节（或一段）课，叠在学校课表上面。不会影响 API 数据。"""
    from datetime import datetime as _dt

    conn = connect()
    _ensure(conn)
    try:
        cur = conn.execute(
            "INSERT INTO schedule_custom (action, course_name, day, period, end_period, "
            "location, teacher, start_time, end_time, weeks, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                "add", course.strip(), int(day), int(period),
                int(end_period) if end_period else None,
                location.strip(), teacher.strip(), start_time.strip(), end_time.strip(),
                json.dumps(_expand_weeks(weeks), ensure_ascii=False),
                _dt.now().isoformat(timespec="seconds"),
            ),
        )
        conn.commit()
        _touch()
        return int(cur.lastrowid)
    finally:
        conn.close()


def remove_custom(day: int, period: int, end_period: int | None = None,
                  weeks: str = "") -> int:
    """删掉某天某节（或一段），只作用于学校课表；再加一条 add 就是「改」。

    weeks 留空=整学期都删；填「8」就只删第 8 周那一次。
    """
    from datetime import datetime as _dt

    conn = connect()
    _ensure(conn)
    try:
        cur = conn.execute(
            "INSERT INTO schedule_custom (action, day, period, end_period, weeks, created_at) "
            "VALUES (?,?,?,?,?,?)",
            ("remove", int(day), int(period),
             int(end_period) if end_period else None,
             json.dumps(_expand_weeks(weeks), ensure_ascii=False),
             _dt.now().isoformat(timespec="seconds")),
        )
        conn.commit()
        _touch()
        return int(cur.lastrowid)
    finally:
        conn.close()


def list_custom() -> list[dict[str, Any]]:
    conn = connect()
    _ensure(conn)
    try:
        cur = conn.execute(
            "SELECT id, action, course_name, day, period, end_period, location, weeks "
            "FROM schedule_custom ORDER BY id"
        )
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        conn.close()


def undo_last() -> dict[str, Any] | None:
    """撤销最近一次自定义改动（加课/删课）。没有记录返回 None。"""
    conn = connect()
    _ensure(conn)
    try:
        row = conn.execute(
            "SELECT id, action, course_name, day, period FROM schedule_custom ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        conn.execute("DELETE FROM schedule_custom WHERE id = ?", (row[0],))
        conn.commit()
        _touch()
        return dict(zip(["id", "action", "course_name", "day", "period"], row))
    finally:
        conn.close()


def clear_custom() -> int:
    conn = connect()
    _ensure(conn)
    try:
        cur = conn.execute("DELETE FROM schedule_custom")
        conn.commit()
        _touch()
        return cur.rowcount
    finally:
        conn.close()


def _custom_in_week(weeks_json: str, week: int | None) -> bool:
    if week is None:
        return True
    try:
        arr = json.loads(weeks_json or "[]")
    except (ValueError, TypeError):
        return True
    return not arr or week in arr


def _apply_custom(rows: list[tuple], week: int | None = None,
                  weekday: int | None = None) -> list[tuple]:
    """把用户自定义叠到学校课表上：remove 先剔，add 再补。"""
    conn = connect()
    _ensure(conn)
    try:
        sql = "SELECT * FROM schedule_custom"
        params: list[Any] = []
        if weekday is not None:
            sql += " WHERE day = ?"
            params.append(weekday)
        cur = conn.execute(sql, params)
        cols = [c[0] for c in cur.description]
        customs = [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        conn.close()

    customs = [c for c in customs if _custom_in_week(c.get("weeks") or "[]", week)]
    if not customs:
        return rows

    drop: set[tuple[int, int]] = set()
    extra: list[tuple] = []
    for c in customs:
        start = int(c["period"])
        end = int(c.get("end_period") or start)
        # sections 要带整段：每行只写 [p] 的话 _merge 会把 3-4 节拆成两条
        span = list(range(start, end + 1))
        for p in span:
            if c["action"] == "remove":
                drop.add((int(c["day"]), p))
            else:
                extra.append((
                    c["course_name"], int(c["day"]), p,
                    json.dumps(span), c.get("location") or "",
                    c.get("teacher") or "", c.get("start_time") or "",
                    c.get("end_time") or "",
                ))
    # 不能只按 period 剔行：同一门课的行里 sections 是整段 [1,2,3]，
    # 删掉第 3 节后 1、2 节那两行还带着 [1,2,3]，合并出来仍是 1-3
    kept: list[tuple] = []
    for r in rows:
        if (r[1], r[2]) in drop:
            continue
        try:
            secs = json.loads(r[3] or "[]") or [r[2]]
        except (ValueError, TypeError):
            secs = [r[2]]
        remain = [s for s in secs if (r[1], s) not in drop]
        if not remain:
            continue
        if len(remain) != len(secs):
            r = (r[0], r[1], r[2], json.dumps(remain), r[4], r[5], r[6], r[7])
        kept.append(r)
    return kept + extra


def by_week(week: int) -> list[dict[str, Any]]:
    """按教学周查课表（整学期缓存里筛）。"""
    conn = connect()
    _ensure(conn)
    try:
        rows = conn.execute(
            "SELECT course_name, day, period, sections, location, teacher, start_time, end_time "
            "FROM schedule WHERE EXISTS (SELECT 1 FROM json_each(schedule.weeks) WHERE value = ?)",
            (week,),
        ).fetchall()
    finally:
        conn.close()
    items = _merge(_apply_custom(rows, week=week)) + study_items(week)
    return sorted(items, key=lambda x: (x["day"], x["start_section"]))


def _merge(rows: list[tuple]) -> list[dict[str, Any]]:
    """同一门课连续节次合并成一段。

    rows 的每行是单个 period，start/end 是那一节的时间；
    合并后要取最早节的开始时间 + 最晚节的结束时间。
    """
    merged: dict[tuple, dict[str, Any]] = {}
    for course, day, period, sections, location, teacher, start, end in rows:
        secs = json.loads(sections or "[]") or [period]
        key = (course, day, tuple(secs), location or "")
        entry = merged.get(key)
        if entry is None:
            merged[key] = {
                "course": course,
                "day": day,
                "start_section": min(secs),
                "end_section": max(secs),
                "location": location or "",
                "teacher": teacher or "",
                "start_time": start or "",
                "end_time": end or "",
                "_slots": {period: (start, end)},
            }
        else:
            entry["_slots"][period] = (start, end)

    for entry in merged.values():
        slots = entry.pop("_slots", {})
        if slots:
            first = slots.get(min(slots))
            last = slots.get(max(slots))
            if first and first[0]:
                entry["start_time"] = first[0]
            if last and last[1]:
                entry["end_time"] = last[1]
    return sorted(merged.values(), key=lambda x: (x["day"], x["start_section"]))


def apply_custom_to_items(items: list[dict[str, Any]], day: date) -> list[dict[str, Any]]:
    """把自定义改动叠到 hduhelp 格式的某天课程上。

    今日/明日是实时接口，不走缓存，所以得单独套一遍；
    否则「改了今天第 3 节」之后问今天，看到的还是原来那节课。
    """
    from app.tools.classroom import SECTION_TIME

    week = week_of(day)
    weekday = day.isoweekday()
    rows: list[tuple] = []
    for it in items or []:
        try:
            start = int(it.get("startSection") or 0)
            end = int(it.get("endSection") or start)
        except (TypeError, ValueError):
            continue
        if start <= 0:
            continue
        secs = list(range(start, end + 1))
        duration = str(it.get("duration") or "")
        if "-" in duration:
            left, _, right = duration.partition("-")
            start_time, end_time = left.strip(), right.strip()
        else:
            start_time = SECTION_TIME.get(start, ("", ""))[0]
            end_time = SECTION_TIME.get(end, ("", ""))[1]
        for p in secs:
            rows.append((
                str(it.get("courseName") or ""), weekday, p,
                json.dumps(secs), it.get("classroom") or "",
                it.get("teacher") or "", start_time, end_time,
            ))

    merged = _merge(_apply_custom(rows, week=week, weekday=weekday))
    out: list[dict[str, Any]] = []
    for m in merged:
        start_time = m["start_time"] or SECTION_TIME.get(m["start_section"], ("", ""))[0]
        end_time = m["end_time"] or SECTION_TIME.get(m["end_section"], ("", ""))[1]
        out.append({
            "courseName": m["course"],
            "startSection": m["start_section"],
            "endSection": m["end_section"],
            "classroom": m["location"],
            "teacher": m["teacher"],
            "duration": f"{start_time} - {end_time}" if start_time and end_time else "",
        })
    return sorted(out, key=lambda x: x["startSection"])


def this_week() -> list[dict[str, Any]]:
    conn = connect()
    _ensure(conn)
    try:
        rows = conn.execute(
            "SELECT course_name, day, period, sections, location, teacher, start_time, end_time "
            "FROM schedule WHERE is_this_week = 1"
        ).fetchall()
    finally:
        conn.close()
    week = current_week()
    items = _merge(_apply_custom(rows, week=week))
    if week:
        items += study_items(week)
    return sorted(items, key=lambda x: (x["day"], x["start_section"]))


def by_weekday(weekday: int) -> list[dict[str, Any]]:
    conn = connect()
    _ensure(conn)
    try:
        rows = conn.execute(
            "SELECT course_name, day, period, sections, location, teacher, start_time, end_time "
            "FROM schedule WHERE day = ? AND is_this_week = 1",
            (weekday,),
        ).fetchall()
    finally:
        conn.close()
    week = current_week()
    items = _merge(_apply_custom(rows, week=week, weekday=weekday))
    if week:
        items += [item for item in study_items(week) if item["day"] == weekday]
    return sorted(items, key=lambda x: (x["day"], x["start_section"]))


def all_courses() -> list[dict[str, Any]]:
    conn = connect()
    _ensure(conn)
    try:
        rows = conn.execute(
            "SELECT course_name, weeks, credit, teacher FROM schedule ORDER BY course_name, day, period"
        ).fetchall()
    finally:
        conn.close()
    # 同一门课有多条（不同星期/不同周模式），周次要取并集
    courses: dict[str, dict[str, Any]] = {}
    for name, weeks_json, credit, teacher in rows:
        entry = courses.setdefault(name, {"course": name, "weeks": set(), "credit": credit or "", "teacher": teacher or ""})
        entry["weeks"].update(json.loads(weeks_json or "[]"))
        if not entry["credit"] and credit:
            entry["credit"] = credit
        if not entry["teacher"] and teacher:
            entry["teacher"] = teacher
    # 用户自己加的课也要出现在整学期清单里
    for c in list_custom():
        if c["action"] != "add" or not c["course_name"]:
            continue
        entry = courses.setdefault(
            c["course_name"],
            {"course": c["course_name"], "weeks": set(), "credit": "", "teacher": c.get("teacher") or ""},
        )
        try:
            entry["weeks"].update(json.loads(c["weeks"] or "[]"))
        except (ValueError, TypeError):
            pass

    result = []
    for entry in courses.values():
        entry["weeks"] = sorted(entry["weeks"])
        result.append(entry)
    result.append(
        {
            "course": STUDY_NAME,
            "weeks": list(range(STUDY_FIRST_WEEK, STUDY_LAST_WEEK + 1)),
            "credit": "",
            "teacher": "",
        }
    )
    return sorted(result, key=lambda x: x["course"])


def status() -> dict[str, Any]:
    conn = connect()
    _ensure(conn)
    try:
        total = conn.execute("SELECT COUNT(*) FROM schedule").fetchone()[0]
    finally:
        conn.close()
    return {"rows": total}
