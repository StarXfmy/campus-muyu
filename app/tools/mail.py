"""杭电邮箱（网易企业邮托管）：查未读、读摘要。

网易 IMAP 有个坑：必须先发 ID 命令声明客户端身份，
否则服务器返回 "Unsafe Login. Please contact ..."。
"""
from __future__ import annotations

import email
import imaplib
from email.header import decode_header
from typing import Any

from app.config import MAIL_ACCOUNT, MAIL_IMAP_HOST, MAIL_IMAP_PORT, MAIL_PASSWORD


def _decode(value: str | None) -> str:
    if not value:
        return ""
    parts = decode_header(value)
    out = []
    for chunk, charset in parts:
        if isinstance(chunk, bytes):
            try:
                out.append(chunk.decode(charset or "utf-8", errors="replace"))
            except (LookupError, TypeError):
                out.append(chunk.decode("utf-8", errors="replace"))
        else:
            out.append(chunk)
    return "".join(out).strip()


def _connect() -> imaplib.IMAP4_SSL:
    if not MAIL_ACCOUNT or not MAIL_PASSWORD:
        raise RuntimeError("邮箱账号或密码没配置（.env 里的 MAIL_ACCOUNT / MAIL_PASSWORD）")
    conn = imaplib.IMAP4_SSL(MAIL_IMAP_HOST, MAIL_IMAP_PORT, timeout=15)
    # 网易要求先声明客户端，否则 Unsafe Login
    try:
        conn.xatom("ID", '("name" "campus-assistant" "version" "1.0" "vendor" "hdu-student")')
    except Exception:
        pass
    conn.login(MAIL_ACCOUNT, MAIL_PASSWORD)
    return conn


def unread(limit: int = 5) -> dict[str, Any]:
    """返回未读邮件摘要：总数 + 最近几封（主题/发件人/日期）。"""
    conn = _connect()
    try:
        typ, box = conn.select("INBOX")
        if typ != "OK":
            raise RuntimeError(f"打开收件箱失败：{box}")
        total = int(box[0].decode() or 0)
        typ, data = conn.search(None, "UNSEEN")
        ids = data[0].split() if data and data[0] else []
        recent_ids = ids[-limit:] if ids else []

        items = []
        for msg_id in reversed(recent_ids):
            typ, raw = conn.fetch(msg_id, "(RFC822)")
            if typ != "OK" or not raw or not raw[0]:
                continue
            message = email.message_from_bytes(raw[0][1])
            items.append({
                "subject": _decode(message.get("Subject")) or "(无主题)",
                "from": _decode(message.get("From"))[:60],
                "date": (message.get("Date") or "")[:31],
            })
        return {"unread": len(ids), "total": total, "items": items}
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def recent(limit: int = 5) -> dict[str, Any]:
    """最近几封邮件（不论读没读），用于用户想看点内容时。"""
    conn = _connect()
    try:
        typ, box = conn.select("INBOX")
        if typ != "OK":
            raise RuntimeError(f"打开收件箱失败：{box}")
        total = int(box[0].decode() or 0)
        typ, data = conn.search(None, "ALL")
        ids = data[0].split() if data and data[0] else []
        picked = ids[-limit:] if ids else []

        items = []
        for msg_id in reversed(picked):
            typ, raw = conn.fetch(msg_id, "(FLAGS RFC822.HEADER)")
            if typ != "OK" or not raw or not raw[0]:
                continue
            header = email.message_from_bytes(raw[0][1])
            flags = ""
            if isinstance(raw[0], tuple) and len(raw[0]) > 0:
                flags = str(raw[0][0])
            items.append({
                "subject": _decode(header.get("Subject")) or "(无主题)",
                "from": _decode(header.get("From"))[:60],
                "date": (header.get("Date") or "")[:31],
                "unread": "\\Seen" not in flags,
            })
        return {"total": total, "items": items}
    finally:
        try:
            conn.logout()
        except Exception:
            pass

def _build_message(to: str, subject: str, body: str):
    """构造 MIME 消息。单独抽出来，能验证发件人/收件人头部而不用真发。"""
    from email.mime.text import MIMEText
    from email.utils import formataddr

    from app import config

    message = MIMEText(body, "plain", "utf-8")
    message["Subject"] = subject
    message["From"] = formataddr((config.MAIL_SENDER_NAME, MAIL_ACCOUNT))
    message["To"] = to
    return message


def send(to: str, subject: str, body: str) -> dict[str, Any]:
    """发一封纯文本邮件。

    默认不发：.env 里 MAIL_SMTP_ENABLED=1 才会真的连服务器。
    开关没开时直接返回原因，调用方据此告诉用户「功能还没启用」。
    """
    from app import config

    if not config.MAIL_SMTP_ENABLED:
        return {"ok": False, "reason": "发信功能还没启用（需要在 .env 里设 MAIL_SMTP_ENABLED=1）"}
    if not to or "@" not in to:
        return {"ok": False, "reason": "收件地址不对"}
    if not MAIL_ACCOUNT or not MAIL_PASSWORD:
        return {"ok": False, "reason": "邮箱账号或授权码没配置"}

    import smtplib

    message = _build_message(to, subject, body)

    try:
        with smtplib.SMTP_SSL(config.MAIL_SMTP_HOST, config.MAIL_SMTP_PORT, timeout=20) as server:
            server.login(MAIL_ACCOUNT, MAIL_PASSWORD)
            server.sendmail(MAIL_ACCOUNT, [to], message.as_string())
    except Exception as exc:
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}
    return {"ok": True, "reason": "已发送"}
