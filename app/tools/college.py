"""学院信息：杭电各学院官网的栏目发现与内容抓取。

主站 www.hdu.edu.cn 上那些学院链接全是空壳页（4KB 模板、零文章），
真实站点都在独立子域。各站栏目 ID 自己一套且会变，所以运行时
从首页解析栏目，不硬编码栏目路径。
"""
from __future__ import annotations

import re
import sqlite3
import time
import urllib.request
from html import unescape
from pathlib import Path
from urllib.parse import urljoin

from app.config import DB_PATH

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

# 学院名 -> (子域名, 别名)
COLLEGES: dict[str, tuple[str, tuple[str, ...]]] = {
    "机械工程学院": ("me", ("机械", "机械工程")),
    "电子信息学院": ("ee", ("电子信息", "电子", "电信", "EE")),
    "通信工程学院": ("comm", ("通信", "通信工程")),
    "自动化学院": ("auto", ("自动化", "人工智能")),
    "计算机学院": ("cs", ("计算机", "软件学院", "计院")),
    "网络空间安全学院": ("cbs", ("网络空间安全", "网安", "保密学院")),
    "材料与环境工程学院": ("cmee", ("材料与环境", "材料", "材环", "环境工程")),
    "理学院": ("sci", ("理学院",)),
    "数学学院": ("math", ("数学",)),
    "物理学院": ("phy", ("物理",)),
    "经济学院": ("eco", ("经济",)),
    "管理学院": ("management", ("管理", "管院")),
    "会计学院": ("acc", ("会计",)),
    "外国语学院": ("foreignedu", ("外国语", "外语", "英语")),
    "法学院": ("rwxy", ("法学", "法律")),
    "人文艺术与数字媒体学院": ("syxy", ("人文艺术", "数字媒体", "数媒", "文法")),
    "马克思主义学院": ("marxism", ("马克思", "马院")),
    "卓越学院": ("honor", ("卓越",)),
    "国际教育学院": ("intedu", ("国际教育", "留学生")),
    "继续教育学院": ("adultedu", ("继续教育", "成教")),
    "体育教学部": ("pe", ("体育", "体军部")),
    "圣光机联合学院": ("joint", ("圣光机",)),
    "集成电路学院": ("ic", ("集成电路", "微电子")),
}

# 问题关键词 -> 想看的栏目类型
_TOPICS: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    # (类型, 用户可能说的词, 栏目名里可能出现的词)
    ("通知", ("通知", "公告", "公示", "动态", "新闻", "最近", "最新", "发了啥", "发了什么"),
     ("通知", "公告", "公示", "动态", "新闻")),
    ("专业", ("专业", "培养方案", "课程", "教学", "本科", "学什么"),
     ("专业介绍", "专业", "培养", "本科生教育", "本科教育")),
    ("领导", ("院长", "书记", "领导", "谁管", "负责人"),
     ("现任领导", "领导", "概况", "简介")),
    ("师资", ("师资", "老师", "教师", "教授", "导师", "队伍"),
     ("师资", "教师", "教授", "导师", "队伍")),
    ("联系", ("联系", "办公", "地址", "电话", "在哪", "怎么找"),
     ("联系", "办公", "地址", "电话", "机构设置")),
    ("科研", ("科研", "研究", "平台", "团队", "成果", "实验室"),
     ("科研", "研究", "平台", "团队", "成果", "实验室")),
)

DEFAULT_TOPIC = "通知"
MEM_TTL = 600          # 进程内缓存，减少打库
CACHE_TTL = 6 * 3600   # 落库缓存，跨进程/重启有效
_cache: dict[str, tuple[float, str]] = {}
_cache_db: Path | None = None

CACHE_SCHEMA = """
CREATE TABLE IF NOT EXISTS college_cache (
    url TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    fetched_at TEXT NOT NULL DEFAULT ''
)
"""


def configure(db_path: Path | str | None = None) -> None:
    """切缓存落库位置。自验用临时库，生产留空走 config.DB_PATH。"""
    global _cache_db
    _cache_db = Path(db_path) if db_path else None
    _cache.clear()


