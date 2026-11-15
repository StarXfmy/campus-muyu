"""微信端 → 飞书大脑 的本地 HTTP 入口（独立进程，不改任何现有文件）。

- 人设：从《木鱼提示词合集》抽 C 节正文；按请求场景（群聊 / 私聊主人 / 私聊他人）
  追加一行【当前场景】说明，其余规则全由 C 版自己管。
- 回复：调 handlers.answer_blocks()，与飞书端同一条主链（规则路由 → 题库兜底 → Agent）。
  微信端只多两件事：[[IMG:路径]] 发图指令、卡片 markdown 清洗成纯文本。
- 上下文：写 conversations 表，chat_id 加 wx: 前缀，与飞书的会话隔离。
- 只监听 127.0.0.1；带 Bearer token 鉴权。

启动：
    python app/http_bridge.py
环境变量：
    WX_BRIDGE_PORT   默认 8790
    WX_BRIDGE_TOKEN  不设则用 dev-token（本机联调）
    WX_PERSONA_MD    木鱼提示词合集的绝对路径（默认按本机约定）
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config as app_config  # noqa: E402

# 人设文档搬家过一次（原来在 Qoder CN\木鱼人设，现在在 Qoder CN\飞书校园助手\木鱼人设）。
# 路径写死成旧位置时读不到文件，load_c_persona 会静默返回空串——
# 结果微信端一直在「没有人设」的状态下说话，/health 里的 personaChars 就是 0。
# 这里按候选顺序取第一个真实存在的，两个位置都认。
_PERSONA_CANDIDATES = [
    Path(r"C:\Users\lenovo\Desktop\AI Agent\Qoder CN\飞书校园助手\木鱼人设\木鱼提示词合集.md"),
    Path(r"C:\Users\lenovo\Desktop\AI Agent\Qoder CN\木鱼人设\木鱼提示词合集.md"),
]
PERSONA_MD = Path(os.getenv("WX_PERSONA_MD") or next(
    (p for p in _PERSONA_CANDIDATES if p.exists()), _PERSONA_CANDIDATES[0]
))
PORT = int(os.getenv("WX_BRIDGE_PORT", "8790"))
TOKEN = os.getenv("WX_BRIDGE_TOKEN", "dev-token")
# 主动推送只发给主人私聊；群聊一律不推
PUSH_TARGET = os.getenv("WX_PUSH_TARGET", "wx:private:wxid_zn3sbb4pla0t12")

_persona_cache: dict = {"mtime": 0.0, "text": ""}
_ctx = threading.local()


def load_c_persona() -> str:
    """从提示词合集里抽 C 节；文件改动自动重载，改了文档不用重启。"""
    try:
        mtime = PERSONA_MD.stat().st_mtime
        if mtime != _persona_cache["mtime"]:
            md = PERSONA_MD.read_text(encoding="utf-8")
            section = re.search(r"##\s*C\.[^\n]*\n(.*?)(?=\n##\s|\Z)", md, re.S)
            body = section.group(1) if section else ""
            block = re.search(r"```text\s*\n(.*?)```", body, re.S)
            _persona_cache["text"] = (block.group(1) if block else body).strip()
            _persona_cache["mtime"] = mtime
    except OSError as exc:
        print(f"[persona] 读取失败：{exc}", flush=True)
    return _persona_cache["text"]


_orig_load_persona = app_config.load_persona

# 只在微信入口追加的排版要求：不改 C 版文档，换入口随时可调
LAYOUT_RULE = (
    "【排版】\n"
    "数字和中文之间留一个空格（例：55 间、3 项、13:30-14:15）；\n"
    "列举 3 项以上时每项单独一行，不要用顿号挤成一句；\n"
    "不同段落之间空一行；列表项不加星号，直接写内容。\n\n"
    "【不许省略】\n"
    "工具查回来的清单要逐条列全，每一项的时间和地点（或金额）都要带上，"
    "不许概括成「包括 A、B、C 等」，不许加「您可以前往……查看更多」这类打发话。"
    "用户问「有什么」就是想看完整清单，压缩等于没查。\n\n"
    "【不许只说不做】\n"
    "禁止说「我看看」「稍等」「给你找找」「马上」这类话然后不给结果。"
    "要么这一条就把答案写出来，要么直接说查不到、缺什么信息。\n\n"
    "【微信特色】\n"
    "别写小作文，但也别变冷淡：闲聊、观点、安慰最多 4 段，段与段之间空一行，\n"
    "语气照旧活泼，该多一句吐槽或关心就多一句，别为了短把人气砍掉；\n"
    "不用「•」「-」这类列表符号，也不写「首先/其次/总之」这种书面腔；\n"
    "但清单类问题（活动、课表、菜、消费明细）照上面的规则逐条列全，不许为了短而省略。"
)


def _patched_load_persona() -> str:
    base = load_c_persona()
    scene = getattr(_ctx, "scene", "")
    tail = LAYOUT_RULE + ("\n\n" + scene if scene else "")
    return f"{base}\n\n{tail}"


# 只影响本进程；飞书 bot 是另一个进程，仍用它自己的 persona.txt
app_config.load_persona = _patched_load_persona

from app.agent import agent as agent_mod  # noqa: E402
from app.agent import context as ctx_mod  # noqa: E402


def build_scene(payload: dict) -> str:
    from datetime import datetime

    now = datetime.now()
    stamp = (
        f"现在是 {now:%Y-%m-%d} 周{'一二三四五六日'[now.weekday()]} {now:%H:%M}。"
        "任何涉及时间、白天晚上、早中晚的判断都只用这个时间，不要自己推测："
        "20 点属于晚上但不是深夜，23 点以后才算夜深。"
    )
    chat_type = str(payload.get("chatType") or "private")
    sender = str(payload.get("senderName") or "").strip()
    title = str(payload.get("chatTitle") or "").strip()
    is_master = payload.get("isMaster") is True
    who = sender or "对方"
    if chat_type == "group":
        where = f"微信群聊「{title}」" if title else "微信群聊"
        scene = f"这是{where}，发言人是「{who}」。按群聊规则：被叫到才回，称呼一律用「你」，永远不叫主人。"
    elif is_master:
        scene = f"这是微信私聊，对方是绑定主人「{who}」。可以称呼「主人」。"
    else:
        scene = (
            f"这是微信私聊，对方是「{who}」，不是主人。"
            "称呼只能用「你」；不许叫主人，也不许透露主人的名字、课表、余额、消费、邮件、行程这些信息。"
        )
    return f"{stamp}\n{scene}"


# 清单类问题走原文直出：模型转述一定会把时间地点概括掉（实测 742 字 → 120 字）
# 这些直出规则已统一收到 handlers.answer() 里，微信端不再自己判领域，
# 只保留下面「要图」和呈现层清洗两件微信端独有的事。


def _strip_md(text: str) -> str:
    """飞书回的是卡片 markdown，微信是纯文本：剥掉星号。

    顺手把秒去掉（15:00:00 → 15:00），微信里那一串秒是噪音。
    """
    out = re.sub(r"\*\*(.+?)\*\*", r"\1", text or "").strip()
    return re.sub(r"(\d{1,2}:\d{2}):00", r"\1", out)


_SCHEDULE_IMAGE_RE = re.compile(r"(课表图|课程表图|课表.*图片|图片.*课表|画个课表|画一下课表|把课表画)")
_EXPENSE_IMAGE_RE = re.compile(r"(消费图|消费.*图表|花钱.*图|消费趋势图)")


def _image_answer(text: str, chat_id: str | None, is_master: bool) -> str | None:
    """要图：只给主人、只在私聊。

    课表和消费都是个人数据，群里发出去等于当众公开。
    """
    want_schedule = _SCHEDULE_IMAGE_RE.search(text)
    want_expense = _EXPENSE_IMAGE_RE.search(text)
    if not (want_schedule or want_expense):
        return None

    is_private = str(chat_id or "").startswith("wx:private:")
    if not is_master or not is_private:
        return "这个本鱼不能说"

    if want_schedule:
        try:
            from app.tools import schedule_image

            path = schedule_image.render_week().resolve()
            return "课表画好了\n[[IMG:%s]]" % path
        except Exception as exc:
            print(f"[img] 课表图生成失败：{exc!r}", flush=True)
            return "课表图没画出来，过会儿再试"

    try:
        from app.tools import chart_image

        path = chart_image.render_expense_from_log(chat_id or "")
    except Exception as exc:
        print(f"[img] 消费图生成失败：{exc!r}", flush=True)
        path = None
    if not path:
        return "本鱼这边还没有你的消费记录，先问一次「这个月花了多少」我再画"
    return "消费图给你\n[[IMG:%s]]" % path


_REMIND_INTENTS = frozenset({"remind", "edit_remind", "cancel_remind", "my_reminders"})

_REMIND_OFF_WECHAT = (
    "设提醒得去飞书那边说一声。\n\n"
    "不是本鱼不肯记——记了在微信里到点也发不出来，与其应下来不办，不如说清楚。"
)


# 微信端不接提醒，追问里再推荐「设个提醒」就是点不动的选项
_REMIND_HINTS = ("设个提醒", "设个明早提醒", "我的提醒有哪些")


def _drop_remind_hints(reply: str) -> str:
    marker = "—— 你可能还想问："
    if marker not in reply:
        return reply
    head, tail = reply.split(marker, 1)
    kept = []
    for line in tail.splitlines():
        item = line.strip().lstrip("·").strip()
        if item and not any(k in item for k in _REMIND_HINTS):
            kept.append(item)
    if not kept:
        return head.rstrip()
    return head + marker + "\n" + "\n".join("· " + item for item in kept)


def _trim_group_reply(reply: str, limit: int = 70) -> str:
    """群里一句话回答压在 70 字内。

    带换行的清单、课表、活动列表不截——截了剩下的半张表更没法看，
    而且群里问这些本来就是要把条目列全。
    """
    if "\n" in reply or len(reply) <= limit:
        return reply
    cut = reply[:limit]
    for mark in ("。", "！", "？", "；", "，"):
        pos = cut.rfind(mark)
        if pos >= int(limit * 0.5):
            return cut[: pos + 1]
    return cut.rstrip() + "…"


def _multi_reply(text: str, chat_id: str, is_master: bool = False) -> str | None:
    """微信入口与飞书走同一条主链：handlers.answer_blocks。

    以前这里自己写了半套：只有活动/食堂/课表/金额/学期报告五类直出，其余全交给
    模型自由发挥，于是飞书能答的「快递在哪里取」（走题库原文直出）微信答不出。
    现在规则路由、题库兜底、纠正层、写操作直出、链接抓取、多问题分段全部共用一份。

    只有「要图」留在微信端：[[IMG:路径]] 是桥接解析的发图指令，飞书用卡片。
    """
    from app.agent import handlers

    image_reply = _image_answer(text, chat_id, is_master)
    if image_reply:
        return image_reply

    try:
        blocks = handlers.answer_blocks(text, chat_id)
    except Exception as exc:  # noqa: BLE001
        print(f"[chat] answer_blocks 失败，回退 agent：{exc!r}", flush=True)
        blocks = []
    reply = _strip_md("\n\n".join(b for b in blocks if b))
    if reply:
        return reply
    return _strip_md(agent_mod.run(text, chat_id) or "")


class Handler(BaseHTTPRequestHandler):
    server_version = "WxBrainBridge/1.0"

    def _send(self, code: int, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        return not TOKEN or self.headers.get("authorization", "") == f"Bearer {TOKEN}"

    def _audit(self, chat_id: str, text: str, started: float, ok: bool) -> None:
        """记一条审计：意图、耗时、成功与否。失败不影响回复。"""
        try:
            from app.agent import router as _router
            from app.ops import audit as audit_mod

            try:
                intent = _router.detect(text, chat_id).name
            except Exception:
                intent = ""
            audit_mod.record(
                chat_id,
                intent,
                latency_ms=int((time.time() - started) * 1000),
                ok=ok,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[audit] 写入失败：{exc!r}", flush=True)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self._send(200, {"ok": True, "personaChars": len(load_c_persona())})
            return
        if self.path.startswith("/push/pending"):
            if not self._authorized():
                self._send(401, {"error": "bad token"})
                return
            from app import push_scheduler

            self._send(200, {"items": push_scheduler.pending()})
            return
        self._send(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path not in ("/chat", "/push/ack"):
            self._send(404, {"error": "not found"})
            return
        if not self._authorized():
            self._send(401, {"error": "bad token"})
            return
        try:
            length = int(self.headers.get("content-length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            self._send(400, {"error": "bad json"})
            return

        if self.path == "/push/ack":
            from app import push_scheduler

            ids = payload.get("ids") or []
            ids = [int(x) for x in ids if isinstance(x, (int, str)) and str(x).isdigit()]
            self._send(200, {"acked": push_scheduler.ack(ids)})
            return

        text = str(payload.get("text") or "").strip()
        # 微信桥有时把「wxid_x:」群前缀一起传过来：知识库检索拿整串切片，
        # 脏前缀会把不相干的问题命中（实测「wxid_x: @木鱼 好久不见」答成食堂）
        text = re.sub(r"^[A-Za-z0-9_\-]{4,}:\s*", "", text).strip()
        t_start = time.time()
        if not text:
            self._send(200, {"reply": ""})
            return

        chat_id = f"wx:{payload.get('chatType') or 'private'}:{payload.get('chatId') or ''}"
        _ctx.scene = build_scene(payload)
        try:
            from app.agent import handlers as _h
            from app.agent import router as _r

            if payload.get("isMaster") is True and _h.is_clear_context(text):
                # 只认主人：这是删数据的指令，混人群里一句「清空历史」不该动到别人
                reply = _h.handle_clear_context(chat_id)
            elif _r.detect(text, chat_id).name in _REMIND_INTENTS:
                # 微信端不接提醒：reminders 到点没有任何链路写进 wx_outbox
                # （push_scheduler.pending() 只读 outbox），答应他就是开一张永远兑不了的空头支票。
                # 提醒本身没删，飞书那边的云端 scheduler 会正常推。
                reply = _REMIND_OFF_WECHAT
            else:
                reply = _multi_reply(text, chat_id, payload.get("isMaster") is True)
        except Exception as exc:  # noqa: BLE001
            print(f"[chat] run 失败：{exc!r}", flush=True)
            reply = None
        finally:
            _ctx.scene = ""

        if reply:
            reply = _drop_remind_hints(reply)
        if reply and (payload.get("chatType") or "") == "group":
            reply = _trim_group_reply(reply)

        if reply:
            try:
                ctx_mod.remember(chat_id, text, "wechat", reply)
                # 飞书那条链还多记一层「30 分钟内的最近两轮摘要」，微信端之前漏了，
                # 于是「那明天呢」这类追问接不上前面的话题
                ctx_mod.record_turn(chat_id, text, reply)
            except Exception as exc:  # noqa: BLE001
                print(f"[chat] 上下文写入失败：{exc!r}", flush=True)
            self._audit(chat_id, text, t_start, True)
            self._send(200, {"reply": reply})
        else:
            self._audit(chat_id, text, t_start, False)
            self._send(200, {"reply": None, "reason": "agent returned empty"})

    def log_message(self, fmt: str, *args) -> None:  # 别刷屏，只留错误
        pass


def main() -> None:
    import logging

    # 不配这个，调度器的 logger.info 全被吞掉，黑窗口里看不到早报有没有发
    logging.basicConfig(level=logging.INFO, format="[%(name)s] %(message)s")

    persona = load_c_persona()
    print(f"[brain] 人设 C 版 {len(persona)} 字，来源 {PERSONA_MD}", flush=True)
    print(f"[brain] 监听 http://127.0.0.1:{PORT}/chat （token={'已设置' if TOKEN else '空'}）", flush=True)

    from app import push_scheduler

    push_scheduler.start(PUSH_TARGET)

    from app.tools import sync_bitable

    sync_bitable.start()

    HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
