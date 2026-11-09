"""微信端 → 飞书大脑 的本地 HTTP 入口（独立进程，不改任何现有文件）。

- 人设：从《木鱼提示词合集》抽 C 节正文；按请求场景（群聊 / 私聊主人 / 私聊他人）
  追加一行【当前场景】说明，其余规则全由 C 版自己管。
- 回复：调 app.agent.agent.run()
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
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config as app_config  # noqa: E402

PERSONA_MD = Path(os.getenv(
    "WX_PERSONA_MD",
    r"C:\Users\lenovo\Desktop\AI Agent\Qoder CN\木鱼人设\木鱼提示词合集.md",
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
    "要么这一条就把答案写出来，要么直接说查不到、缺什么信息。"
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
        scene = f"这是微信私聊，对方是「{who}」，不是主人。称呼用「你」。"
    return f"{stamp}\n{scene}"


# 清单类问题走原文直出：模型转述一定会把时间地点概括掉（实测 742 字 → 120 字）
# 只要问到活动/二课就给完整清单：模型转述一定丢时间地点，还会说「我看看」然后没了
_ACTIVITY_LIST_RE = re.compile(r"(活动|讲座|二课|第二课堂)")
_ACTIVITY_EXCLUDE_RE = re.compile(r"(报名|推荐|适合|哪个好|值得|哪个更|怎么选|取消)")
_CANTEEN_RE = re.compile(r"(食堂|吃啥|吃什么|好吃|菜品|档口)")
_CANTEEN_EXCLUDE_RE = re.compile(r"(几点开|开门|关门|营业|营业时间|几点关门|价格多少)")
_SCHEDULE_LIST_RE = re.compile(r"(有什么课|有啥课|什么课|课表|课程表|几节课|有哪些课)")
_SCHEDULE_EXCLUDE_RE = re.compile(r"(几点下课|几点上课|什么时候上|哪个老师|谁教|空教室|没课吧|二课)")
_NEEDS_OPINION_RE = re.compile(r"(推荐|适合|哪个好|值得|哪个更|怎么选)")


def _schedule_scope(text: str) -> str:
    if "明天" in text or "明日" in text:
        return "tomorrow"
    if "后天" in text:
        return "day_after_tomorrow"
    if "下周" in text:
        return "next_week"
    if "这周" in text or "本周" in text:
        return "this_week"
    return "today"


def _strip_md(text: str) -> str:
    return re.sub(r"\*\*(.+?)\*\*", r"\1", text or "").strip()


def _money_kind(text: str, chat_id: str | None) -> str | None:
    """数字类问题认准了谁，才敢直出。认不准就交给模型（它会追问）。"""
    if re.search(r"(电费|多少电|还剩多少电)", text):
        return "electric"
    # 「饭卡花了多少」问的是流水不是余额，这条必须在余额前面
    if re.search(r"(花了多少|消费|账单|花销)", text):
        return "consumption"
    if re.search(r"(饭卡|校园卡|一卡通|余额)", text):
        return "balance"
    if re.search(r"(还剩多少|还有多少|剩多少)", text) and chat_id:
        from app.agent import context

        prev = context.last_reply(chat_id) or ""
        if "消费记录" in prev:
            return "consumption"
        if "电费" in prev:
            return "electric"
        if "校园卡" in prev:
            return "balance"
    return None


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


def _direct_answer(text: str, chat_id: str | None = None, is_master: bool = False) -> str | None:
    """能直出就别让模型措辞。返回 None 表示交给 Agent。

    模型转述清单一定会砍掉时间地点（实测活动 742 字 → 120 字、课表 60 字 → 20 字），
    所以这两类是原文直出。
    """
    from app.agent import handlers

    # 要图的请求排在最前面：它和「课表/消费」共用关键词，先拦下来
    image_reply = _image_answer(text, chat_id, is_master)
    if image_reply:
        return image_reply

    if _ACTIVITY_LIST_RE.search(text) and not _NEEDS_OPINION_RE.search(text):
        campus = handlers._wanted_campus(text)
        body = handlers.handle_activities(campus=campus)
        # 2026-10-31 15:00:00 → 15:00，秒在微信里是噪音
        return re.sub(r"(\d{1,2}:\d{2}):00", r"\1", body)

    # 食堂也直出：模型会把「一楼」的店混进「二楼推荐」，价格也丢
    if _CANTEEN_RE.search(text) and not _CANTEEN_EXCLUDE_RE.search(text):
        return _strip_md(handlers.handle_canteen(text))

    if _SCHEDULE_LIST_RE.search(text) and not _SCHEDULE_EXCLUDE_RE.search(text):
        return handlers.handle_schedule(_schedule_scope(text))

    # 金额类必须直出：实测模型会照着正确数字另编一个（81.34→128.50、474.82→0.00）
    kind = _money_kind(text, chat_id)
    if kind == "balance":
        return _strip_md(handlers.handle_balance())
    if kind == "electric":
        return _strip_md(handlers.handle_electric())
    if kind == "consumption":
        return _strip_md(handlers.handle_consumption())

    if re.search(r"(学期报告|学期总结|这学期过得)", text):
        return _strip_md(handlers.handle_semester_report())

    return None


def _multi_reply(text: str, chat_id: str, is_master: bool = False) -> str | None:
    """一句话里问了几件事就分段各答一遍；单问题保持原来的整段回答。"""
    from app.agent import handlers

    parts = handlers.split_multi_intents(text, chat_id)
    if len(parts) < 2:
        return _direct_answer(text, chat_id, is_master) or agent_mod.run(text, chat_id)

    blocks: list[str] = []
    for part in parts:
        piece = _direct_answer(part, chat_id, is_master) or agent_mod.run(part, chat_id)
        if not piece:
            return agent_mod.run(text, chat_id)
        blocks.append(piece.strip())
    return "\n\n".join(blocks) if len(blocks) >= 2 else agent_mod.run(text, chat_id)


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
        if not text:
            self._send(200, {"reply": ""})
            return

        chat_id = f"wx:{payload.get('chatType') or 'private'}:{payload.get('chatId') or ''}"
        _ctx.scene = build_scene(payload)
        try:
            reply = _multi_reply(text, chat_id, payload.get("isMaster") is True)
        except Exception as exc:  # noqa: BLE001
            print(f"[chat] run 失败：{exc!r}", flush=True)
            reply = None
        finally:
            _ctx.scene = ""

        if reply:
            try:
                ctx_mod.remember(chat_id, text, "wechat", reply)
            except Exception as exc:  # noqa: BLE001
                print(f"[chat] 上下文写入失败：{exc!r}", flush=True)
            self._send(200, {"reply": reply})
        else:
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