def cache_stats(db_path: Path | str | None = None) -> dict:
    conn = _connect_cache(db_path)
    try:
        total = conn.execute("SELECT COUNT(*) FROM college_cache").fetchone()[0]
        newest = conn.execute("SELECT MAX(fetched_at) FROM college_cache").fetchone()[0]
    finally:
        conn.close()
    return {"rows": total, "newest": newest or ""}


def _connect_cache(db_path: Path | str | None = None) -> sqlite3.Connection:
    target = Path(db_path) if db_path else (_cache_db or DB_PATH)
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target, timeout=10)
    conn.executescript(CACHE_SCHEMA)
    return conn


def _cache_read(url: str) -> str | None:
    conn = _connect_cache()
    try:
        row = conn.execute(
            "SELECT body FROM college_cache WHERE url = ? AND body != ''", (url,)
        ).fetchone()
        if not row:
            return None
        age = conn.execute(
            "SELECT CAST((julianday('now','localtime') - julianday(fetched_at)) * 86400 AS INTEGER)"
            " FROM college_cache WHERE url = ?",
            (url,),
        ).fetchone()
        if age and age[0] is not None and age[0] > CACHE_TTL:
            return None
        return row[0]
    finally:
        conn.close()


def _cache_write(url: str, title: str, body: str) -> None:
    conn = _connect_cache()
    try:
        conn.execute(
            "INSERT INTO college_cache (url, title, body, fetched_at)"
            " VALUES (?, ?, ?, datetime('now','localtime'))"
            " ON CONFLICT(url) DO UPDATE SET title=excluded.title,"
            " body=excluded.body, fetched_at=excluded.fetched_at",
            (url, title, body),
        )
        conn.commit()
    finally:
        conn.close()


def names() -> list[str]:
    return list(COLLEGES)


def resolve(text: str) -> str | None:
    """从一句话里认出学院。别名按长度降序比，避免「电子」抢走「电子信息」。"""
    text = text or ""
    best: tuple[str, int] | None = None
    for name, (_host, aliases) in COLLEGES.items():
        for alias in (name,) + aliases:
            if alias and alias in text:
                if best is None or len(alias) > best[1]:
                    best = (name, len(alias))
    return best[0] if best else None


