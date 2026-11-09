"""公共问答：静态题库 + 用户收录条目，双路检索。

检索分两路：
- 两字及以下的查询走关键词打分（trigram 至少要 3 个字符，中文两字词它会漏）
- 三字及以上先走 FTS5 召回，再在召回集上按关键词打分排序
  FTS5 一条都没召回时，回落到全量关键词打分——这条路保证不会因为索引问题丢掉答案

每条都标了出处。抓不到官网原文的条目一律写 [待补]，
不凭常识写校规——答辩时被问「哪规定的」比少几条更致命。
"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime

from app.config import DB_PATH

ITEMS: list[dict] = [
    {
        "title": "成绩单 / 在读证明",
        "keywords": ("成绩单", "在读证明", "证明", "出国", "盖章"),
        "answer": "教务处「成绩管理」栏目有《出国成绩单及证明办理》通知，按通知里的时间、材料和地点办。",
        "source": "https://jwc.hdu.edu.cn/cjgl/list.htm",
        "detail": "https://jwc.hdu.edu.cn/2026/0617/c13477a297724/page.htm",
    },
    {
        "title": "考试纪律与作弊后果",
        "keywords": ("作弊", "考试纪律", "违纪", "处分", "带手机"),
        "answer": "教务处发过《关于进一步加强考试管理和严肃考试纪律的通知》，具体处理办法看原文。",
        "source": "https://jwc.hdu.edu.cn/ksgl/list.htm",
        "detail": "https://jwc.hdu.edu.cn/2026/0616/c13475a297601/page.htm",
    },
    {
        "title": "缓考 / 补考",
        "keywords": ("缓考", "补考", "重修", "挂科"),
        "answer": "缓考、补考安排都发在教务处「考试管理」栏目，按学期通知走，具体时间以当期通知为准。",
        "source": "https://jwc.hdu.edu.cn/ksgl/list.htm",
    },
    {
        "title": "休学 / 复学 / 退学",
        "keywords": ("休学", "复学", "退学", "学籍异动"),
        "answer": "学籍异动归教务处「学籍管理」栏目，办理手续和表格都在那一栏，具体规定原文 [待补]。",
        "source": "https://jwc.hdu.edu.cn/xjgl/list.htm",
    },
    {
        "title": "转专业",
        "keywords": ("转专业", "换专业", "转系"),
        "answer": (
            "2026 级转专业细则已发（教务处 2026-09-16）：转入计划数 = 该专业 2026 级招生人数 × 转入比例；"
            "从大一第二学期起可申请，大一第二学期 / 大二第一学期 / 大二第二学期三个时点要求的先修课程不同。"
            "各学院考核方案、咨询电话都写在细则的 Excel 附件里。依据文件是杭电教【2025】136 号。"
            "这是 2026 级口径，往届细则作废。"
        ),
        "source": "https://jwc.hdu.edu.cn/xjgl/list.htm",
        "detail": "https://jwc.hdu.edu.cn/2026/0916/c13474a300382/page.htm",
    },
    {
        "title": "选课 / 退课",
        "keywords": ("选课", "退课", "抢课", "选不上"),
        "answer": "选课、退课在教务处「排课选课」栏目，每学期有固定轮次，错过只能等下一轮或找开课学院。",
        "source": "https://jwc.hdu.edu.cn/pkxk/list.htm",
    },
    {
        "title": "毕业 / 学位条件",
        "keywords": ("毕业", "学位", "学位证", "毕业证", "学分要求"),
        "answer": "毕业与学位审核归教务处「毕业管理」栏目，具体要求按你所在年级的培养方案执行。",
        "source": "https://jwc.hdu.edu.cn/bygl/list.htm",
    },
    {
        "title": "校历 / 放假安排",
        "keywords": ("校历", "放假", "开学", "寒假", "暑假", "调休"),
        "answer": "校历和放假安排发在教务处「通知公告」栏目，以最新一份校历为准。",
        "source": "https://jwc.hdu.edu.cn/13022/list.htm",
    },
    {
        "title": "校园网 / 网费",
        "keywords": ("校园网", "网费", "断网", "上网", "wifi", "网络"),
        "answer": "校园网归信息技术中心管，开通、缴费、报障都在他们站点。",
        "source": "https://nic.hdu.edu.cn/",
    },
    {
        "title": "校医院 / 就医",
        "keywords": ("校医院", "看病", "医务室", "发烧", "开药"),
        "answer": "校医院有独立站点，门诊时间、科室和医保相关说明在上面。",
        "source": "https://xyy.hdu.edu.cn/",
    },
    {
        "title": "医保 / 报销",
        "keywords": ("医保", "报销", "医疗费", "社保"),
        "answer": "[待补] 医保报销的具体口径本鱼还没找到官网原文，建议直接问校医院或学生处，别按网上的说法办。",
        "source": "https://xyy.hdu.edu.cn/",
    },
    {
        "title": "一卡通 / 校园卡补办",
        "keywords": ("一卡通", "校园卡丢", "饭卡丢", "补办卡", "卡消磁"),
        "answer": "[待补] 补办地点和工本费本鱼没查到官网原文。先在「杭电助手」的服务大厅找卡务入口，或直接问卡务中心。",
        "source": "",
    },
    {
        "title": "奖学金 / 助学金",
        "keywords": ("奖学金", "助学金", "国家励志", "评奖", "困难认定"),
        "answer": "[待补] 评定办法和名额分配本鱼还没抓到官网原文，这类每年都变，建议看学生处通知或问辅导员。",
        "source": "",
    },
    {
        "title": "助学贷款",
        "keywords": ("助学贷款", "生源地贷款", "贷款"),
        "answer": "[待补] 生源地贷款和校园地贷款的办理口径不同，本鱼还没找到官网原文，问学生处或辅导员最准。",
        "source": "",
    },
    {
        "title": "宿舍报修",
        "keywords": ("宿舍报修", "寝室坏了", "水电维修", "空调坏", "漏水", "灯坏", "没水"),
        "answer": (
            "绍兴校区找后勤绍兴服务部：维修部 0575-89110660、物业组 0575-89110241；"
            "宿舍楼值班室号码在后勤「办公电话」页，表里以 5 号楼为例是 0575-89110225。"
            "杭州校区打后勤服务一号通 86915000。号码出自后勤官网办公电话表，报修前先说清楼栋房号。"
        ),
        "source": "https://hq.hdu.edu.cn/13000/list.htm",
    },
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS kb_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    keywords TEXT NOT NULL DEFAULT '',
    answer TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '',
    fetched_at TEXT NOT NULL DEFAULT '',
    verified_by_user INTEGER NOT NULL DEFAULT 0,
    raw_excerpt TEXT NOT NULL DEFAULT '',
    origin TEXT NOT NULL DEFAULT 'static',
    UNIQUE(origin, title)
);
CREATE VIRTUAL TABLE IF NOT EXISTS faq_fts USING fts5(
    title, keywords, answer,
    content='kb_items', content_rowid='id',
    tokenize='trigram'
);
"""


