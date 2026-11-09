"""抓网页正文给模型读：去脚本样式、去标签、保留标题。"""
from __future__ import annotations

import re
import urllib.error
import urllib.request
from urllib.parse import quote, urlsplit, urlunsplit

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


def _encode_url(url: str) -> str:
    """链接里带中文或空格时，urllib 会报 UnicodeEncodeError，先做百分号编码。"""
    parts = urlsplit(url)
    return urlunsplit((
        parts.scheme,
        parts.netloc,
        quote(parts.path, safe="/%"),
        quote(parts.query, safe="=&%/:?.,-+"),
        quote(parts.fragment, safe=""),
    ))


def fetch(url: str, limit: int = 4000) -> str:
    """返回可读正文；失败返回一句可读的原因。"""
    url = (url or "").strip()
    if not url.startswith(("http://", "https://")):
        return "这不是一个网页链接。"
    url = _encode_url(url)

    request = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept-Language": "zh-CN,zh;q=0.9"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            raw = response.read()
            charset = response.headers.get_content_charset() or "utf-8"
    except urllib.error.HTTPError as exc:
        return f"网页返回 {exc.code}，打不开。"
    except Exception as exc:
        return f"抓取失败：{type(exc).__name__}"

    html = raw.decode(charset, errors="ignore")
    html = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html)
    title_match = re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
    title = re.sub(r"\s+", " ", title_match.group(1)).strip() if title_match else ""

    text = re.sub(r"(?s)<[^>]+>", " ", html)
    text = text.replace("&nbsp;", " ").replace("&#160;", " ")
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return "页面里没抓到正文（可能需要登录或靠脚本加载）。"

    head = f"标题：{title}\n" if title else ""
    return (head + text)[:limit]