def _get(url: str, force: bool = False) -> str:
    """抓页面，三级取数：内存 → 落库缓存（6 小时）→ 网络。

    force=True 跳过前两级，用于用户明说「刷新 / 最新」。
    """
    now = time.time()
    if not force:
        hit = _cache.get(url)
        if hit and now - hit[0] < MEM_TTL:
            return hit[1]
        try:
            stored = _cache_read(url)
        except Exception:
            stored = None
        if stored is not None:
            _cache[url] = (now, stored)
            return stored

    request = urllib.request.Request(
        url, headers={"User-Agent": _UA, "Accept-Language": "zh-CN,zh;q=0.9"}
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        raw = response.read()
        charset = response.headers.get_content_charset()
    for enc in (charset, "utf-8", "gbk"):
        if not enc:
            continue
        try:
            html = raw.decode(enc)
            break
        except (UnicodeDecodeError, LookupError):
            continue
    else:
        html = raw.decode("utf-8", "ignore")

    _cache[url] = (now, html)
    try:
        title_match = re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
        title = unescape(title_match.group(1)).strip() if title_match else ""
        _cache_write(url, title, html)
    except Exception:
        pass
    return html


def home_url(college: str) -> str:
    return f"https://{COLLEGES[college][0]}.hdu.edu.cn/"


# 导航里这些不是内容栏目，别让「办公」匹配到「校内办公网」
_NAV_SKIP = ("首页", "English", "校内办公网", "校内网", "学院首页", "旧版入口", "返回")


def sections(college: str) -> list[tuple[str, str]]:
    """首页上的栏目链接：[(文字, 绝对 URL)]。去重、去导航噪声、去超长文字。"""
    home = home_url(college)
    html = _get(home)
    host = COLLEGES[college][0]
    columns: list[tuple[str, str]] = []
    articles: list[tuple[str, str]] = []
    seen: set[str] = set()
    # 属性可能用单引号（自动化学院就是这样），两种都得认
    for href, inner in re.findall(r'''(?is)<a[^>]+href=["']([^"']+)["'][^>]*>(.{0,120}?)</a>''', html):
        label = re.sub(r"<[^>]+>", "", inner)
        label = re.sub(r"\s+", "", label)
        if not label or len(label) > 24:
            continue
        if label in _NAV_SKIP:
            continue
        url = urljoin(home, href)
        if host not in url or url in seen:
            continue
        seen.add(url)
        # 首页也会挂最新文章标题，栏目（list.htm）要排在前面，否则「通知」会匹配到某篇文章
        if "/list.htm" in url or "/list.psp" in url:
            columns.append((label, url))
        else:
            articles.append((label, url))
    return columns + articles


def detect_topic(query: str) -> str:
    text = query or ""
    for topic, user_words, _column_words in _TOPICS:
        if any(word in text for word in user_words):
            return topic
    return DEFAULT_TOPIC


def _column_words(topic: str) -> tuple[str, ...]:
    for name, _user_words, column_words in _TOPICS:
        if name == topic:
            return column_words
    return ()


# 精确名优先，避免「通知」被「学术动态」「科研新闻」抢走
_PREFERRED = {
    "通知": ("通知公告", "通知信息", "本科生通知", "本科通知", "学生工作通知", "学院公告", "公告通知"),
    "专业": ("专业介绍", "专业设置", "学科专业", "本科生教育", "本科教育", "人才培养", "教学与培养"),
    "领导": ("学院领导", "现任领导", "领导班子"),
    "科研": ("科研平台", "科研机构", "研究机构", "科研团队", "科研成果", "科研概况"),
}
# 各主题下这些栏目算噪声：要通知就别给学术动态，要科研平台就别给科研通知
_TOPIC_NOISE = {
    "通知": ("新闻", "学术", "科研", "教学", "学工", "党建", "工会", "招生"),
    "科研": ("通知", "动态", "信息", "新闻"),
    "专业": ("通知", "动态", "新闻"),
}


def pick_section(college: str, topic: str) -> tuple[str, str] | None:
    """按主题挑栏目：精确名优先，再看包含匹配。"""
    try:
        found = sections(college)
    except Exception:
        return None
    for want in _PREFERRED.get(topic, ()):
        for label, url in found:
            if label == want:
                return label, url
    words = _column_words(topic)
    noise = _TOPIC_NOISE.get(topic, ())
    for label, url in found:
        if not any(word in label for word in words):
            continue
        if any(word in label for word in noise):
            continue
        return label, url
    return None


_CANDIDATE_WORDS = ("动态", "新闻", "通知", "公告", "信息", "速递", "要闻")


def candidates(college: str, limit: int = 5) -> list[tuple[str, str]]:
    """没有标准通知栏目时，把可能沾边的入口列出来让人自己点。"""
    try:
        found = sections(college)
    except Exception:
        return []
    out = []
    for label, url in found:
        if any(word in label for word in _CANDIDATE_WORDS):
            out.append((label, url))
        if len(out) >= limit:
            break
    return out


def _date_from_url(url: str) -> str:
    """苏迪站的 URL 自带发布日期：/2026/1021/c3778a301819/page.htm → 10-21。

    页面上的日期常和标题错位（日期列在左边或跨行），URL 里的不会错。
    """
    match = re.search(r"/(20\d{2})/(\d{2})(\d{2})/", url or "")
    if not match:
        return ""
    return f"{match.group(2)}-{match.group(3)}"


def _date_near(html: str, pos: int) -> str:
    tail = html[pos:pos + 400]
    match = re.search(r"20\d\d-(\d{2})-(\d{2})", tail)
    if match:
        return f"{match.group(1)}-{match.group(2)}"
    match = re.search(r"20\d\d/(\d{2})/(\d{2})", tail)
    if match:
        return f"{match.group(1)}-{match.group(2)}"
    match = re.search(r"\b(\d{1,2})-(\d{1,2})\b", tail)
    if match:
        return f"{int(match.group(1)):02d}-{int(match.group(2)):02d}"
    return ""


def parse_items(html: str, base: str, limit: int = 8) -> list[dict]:
    """从列表页抽文章条目：标题 + 日期 + 链接。"""
    items: list[dict] = []
    seen: set[str] = set()
    titles: set[str] = set()
    pattern = re.compile(
        r'''(?is)<a[^>]+href=["']([^"']*(?:page|content|info)[^"']*)["'][^>]*>(.{0,240}?)</a>'''
    )
    for match in pattern.finditer(html):
        title = unescape(re.sub(r"<[^>]+>", "", match.group(2)))
        title = title.replace("\xa0", " ")
        title = re.sub(r"\s+", " ", title).strip()
        if len(title) < 6:
            continue
        url = urljoin(base, match.group(1))
        if url in seen:
            continue
        # 同标题只留一条：列表页里常有「开启程序人生」这种同名不同 ID 的老文章
        key = title[:30]
        if key in titles:
            continue
        seen.add(url)
        titles.add(key)
        date = _date_from_url(url) or _date_near(html, match.end())
        items.append({"title": title, "date": date, "url": url})
        if len(items) >= limit:
            break
    return items


def items_with_dates(html: str, base: str, limit: int = 6) -> list[dict]:
    """只收带日期的链接——首页导航和纯栏目入口没有日期，正好被筛掉。

    用来兜底「列表页是脚本渲染」的学院：它们的首页反而有静态的最新条目。
    """
    out: list[dict] = []
    seen: set[str] = set()
    titles: set[str] = set()
    pattern = re.compile(
        r'''(?is)<a[^>]+href=["']([^"']*(?:page|content|info)[^"']*)["'][^>]*>(.{6,240}?)</a>'''
    )
    for match in pattern.finditer(html):
        title = unescape(re.sub(r"<[^>]+>", "", match.group(2)))
        title = re.sub(r"\s+", " ", title.replace("\xa0", " ")).strip()
        if len(title) < 6:
            continue
        url = urljoin(base, match.group(1))
        date = _date_from_url(url) or _date_near(html, match.end())
        if not date:
            continue
        if url in seen:
            continue
        key = title[:30]
        if key in titles:
            continue
        seen.add(url)
        titles.add(key)
        out.append({"title": title, "date": date, "url": url})
        if len(out) >= limit:
            break
    return out


def page_text(html: str, limit: int = 900) -> str:
    """正文纯文本：去脚本样式、去标签、压空白。"""
    body = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", body)
    text = text.replace("&nbsp;", " ").replace("&#160;", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def wants_fresh(keyword: str) -> bool:
    """用户明说要最新的才绕过缓存，否则一律走缓存，别反复打学校服务器。"""
    text = keyword or ""
    return any(word in text for word in ("刷新", "最新", "重新抓", "更新一下"))


def query(college: str, keyword: str = "", limit: int = 8) -> dict:
    """查一个学院的某类信息。

    返回 {college, home, topic, section, url, items, text, error}
    列表页给 items，内容页给 text；两样都可能为空，调用方据此判断抓没抓到。
    """
    force = wants_fresh(keyword)
    result: dict = {
        "college": college,
        "home": home_url(college),
        "topic": detect_topic(keyword),
        "section": "",
        "url": "",
        "items": [],
        "text": "",
        "candidates": [],
        "error": "",
    }
    picked = pick_section(college, result["topic"])
    if not picked:
        result["error"] = "no_section"
        # 没有通知栏目也不能空手：首页通常有静态的最新条目
        if result["topic"] == DEFAULT_TOPIC:
            try:
                home_items = items_with_dates(_get(home_url(college), force), home_url(college), limit)
            except Exception:
                home_items = []
            if home_items:
                result["items"] = home_items
                result["section"] = "首页最新"
                result["url"] = home_url(college)
                result["error"] = ""
                return result
        result["candidates"] = candidates(college)
        return result

    label, url = picked
    result["section"] = label
    result["url"] = url
    try:
        html = _get(url, force)
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}"
        return result

    items = parse_items(html, url, limit)
    if not items and result["topic"] == DEFAULT_TOPIC:
        # 列表页是脚本渲染的（自动化、外国语这类），回首页捞带日期的条目
        try:
            home_items = items_with_dates(_get(home_url(college), force), home_url(college), limit)
        except Exception:
            home_items = []
        if home_items:
            items = home_items
            # 首页混着新闻和通知，不能还挂原栏目名，否则像是该栏目发的内容
            result["section"] = "首页最新（原列表页脚本加载，读不出条目）"
            result["url"] = home_url(college)

    if items:
        result["items"] = items
    else:
        result["text"] = page_text(html)
    return result