def connect(db_path=None) -> sqlite3.Connection:
    target = db_path or DB_PATH
    conn = sqlite3.connect(target, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def ensure(conn: sqlite3.Connection) -> None:
    """建表并把静态题库同步进去。只在有新条目时重建 FTS 索引。"""
    conn.executescript(SCHEMA)
    added = 0
    for item in ITEMS:
        cursor = conn.execute(
            "INSERT OR IGNORE INTO kb_items"
            " (title, keywords, answer, source, detail, origin)"
            " VALUES (?, ?, ?, ?, ?, 'static')",
            (
                item["title"],
                ",".join(item["keywords"]),
                item["answer"],
                item.get("source", ""),
                item.get("detail", ""),
            ),
        )
        added += cursor.rowcount
    if added:
        conn.execute("INSERT INTO faq_fts(faq_fts) VALUES('rebuild')")
    conn.commit()


def _row_to_item(row: sqlite3.Row) -> dict:
    return {
        "title": row["title"],
        "keywords": tuple(k for k in str(row["keywords"]).split(",") if k),
        "answer": row["answer"],
        "source": row["source"],
        "detail": row["detail"],
        "origin": row["origin"],
        "verified_by_user": bool(row["verified_by_user"]),
    }


def _all_items(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT title, keywords, answer, source, detail, origin, verified_by_user"
        " FROM kb_items ORDER BY id"
    ).fetchall()
    return [_row_to_item(row) for row in rows]


def _grams(text: str) -> str:
    """把查询切成 3 字滑窗，用 OR 连起来——trigram 只认连续匹配，整串匹配会漏。"""
    seen: list[str] = []
    for index in range(len(text) - 2):
        gram = text[index:index + 3]
        if gram not in seen:
            seen.append(gram)
    if not seen:
        return ""
    return " OR ".join('"' + gram.replace('"', '""') + '"' for gram in seen)


def _fts_items(conn: sqlite3.Connection, text: str) -> list[dict]:
    query = _grams(text)
    if not query:
        return []
    try:
        rows = conn.execute(
            "SELECT k.title, k.keywords, k.answer, k.source, k.detail, k.origin, k.verified_by_user"
            " FROM faq_fts f JOIN kb_items k ON k.id = f.rowid"
            " WHERE faq_fts MATCH ?",
            (query,),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [_row_to_item(row) for row in rows]


def _rank_by_keywords(
    items: list[dict], text: str, limit: int, baseline: int = 0
) -> list[dict]:
    """关键词命中数打分，同分取先出现的。标题里的主题词额外加权。

    baseline 给 FTS 召回的条目一个保底分：正文里能连续命中的词未必写在
    keywords 里（比如「电磁炉」只出现在 detail），不该因此被丢掉。
    """
    lowered = (text or "").lower()
    scored: list[tuple[int, int, dict]] = []
    for index, item in enumerate(items):
        score = sum(1 for keyword in item["keywords"] if keyword.lower() in lowered)
        # 「图书馆几点关门」要落到图书馆，别被门禁条目的「几点关门」抢走
        score += 3 * sum(
            1
            for word in re.split(r"[/\s]+", item["title"])
            if len(word) >= 2 and word.lower() in lowered
        )
        if score or baseline:
            scored.append((score + baseline, -index, item))
    scored.sort(reverse=True)
    return [item for _, _, item in scored[:limit]]


def search(query: str, limit: int = 1, db_path=None) -> list[dict]:
    text = (query or "").strip()
    if not text:
        return []

    conn = connect(db_path)
    try:
        ensure(conn)
        pool = _all_items(conn)
        if len(text) <= 2:
            return _rank_by_keywords(pool, text, limit)
        candidates = _fts_items(conn, text)
        if candidates:
            ranked = _rank_by_keywords(candidates, text, limit, baseline=1)
            if ranked:
                return ranked
        return _rank_by_keywords(pool, text, limit)
    finally:
        conn.close()


def render(item: dict) -> str:
    lines = [f"**{item['title']}**", item["answer"]]
    if item.get("source"):
        lines.append(f"出处：{item['source']}")
    if item.get("detail"):
        lines.append(f"原文：{item['detail']}")
    lines.append("（以学校最新通知为准，本鱼只负责指路）")
    return "\n".join(lines)
