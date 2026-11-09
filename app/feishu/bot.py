"""飞书长连接 Bot：不依赖公网回调，本机跑起来就能收消息。

启动前置：
  1. .env 里填好 FEISHU_APP_ID / FEISHU_APP_SECRET
  2. 飞书开放平台已开通机器人能力并订阅 im.message.receive_v1
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

import lark_oapi as lark  # noqa: E402
from lark_oapi.api.im.v1 import (  # noqa: E402
    CreateMessageRequest,
    CreateMessageRequestBody,
    P2ImMessageReceiveV1,
)
from lark_oapi.event.callback.model.p2_card_action_trigger import (  # noqa: E402
    P2CardActionTrigger,
    P2CardActionTriggerResponse,
)

from app.agent import handlers, router  # noqa: E402
from app.config import FEISHU_APP_ID, FEISHU_APP_SECRET  # noqa: E402
from app.agent import pager  # noqa: E402
from app.feishu import cards, scheduler  # noqa: E402
from app.ops import audit, ratelimit  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
audit.setup_logging()
logger = logging.getLogger("campus-bot")

_processed: set[str] = set()
_lock = threading.Lock()

# 长连接看门狗：飞书偶尔静默断线——进程活着、WebSocket 已死、服务端还当连着不重投。
# 2026-10-29 实测断过一次，14 分钟就哑了，只能自己发现。
RECV_STATE = {"at": time.time(), "count": 0}
IDLE_RECONNECT = 20 * 60  # 空闲 20 分钟就主动重建一次
WATCH_INTERVAL = 30


def _touch_event() -> None:
    RECV_STATE["at"] = time.time()
    RECV_STATE["count"] += 1


def _watch_connection(client, stop: threading.Event) -> None:
    """空闲太久就结束进程，交给 systemd 重新拉起。

    SDK 的 start() 内部会无限重连、永不返回，也没对外暴露停止方法，
    从外面没法只断一条连接——直接退出最可靠。
    不能只看「多久没消息」判断死没死（半夜本来就没人说话），
    所以这里只是定期重建：一次重启十几秒，比静默失联几小时划算。
    """
    while not stop.is_set():
        idle = time.time() - RECV_STATE["at"]
        if idle >= IDLE_RECONNECT:
            logger.warning("长连接静默 %d 分钟，重启进程重建", int(idle // 60))
            logging.shutdown()
            os._exit(0)
        if stop.wait(WATCH_INTERVAL):  # 先查再等：刚起来那一刻也看一眼
            return


class _ConnectHints(logging.Handler):
    """把飞书 SDK 的连接日志翻成人话打到黑窗口，免得等握手时以为卡死。"""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
        except Exception:
            return
        hint = ""
        if "connected to wss://" in msg:
            hint = "✓ 已连上飞书，可以发消息了"
        elif "connect failed" in msg:
            hint = "… 这次没连上飞书，约 20 秒后自动重试，窗口不要关"
        elif "trying to reconnect" in msg:
            hint = "… 正在重连飞书…"
        elif "receive message loop exit" in msg:
            hint = "… 飞书连接断了，准备重连"
        if hint:
            try:
                print(hint, flush=True)
            except Exception:
                pass


def _dedup(message_id: str) -> bool:
    """飞书事件可能重投，按 message_id 去重。返回 True 表示重复。"""
    if not message_id:
        return False
    with _lock:
        if message_id in _processed:
            return True
        _processed.add(message_id)
        if len(_processed) > 2000:
            _processed.clear()
    return False


_recent_texts: dict[tuple[str, str], float] = {}

# 飞书对同一条内容有时会推两个不同 message_id 的事件，光靠 ID 去重拦不住
_SAME_TEXT_WINDOW = 20.0


def _same_text_recent(chat_id: str, text: str) -> bool:
    """同一个人 20 秒内发同一句话，只处理第一次。返回 True 表示这次该忽略。"""
    value = (text or "").strip()
    if not value:
        return False
    key = (chat_id, value)
    now = time.time()
    with _lock:
        last = _recent_texts.get(key)
        _recent_texts[key] = now
        if len(_recent_texts) > 500:
            cutoff = now - _SAME_TEXT_WINDOW
            for old_key in [k for k, stamp in _recent_texts.items() if stamp < cutoff]:
                _recent_texts.pop(old_key, None)
    return bool(last and now - last < _SAME_TEXT_WINDOW)


def _extract_text(message) -> str:
    if message is None:
        return ""
    raw = getattr(message, "content", "") or ""
    try:
        content = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    if not isinstance(content, dict):
        return raw

    text = (content.get("text") or "").strip()
    if text:
        return text

    # 富文本 / 分享卡片：文字和链接散在 content 的每一行里
    parts: list[str] = []
    title = (content.get("title") or "").strip()
    if title:
        parts.append(title)
    for line in content.get("content") or []:
        if not isinstance(line, list):
            continue
        for element in line:
            if not isinstance(element, dict):
                continue
            if element.get("href"):
                parts.append(str(element["href"]))
            if element.get("text"):
                parts.append(str(element["text"]))
    return " ".join(parts).strip()


def _send(chat_id: str, text: str, card: dict | None = None) -> None:
    client = lark.Client.builder().app_id(FEISHU_APP_ID).app_secret(FEISHU_APP_SECRET).build()

    def _dispatch(msg_type: str, content: str) -> bool:
        body = (
            CreateMessageRequestBody.builder()
            .receive_id(chat_id)
            .content(content)
            .msg_type(msg_type)
            .build()
        )
        request = CreateMessageRequest.builder().receive_id_type("chat_id").request_body(body).build()
        response = client.im.v1.message.create(request)
        if not response.success():
            logger.error("发送失败 type=%s code=%s msg=%s", msg_type, response.code, response.msg)
            return False
        return True

    if card is not None:
        if _dispatch("interactive", json.dumps(card, ensure_ascii=False)):
            return
        logger.warning("卡片发送失败，降级纯文本")
    _dispatch("text", json.dumps({"text": text}, ensure_ascii=False))


def _send_card_get_id(chat_id: str, card: dict) -> str:
    """发一张卡片并返回 message_id，供之后原地更新。失败返回空串。"""
    client = lark.Client.builder().app_id(FEISHU_APP_ID).app_secret(FEISHU_APP_SECRET).build()
    body = (
        CreateMessageRequestBody.builder()
        .receive_id(chat_id)
        .content(json.dumps(card, ensure_ascii=False))
        .msg_type("interactive")
        .build()
    )
    request = CreateMessageRequest.builder().receive_id_type("chat_id").request_body(body).build()
    response = client.im.v1.message.create(request)
    if not response.success():
        logger.warning("占位卡片发送失败 code=%s msg=%s", response.code, response.msg)
        return ""
    data = response.data
    return (getattr(data, "message_id", "") or "") if data else ""


_PATCH_STATE = {"fails": 0}


def _patch_card(message_id: str, card: dict) -> bool:
    """原地更新机器人自己发的卡片。"""
    from lark_oapi.api.im.v1 import PatchMessageRequest, PatchMessageRequestBody

    client = lark.Client.builder().app_id(FEISHU_APP_ID).app_secret(FEISHU_APP_SECRET).build()
    body = PatchMessageRequestBody.builder().content(json.dumps(card, ensure_ascii=False)).build()
    request = PatchMessageRequest.builder().message_id(message_id).request_body(body).build()
    response = client.im.v1.message.patch(request)
    if not response.success():
        _PATCH_STATE["fails"] += 1
        logger.warning("卡片更新失败 code=%s msg=%s（连续 %s 次）", response.code, response.msg, _PATCH_STATE["fails"])
        return False
    _PATCH_STATE["fails"] = 0
    return True


_PROGRESS_HINTS = (
    (("教室", "自习", "看书", "空着"), ("正在挨个敲门，看哪间教室没人…", "1089 间教室，本鱼跑快点…")),
    (("课", "上课", "早八"), ("正在翻你的课表，顺便数了数这周有几节早八…", "本鱼看看这周哪天最惨…")),
    (("余额", "校园卡", "饭卡", "多少钱"), ("正在帮你数钱…",)),
    (("天气", "下雨", "带伞"), ("正在抬头看天，顺便问了问空气质量…",)),
    (("活动", "讲座", "比赛"), ("正在翻活动列表，帮你挑值得去的…",)),
    (("图书馆", "座位"), ("正在数座位，别急…",)),
    (("跑步", "长跑"), ("正在翻你的跑步记录…",)),
    (("电费",), ("正在看电表…",)),
    (("邮件", "邮箱"), ("正在开邮箱…",)),
    (("消费", "花了"), ("正在翻账单，放心本鱼只算总额…",)),
    (("失物", "丢了"), ("正在翻失物招领…",)),
)
_PROGRESS_DEFAULT = ("正在查数据，几秒钟就好…", "稍等，本鱼在翻资料…", "本鱼查一下，马上来…")

# 等得久时每 2.5 秒换一句，别让人盯着一个静态“查询中”以为卡死
_ROTATE_HINTS = (
    "还在查，马上就好…",
    "快了快了，正在跟学校服务器握手…",
    "数据有点多，本鱼挑重点给你…",
    "别走开，最后一步了…",
)


def _progress_text(text: str, intent=None) -> str:
    import random

    text = text or ""
    try:
        from app.agent.handlers import _needs_deep_agent

        if _needs_deep_agent(text, intent):
            return "这个问题要连查几个数据源，大约 15 到 20 秒，本鱼在跑…"
    except Exception:
        pass
    for keywords, hints in _PROGRESS_HINTS:
        if any(keyword in text for keyword in keywords):
            return random.choice(hints)
    return random.choice(_PROGRESS_DEFAULT)


def _rotate_progress(hint_id: str, stop: threading.Event) -> None:
    """查询久时每 2.5 秒换一句文案，结果出来就停。"""
    for hint in _ROTATE_HINTS:
        if stop.wait(2.5):
            return
        if _PATCH_STATE["fails"] >= 3:
            return
        if not _patch_card(hint_id, cards.build_progress(hint)):
            return


def _stop_rotate(stop: threading.Event, thread) -> None:
    stop.set()
    if thread:
        thread.join(timeout=1.5)


_ACK_STATE = {"warned": False}


def _ack(message_id: str) -> None:
    """给用户消息加个表情，表示「收到了」。没权限就静默跳过，不影响回复。"""
    try:
        from lark_oapi.api.im.v1 import (
            CreateMessageReactionRequest,
            CreateMessageReactionRequestBody,
            Emoji,
        )

        client = lark.Client.builder().app_id(FEISHU_APP_ID).app_secret(FEISHU_APP_SECRET).build()
        body = (
            CreateMessageReactionRequestBody.builder()
            .reaction_type(Emoji.builder().emoji_type("OK").build())
            .build()
        )
        request = CreateMessageReactionRequest.builder().message_id(message_id).request_body(body).build()
        response = client.im.v1.message_reaction.create(request)
        if not response.success() and not _ACK_STATE["warned"]:
            _ACK_STATE["warned"] = True
            logger.warning("表情回执未生效 code=%s msg=%s（检查是否开了「消息表情回复」权限）",
                           response.code, response.msg)
    except Exception:
        logger.debug("表情回执异常", exc_info=True)


def _download_audio(message_id: str, file_key: str) -> bytes:
    """下载飞书语音消息的音频二进制。"""
    from lark_oapi.api.im.v1 import GetMessageResourceRequest

    client = lark.Client.builder().app_id(FEISHU_APP_ID).app_secret(FEISHU_APP_SECRET).build()
    request = (
        GetMessageResourceRequest.builder()
        .message_id(message_id)
        .file_key(file_key)
        .type("file")
        .build()
    )
    response = client.im.v1.message_resource.get(request)
    if not response.success():
        raise RuntimeError(f"下载语音失败 code={response.code} msg={response.msg}")
    return response.file.read() if hasattr(response.file, "read") else response.file


def do_message(data: P2ImMessageReceiveV1) -> None:
    _touch_event()
    event = data.event
    if event is None or event.message is None:
        return
    message = event.message
    message_id = getattr(message, "message_id", "")
    chat_id = getattr(message, "chat_id", "")
    chat_type = getattr(message, "chat_type", "")

    # 重启/断线重连后飞书会把旧消息补推一遍，几十分钟前的别再逐条回
    created_ms = getattr(message, "create_time", "")
    try:
        created_at = datetime.fromtimestamp(int(created_ms) / 1000)
        if (datetime.now() - created_at).total_seconds() > 180:
            logger.info(
                "过期消息忽略 %s（创建于 %s）", message_id, created_at.strftime("%H:%M:%S")
            )
            return
    except (TypeError, ValueError):
        pass

    if _dedup(message_id):
        logger.info("重复事件已忽略 %s", message_id)
        return

    # 称呼按渠道来：飞书一律「主人」，微信只有绑定主人才叫主人
    from app.agent import tone

    tone.set_context("feishu", chat_id)

    # 限流放在去重之后：重复事件不该占额度，刷屏的才占
    if not ratelimit.check(chat_id):
        logger.info("限流拦截 %s", chat_id)
        _send(chat_id, ratelimit.REPLY)
        return

    text = _extract_text(message)
    if _same_text_recent(chat_id, text):
        logger.info("同内容重复已忽略 [%s] %s", chat_id, text[:40])
        return
    logger.info("收到 [%s/%s] %s", chat_type, getattr(message, "message_type", ""), text[:100])

    if (getattr(message, "message_type", "") or "") == "audio":
        # 群聊语音不带 @ 信息，分不清是不是在叫机器人，只在私聊处理
        if chat_type == "group":
            return
        threading.Thread(target=_ack, args=(message_id,), daemon=True).start()
        # 下载 + 转写要好几秒，放后台线程，别卡住长连接
        threading.Thread(
            target=_handle_audio,
            args=(getattr(message, "content", "") or "", message_id, chat_id),
            daemon=True,
        ).start()
        return

    if (getattr(message, "message_type", "") or "") != "text":
        if chat_type != "group":
            _send(chat_id, "目前只认文字和语音消息。")
        return

    # 群聊里只在被 @ 时响应，避免刷屏
    if chat_type == "group":
        mentions = getattr(message, "mentions", None) or []
        if not mentions:
            return
        for mention in mentions:
            # 飞书消息体里的 @ 是 @_user_1 这种 key，不是显示名
            key = getattr(mention, "key", "")
            name = getattr(mention, "name", "")
            if key:
                text = text.replace(key, "")
            if name:
                text = text.replace(f"@{name}", "")
        text = text.strip()

    if not text:
        return

    # 改课表/记忆/人设只给私聊：群里谁都能喊一嗓子就乱套了
    if chat_type == "group" and "修改木鱼" in text:
        _send(chat_id, "这个本鱼改不了。")
        return

    threading.Thread(target=_ack, args=(message_id,), daemon=True).start()
    started = time.time()
    try:
        _answer_with_progress(chat_id, text)
        logger.info("已回复 %s", chat_id)
        _audit(chat_id, text, started, True)
    except Exception as exc:
        logger.exception("处理失败")
        _send(chat_id, f"出了点问题：{type(exc).__name__}")
        _audit(chat_id, text, started, False)


def _audit(chat_id: str, text: str, started: float, ok: bool) -> None:
    """记一条审计。只记元数据，不记原文。失败不影响回复。"""
    try:
        name = router.detect(text, chat_id).name
    except Exception:
        name = ""
    audit.record(
        chat_id,
        name,
        latency_ms=int((time.time() - started) * 1000),
        ok=ok,
        degraded=_PATCH_STATE["fails"] >= 3,
    )


def _finish_card(chat_id: str, hint_id: str, body: str, card: dict) -> None:
    try:
        from app.tools import intent_logs

        intent_logs.record_answer(chat_id, body)
    except Exception:
        logger.debug("记录回答失败", exc_info=True)
    """优先原地替换占位卡片；替换不了就单独发一条。"""
    if hint_id and _patch_card(hint_id, card):
        return
    _send(chat_id, body, card)


def _finish_error(chat_id: str, hint_id: str, message: str) -> None:
    card = cards.build_error(message)
    if hint_id and _patch_card(hint_id, card):
        return
    _send(chat_id, message, card)


def _upload_image(path) -> str:
    """上传图片到飞书，返回 image_key。失败返回空串。"""
    from lark_oapi.api.im.v1 import CreateImageRequest, CreateImageRequestBody

    client = lark.Client.builder().app_id(FEISHU_APP_ID).app_secret(FEISHU_APP_SECRET).build()
    try:
        with open(path, "rb") as handle:
            body = CreateImageRequestBody.builder().image_type("message").image(handle).build()
            request = CreateImageRequest.builder().request_body(body).build()
            response = client.im.v1.image.create(request)
    except OSError:
        logger.exception("读取图片失败 %s", path)
        return ""
    if not response.success():
        logger.error("图片上传失败 code=%s msg=%s", response.code, response.msg)
        return ""
    return (response.data.image_key if response.data else "") or ""


def _send_image(chat_id: str, image_key: str) -> bool:
    client = lark.Client.builder().app_id(FEISHU_APP_ID).app_secret(FEISHU_APP_SECRET).build()
    body = (
        CreateMessageRequestBody.builder()
        .receive_id(chat_id)
        .content(json.dumps({"image_key": image_key}))
        .msg_type("image")
        .build()
    )
    request = CreateMessageRequest.builder().receive_id_type("chat_id").request_body(body).build()
    response = client.im.v1.message.create(request)
    if not response.success():
        logger.error("图片发送失败 code=%s msg=%s", response.code, response.msg)
        return False
    return True


def _send_activity_confirm(chat_id: str, text: str, hint_id: str) -> None:
    from app.agent import handlers

    activity = handlers.match_activity(text, chat_id)
    if not activity:
        # 没点名具体活动（比如「我要报名绍兴的活动」）：先把能报的列出来让用户挑
        keyword = re.sub(r"(我)?(想|要|帮我)?(报名|参加|报个名)", "", text or "").strip(" ，,。的了")
        head = (
            f"**没找到叫「{keyword}」的报名中活动**，所以没给你出确认卡，也没提交任何报名。\n"
            if keyword else
            "**没听出你要报哪个**，这次没有提交任何报名。\n"
        )
        cached = handlers.recent_activities(chat_id)
        if cached:
            names = "\n".join(f"{i + 1}. {name}" for i, (_, name) in enumerate(cached))
            body = f"{head}\n刚才列过这几个：\n{names}\n\n说「报名第 2 个」或者把活动名发我。"
            logger.info("报名未匹配，用上一轮列表提示：%s", keyword or text)
            _finish_card(chat_id, hint_id, body, cards.build(body, "activities"))
            return
        listing = handlers.handle_activities(text, chat_id=chat_id)
        head = (
            f"**没找到叫「{keyword}」的报名中活动**，所以没给你出确认卡，也没提交任何报名。\n"
            if keyword else
            "**没听出你要报哪个**，这次没有提交任何报名。\n"
        )
        body = f"{head}\n现在能报的有这些：\n\n{listing}\n\n想报哪个，把活动名原样发给本鱼。"
        logger.info("报名未匹配到活动：%s", keyword or text)
        _finish_card(chat_id, hint_id, body, cards.build(body, "activities"))
        return
    detail = handlers.format_activity_confirm(activity)
    card = cards.build_activity_confirm(
        detail,
        str(activity.get("activityID") or ""),
        str(activity.get("activityName") or ""),
    )
    if hint_id and _patch_card(hint_id, card):
        return
    _send(chat_id, detail, card)


def _send_kb_preview(chat_id: str, text: str, hint_id: str) -> None:
    """预览卡：抽不出候选就退化成纯文本，不给假按钮。"""
    from app.tools import kb

    staged = kb.take_for_chat(chat_id)
    if not staged:
        card = cards.build(text, "kb_add")
        _finish_card(chat_id, hint_id, text, card)
        return
    token, candidate = staged
    title = candidate.get("title") or "这条"
    card = cards.build_kb_preview(text, token)
    _finish_card(chat_id, hint_id, f"准备收录：{title}", card)


def _send_mail_confirm(chat_id: str, text: str, hint_id: str) -> None:
    """发信确认卡。没拿到草稿（比如收件人缺失）就只发文本。"""
    from app.agent import handlers as _handlers

    staged = _handlers.take_mail_draft(chat_id)
    if not staged:
        _finish_card(chat_id, hint_id, text, cards.build(text, "mail_send"))
        return
    token, draft = staged
    _finish_card(chat_id, hint_id, text, cards.build_mail_confirm(text, token))


def _do_mail_send(chat_id: str, token: str, message_id: str) -> None:
    """真正发信。成功后 patch 卡片，失败说明原因。"""
    from app.agent import handlers as _handlers
    from app.tools import mail as mail_tool

    draft = _handlers.get_mail_draft(token)
    if not draft:
        if message_id:
            _patch_card(message_id, cards.build("这封邮件草稿过期了，重新说一次要发什么。", "mail_send"))
        return

    result = mail_tool.send(draft["to"], draft["subject"], draft["body"])
    _handlers.drop_mail_draft(token)

    if result.get("ok"):
        logger.info("邮件已发送 -> %s", draft["to"])
        text = f"**已发送**\n收件人：{draft['to']}\n主题：{draft['subject']}"
    else:
        logger.warning("邮件发送失败 -> %s: %s", draft["to"], result.get("reason"))
        text = f"**没发出去**：{result.get('reason')}\n收件人：{draft['to']}"
    if message_id:
        _patch_card(message_id, cards.build(text, "mail_send"))


def _apply_error_text(message: str) -> str:
    """报名失败分类。查不到列表和提交失败是两码事，不能都说成「去授权」。"""
    text = message or ""
    if "40920" in text or "服务授权" in text:
        return (
            "**报名没提交成功**：杭电助手的「二课活动」服务授权没开。\n"
            "打开「杭电助手」→ 我的/设置 → 服务授权 → 二课活动 → 同意，然后再点一次确认。\n"
            "（能查到活动但报名失败，通常就是这一处）"
        )
    if "MISSING_SCOPE" in text or "权限" in text or "scope" in text.lower():
        return (
            "**报名没提交成功**：命令行这边缺报名写权限（campuslife:activity:write）。\n"
            "查活动不受影响；要报名得重新授权一次，把写权限勾上。"
        )
    if "已报名" in text or "重复报名" in text or "already" in text.lower():
        return "这个活动你已经报过名了，不用重复点。"
    if "截止" in text or "结束" in text or "status" in text.lower():
        return f"**报名没提交成功**：活动可能已截止或不在报名中。\n原始信息：{text[:60]}"
    return f"**报名没提交成功**：{text[:100]}"


def _do_activity_apply(chat_id: str, activity_id: str, name: str, message_id: str) -> None:
    from app.tools.hduhelp import HduhelpError, apply_activity

    text = ""
    card = None
    try:
        result = apply_activity(activity_id)
    except HduhelpError as exc:
        logger.warning("报名被拒 %s: %s", activity_id, exc)
        text = _apply_error_text(str(exc))
        # 「已经报过名」不是错误，是状态，别用红卡片吓人
        already = "已经报过名" in text
        card = cards.build(text, "activities") if already else cards.build_error(text)
    except Exception as exc:
        logger.exception("报名异常 %s", activity_id)
        text = f"报名出错：{type(exc).__name__}"
        card = cards.build_error(text)
    else:
        data = result.get("data") or {}
        if data.get("registered") or data.get("success") or data.get("verified"):
            logger.info("报名成功 %s %s", activity_id, name)
            text = f"报名成功：{name}\n\n临时去不了的话，去「青春杭电」→ 第二课堂取消。"
            card = cards.build(text, "activities")
        else:
            text = f"报名没成功：{result.get('msg') or '接口没返回成功状态'}"
            card = cards.build_error(text)

    if message_id and _patch_card(message_id, card):
        return
    _send(chat_id, text, card)


def _send_expense_chart(chat_id: str, hint_id: str, text: str = "") -> None:
    from app.agent import handlers
    from app.feishu import cards as _cards
    from app.tools import chart_image

    # 具体日期范围优先，其次「上个月」「这个月」，都没有就默认最近 7 天
    span = handlers.parse_date_range(text) or handlers.parse_month_range(text)
    start, end = span if span else (None, None)
    # 先查一次消费，顺带把明细落库，图才有数据
    reply = handlers.handle_consumption(chat_id, start=start, end=end)
    path = chart_image.render_expense_from_log(chat_id, start=start, end=end)
    if not path:
        _finish_card(chat_id, hint_id, reply, _cards.build(reply, "consumption"))
        return
    image_key = _upload_image(path)
    if not image_key:
        _finish_card(chat_id, hint_id, reply, _cards.build(reply, "consumption"))
        return
    _finish_card(chat_id, hint_id, reply, _cards.build(reply, "consumption"))
    _send_image(chat_id, image_key)


def _send_schedule_image(chat_id: str, hint_id: str) -> None:
    from app.tools import schedule_image

    try:
        path = schedule_image.render_week()
    except Exception as exc:
        logger.exception("生成课表图片失败")
        _finish_error(chat_id, hint_id, f"画课表失败：{type(exc).__name__}")
        return

    image_key = _upload_image(path)
    if not image_key:
        _finish_error(chat_id, hint_id, "图片上传失败了，先用文字版：问「这周有啥课」")
        return

    text = "本周课表画好了，看下图 👇"
    if hint_id:
        _patch_card(hint_id, cards.build(text, "schedule_image"))
    if not _send_image(chat_id, image_key):
        _finish_error(chat_id, hint_id, "图片没能发出去，稍后再试。")


def _push_more_news(chat_id: str, category: str) -> None:
    from app.agent import handlers

    try:
        text = handlers.news_more(chat_id, category)
    except Exception:
        logger.exception("补发新闻失败")
        return
    _send(chat_id, text)


def _send_news_card(chat_id: str, text: str, hint_id: str) -> None:
    from app.agent import handlers
    from app.feishu import cards as _cards
    from app.tools import news

    data = handlers.news_data(text, chat_id)
    if data["mode"] == "hot":
        if not data["items"]:
            _finish_error(chat_id, hint_id, "热搜暂时拉不到，过一会儿再问本鱼。")
            return
        body = "\n".join(["【百度热搜】", *[it["t"] for it in data["items"]]])
        _finish_card(chat_id, hint_id, body, _cards.build(body, "news"))
        return
    if data["mode"] in ("official", "local", "web"):
        if not data["items"]:
            _finish_error(chat_id, hint_id, f"没找到「{data['keyword']}」相关的新闻。")
            return
        label = {
            "official": f"{data['keyword']} · 官方发布",
            "local": f"{data['keyword']} · 站内新闻",
            "web": f"{data['keyword']} · 联网搜索（非官方）",
        }[data["mode"]]
        lines = [f"【{label}】"]
        for it in data["items"]:
            mark = f"（{it['s']}）" if data["mode"] == "web" and it.get("s") else ""
            lines.append(f"· {it['t']}{mark}")
            if it.get("l"):
                lines.append(f"  {it['l']}")
            if it.get("d"):
                lines.append(f"  {it['d'][:80]}")
        body = "\n".join(lines)
        _finish_card(chat_id, hint_id, body, _cards.build(body, "news"))
        return
    if not data["groups"] and not data["hot"] and not data["campus"]:
        _finish_error(chat_id, hint_id, "新闻源暂时拉不到，过一会儿再问本鱼。")
        return
    fallback: list[str] = []
    if data["campus"]:
        fallback += ["【校内通知】", *data["campus"], ""]
    if data["hot"]:
        fallback += ["【热搜】", *data["hot"], ""]
    fallback += news.format_grouped(data["groups"], per_category=2)
    _finish_card(
        chat_id,
        hint_id,
        "\n".join(fallback).strip(),
        _cards.build_news(data["groups"], data["hot"], data["campus"]),
    )


def _answer_with_progress(chat_id: str, text: str, intent=None) -> None:
    """先发占位卡片，查完原地替换成结果。

    之前用定时器延迟发卡片，结果查询刚好在 1.2 秒后完成时，
    结果会比占位卡片先发出去，形成「结果在上、查询中在下」的错位。
    改成同步发：先拿到 card 的 message_id，再查询，顺序不会乱。
    """
    if intent is None:
        intent = router.detect(text, chat_id)
    try:
        from app.tools import intent_logs

        route = "rule" if intent.name != router.INTENT_UNKNOWN else "agent"
        intent_logs.record(chat_id, text, intent.name, route)
    except Exception:
        logger.debug("记录意图失败", exc_info=True)
    if intent.name == "help":
        _send(chat_id, handlers.HELP_TEXT, cards.build_help_menu())
        return

    # 卡片更新连续失败（多半是权限没开）就别再发占位卡片了，免得每条消息留一张垃圾
    hint_id = ""
    if _PATCH_STATE["fails"] < 3:
        hint_id = _send_card_get_id(chat_id, cards.build_progress(_progress_text(text, intent)))

    stop_rotate = threading.Event()
    rotator = None
    if hint_id:
        rotator = threading.Thread(
            target=_rotate_progress, args=(hint_id, stop_rotate), daemon=True
        )
        rotator.start()

    # 一句话问了几件事就分段走文本回答，别因为第一件事是发图/报名就把其余吞掉
    multi = len(handlers.split_multi_intents(text, chat_id)) >= 2

    if not multi and intent.name == router.INTENT_SCHEDULE_IMAGE:
        _stop_rotate(stop_rotate, rotator)
        _send_schedule_image(chat_id, hint_id)
        return
    if not multi and intent.name == router.INTENT_EXPENSE_CHART:
        _stop_rotate(stop_rotate, rotator)
        _send_expense_chart(chat_id, hint_id, text)
        return
    if not multi and intent.name == router.INTENT_ACTIVITY_SIGNUP:
        _stop_rotate(stop_rotate, rotator)
        _send_activity_confirm(chat_id, text, hint_id)
        return
    if not multi and intent.name == router.INTENT_MAIL_SEND:
        _stop_rotate(stop_rotate, rotator)
        try:
            preview = handlers.answer(text, chat_id, intent=intent)
        except Exception:
            logger.exception("邮件草稿生成失败")
            preview = "这封邮件没准备好，稍后再试一次。"
        _send_mail_confirm(chat_id, preview, hint_id)
        return
    if not multi and intent.name == router.INTENT_NEWS:
        _stop_rotate(stop_rotate, rotator)
        _send_news_card(chat_id, text, hint_id)
        return
    if (
        not multi
        and intent.name == router.INTENT_DAILY_REPORT_SET
        and any(k in text for k in ("现在", "马上", "立即", "来一份", "发一份"))
        and not any(k in text for k in ("关掉", "关闭", "取消", "别发", "停止", "停掉"))
    ):
        _stop_rotate(stop_rotate, rotator)
        report = handlers.build_morning_report(chat_id)
        _finish_card(chat_id, hint_id, report, cards.build(report, "daily_report_set"))
        part = handlers.morning_news_card(chat_id)
        if part:
            news_card, news_fallback = part
            _send(chat_id, news_fallback, news_card)
        return
    if not multi and intent.name == router.INTENT_KB_ADD:
        _stop_rotate(stop_rotate, rotator)
        # 必须先走 handler 做抽取和 stage，直接拿原始消息只会回一句网址
        try:
            preview = handlers.answer(text, chat_id, intent=intent)
        except Exception:
            logger.exception("收录预览失败")
            preview = "收录没跑起来，稍后再试一次。"
        _send_kb_preview(chat_id, preview, hint_id)
        return

    try:
        blocks = handlers.answer_blocks(text, chat_id, intent=intent)
        reply = "\n\n".join(blocks)
    except Exception:
        logger.exception("查询失败")
        _stop_rotate(stop_rotate, rotator)
        message = "查询出错了，稍后再试…"
        if hint_id and _patch_card(hint_id, cards.build_error(message)):
            return
        raise
    finally:
        _stop_rotate(stop_rotate, rotator)

    # 多问题才分块渲染；单问题保持原来的整段卡片，别把一句话拆碎
    card = cards.build_blocks(blocks, intent.name) if len(blocks) > 1 else cards.build(reply, intent.name)
    token = pager.take_last(chat_id)
    if token:
        info = pager.get(token)
        if info:
            card = cards.build_paged(reply, token, 0, len(info["blocks"]) > pager.PAGE_SIZE)
    # 知识库命中和兜底回答最容易出错，给一个「答得不对」的入口
    if intent.name in (
        router.INTENT_KNOWLEDGE,
        router.INTENT_UNKNOWN,
        router.INTENT_OFFICE,
        router.INTENT_COLLEGE,
        router.INTENT_LOSTFOUND,
    ):
        card = cards.with_feedback(card, text)
    _finish_card(chat_id, hint_id, reply, card)
    try:
        from app.agent import context

        context.record_turn(chat_id, text, reply)
    except Exception:
        logger.debug("记录短期上下文失败", exc_info=True)


def _reanswer(chat_id: str, question: str, message_id: str) -> None:
    """点「答得不对」后的重答：记样本 → 用保守通道重想一遍 → 原地换卡。"""
    try:
        from app.tools import intent_logs

        intent_logs.record_answer_wrong(chat_id, question)
    except Exception:
        logger.debug("记录误答样本失败", exc_info=True)

    reply = ""
    try:
        from app.agent import agent as legacy_agent

        reply = legacy_agent.run(
            question
            + "\n\n（用户说上一次的回答不对。请重新判断这个问题；"
            "不确定就直说不知道，不要猜，也不要编。）",
            chat_id,
        ) or ""
    except Exception:
        logger.exception("误答重试失败")

    if not reply.strip():
        reply = "本鱼重新想了一遍，还是没把握。换个说法再问一次？"
    card = cards.build(reply, "unknown")
    if not _patch_card(message_id, card):
        _send(chat_id, reply, card)
    logger.info("误答重答完成 %s", question[:40])


def _handle_action_async(chat_id: str, text: str, key: str) -> None:
    """按钮点击的实际处理，放后台线程跑。

    飞书要求回调 3 秒内响应，而查数据 + LLM 通常 5-10 秒，
    直接同步处理会弹超时警告。
    """
    try:
        _answer_with_progress(chat_id, text)
        logger.info("按钮 %s → %s 已回复", key, text)
    except Exception:
        logger.exception("按钮异步处理失败 %s", key)
        _send(chat_id, "出了点问题，稍后再试。")


def do_card_action(data: P2CardActionTrigger) -> P2CardActionTriggerResponse:
    """处理卡片按钮点击：先把 toast 吐回去，查询丢后台。"""
    _touch_event()
    try:
        event = data.event
        action = getattr(event, "action", None)
        value = getattr(action, "value", None) or {}
        if not isinstance(value, dict):
            value = {}
        key = value.get("action", "")
        context = getattr(event, "context", None)
        chat_id = getattr(context, "open_chat_id", "") or ""

        message_id = getattr(context, "open_message_id", "") or ""

        if key == "news_like":
            category = str(value.get("category") or "")
            if not category or not chat_id:
                return P2CardActionTriggerResponse({"toast": {"type": "warning", "content": "参数缺失"}})
            from app.tools import news as news_store

            news_store.bump_like(chat_id, category)
            threading.Thread(target=_push_more_news, args=(chat_id, category), daemon=True).start()
            return P2CardActionTriggerResponse(
                {"toast": {"type": "success", "content": f"马上给你多来几条{category}"}}
            )
        if key == "answer_wrong":
            question = str(value.get("question") or "")
            if not question or not chat_id:
                return P2CardActionTriggerResponse(
                    {"toast": {"type": "warning", "content": "参数缺失"}}
                )
            threading.Thread(
                target=_reanswer, args=(chat_id, question, message_id), daemon=True
            ).start()
            return P2CardActionTriggerResponse(
                {"toast": {"type": "info", "content": "收到，本鱼重新想一遍"}}
            )

        if key == "act_apply":
            activity_id = str(value.get("activity_id") or "")
            name = str(value.get("activity_name") or "")
            if not activity_id or not chat_id:
                return P2CardActionTriggerResponse({"toast": {"type": "error", "content": "参数缺失"}})
            threading.Thread(
                target=_do_activity_apply,
                args=(chat_id, activity_id, name, message_id),
                daemon=True,
            ).start()
            return P2CardActionTriggerResponse({"toast": {"type": "info", "content": "⏳ 正在报名…"}})

        if key in ("mail_send", "mail_abort"):
            from app.agent import handlers as _handlers

            token = str(value.get("token") or "")
            if key == "mail_abort":
                _handlers.drop_mail_draft(token)
                if message_id:
                    threading.Thread(
                        target=lambda: _patch_card(message_id, cards.build("好的，这封没发。", "mail_send")),
                        daemon=True,
                    ).start()
                return P2CardActionTriggerResponse({"toast": {"type": "info", "content": "已取消"}})

            if not _handlers.get_mail_draft(token):
                return P2CardActionTriggerResponse(
                    {"toast": {"type": "warning", "content": "这封过期了，重新说一次要发什么"}}
                )
            threading.Thread(target=_do_mail_send, args=(chat_id, token, message_id), daemon=True).start()
            return P2CardActionTriggerResponse({"toast": {"type": "info", "content": "正在发送…"}})

        if key in ("kb_save", "kb_drop"):
            from app.tools import kb as kb_store

            token = str(value.get("token") or "")
            candidate = kb_store.get(token)
            if not candidate:
                return P2CardActionTriggerResponse(
                    {"toast": {"type": "warning", "content": "这条预览过期了，重新发一次「收录 网址」"}}
                )

            if key == "kb_drop":
                kb_store.drop(token)
                if message_id:
                    threading.Thread(
                        target=lambda: _patch_card(message_id, cards.build("好的，这条不收。", "kb_add")),
                        daemon=True,
                    ).start()
                return P2CardActionTriggerResponse({"toast": {"type": "info", "content": "已丢弃"}})

            result = kb_store.save(candidate, verified=True)
            kb_store.drop(token)
            if result["ok"]:
                done = (
                    f"**已入库**\n{candidate.get('title')}\n\n"
                    f"搜索关键词：{candidate.get('keywords') or '（无）'}\n"
                    f"原文：{candidate.get('source')}"
                )
                toast = "已入库"
            else:
                done = f"**没入库**：{result['reason']}"
                toast = "没入库"
            if message_id:
                threading.Thread(
                    target=lambda: _patch_card(message_id, cards.build(done, "kb_add")),
                    daemon=True,
                ).start()
            return P2CardActionTriggerResponse({"toast": {"type": "info", "content": toast}})

        if key == "page_go":
            token = str(value.get("token") or "")
            try:
                page = int(value.get("page") or 0)
            except (TypeError, ValueError):
                page = 0
            rendered = pager.page_text(token, page)
            if rendered is None:
                return P2CardActionTriggerResponse(
                    {"toast": {"type": "warning", "content": "这一页过期了，重新问一次吧"}}
                )
            text, has_next, total = rendered
            if message_id:
                threading.Thread(
                    target=lambda: _patch_card(
                        message_id, cards.build_paged(text, token, page, has_next)
                    ),
                    daemon=True,
                ).start()
            return P2CardActionTriggerResponse(
                {"toast": {"type": "info", "content": f"第 {page + 1} 页 / 共 {total} 条"}}
            )

        if key == "act_abort":
            if message_id:
                threading.Thread(
                    target=lambda: _patch_card(message_id, cards.build("好的，这次没报名。", "activities")),
                    daemon=True,
                ).start()
            return P2CardActionTriggerResponse({"toast": {"type": "info", "content": "已取消"}})

        mapping = cards.BUTTON_ACTIONS.get(key)
        if not mapping:
            return P2CardActionTriggerResponse({"toast": {"type": "warning", "content": "未知按钮"}})
        text, _ = mapping
        if not chat_id:
            return P2CardActionTriggerResponse({"toast": {"type": "error", "content": "拿不到会话 ID"}})

        threading.Thread(target=_handle_action_async, args=(chat_id, text, key), daemon=True).start()
        return P2CardActionTriggerResponse({"toast": {"type": "info", "content": "⏳ 收到，结果马上来"}})
    except Exception:
        logger.exception("按钮回调失败")
        return P2CardActionTriggerResponse({"toast": {"type": "error", "content": "回调异常"}})


def _handle_audio(raw: str, message_id: str, chat_id: str) -> None:
    """语音消息：下载 → 转写 → 走文字对话流程。"""
    from app.agent import tone

    tone.set_context("feishu", chat_id)  # 后台线程不会继承主线程的 contextvar
    hint_id = ""
    try:
        if _PATCH_STATE["fails"] < 3:
            hint_id = _send_card_get_id(chat_id, cards.build_progress("🎧 收到语音，正在转写…"))
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            _finish_error(chat_id, hint_id, "语音内容解析失败。")
            return
        file_key = payload.get("file_key") or ""
        if not file_key:
            _finish_error(chat_id, hint_id, f"语音里没有 file_key（content={raw[:100]}）。")
            return

        audio = _download_audio(message_id, file_key)

        from app.tools import voice

        try:
            text = voice.transcribe(audio)
        except Exception as exc:
            logger.exception("语音转写失败")
            _finish_error(chat_id, hint_id, f"语音识别失败：{type(exc).__name__}。先打字吧。")
            return

        if not text:
            _finish_error(chat_id, hint_id, "没听清，再说一遍？")
            return

        logger.info("语音转文字：%s", text)
        intent = router.detect(text, chat_id)
        if intent.name == "help":
            _finish_card(chat_id, hint_id, f"🎤 听到：{text}\n\n{handlers.HELP_TEXT}",
                         cards.build_help_menu(note=f"🎤 听到：{text}"))
            return
        reply = handlers.answer(text, chat_id, intent=intent)
        # 卡片优先发送时 text 参数会被丢弃，听写内容要拼进卡片正文
        body = f"🎤 听到：{text}\n\n{reply}"
        _finish_card(chat_id, hint_id, body, cards.build(body, intent.name))
    except Exception as exc:
        logger.exception("处理语音失败")
        _finish_error(chat_id, hint_id, f"语音处理失败：{type(exc).__name__}")


def main() -> int:
    from app.console import setup

    setup()
    if not FEISHU_APP_ID or not FEISHU_APP_SECRET:
        print("缺少 FEISHU_APP_ID / FEISHU_APP_SECRET。")
        print(f"请在 {ROOT / '.env'} 里填写后重试（可照 .env.example 复制）。")
        return 1

    handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(do_message)
        .register_p2_card_action_trigger(do_card_action)
        .build()
    )
    client = lark.ws.Client(FEISHU_APP_ID, FEISHU_APP_SECRET, event_handler=handler, log_level=lark.LogLevel.INFO)

    print("=" * 56)
    print("校园助手已启动（长连接模式）")
    print("正在连接飞书…首次可能要 10-60 秒，连上会提示「已连上飞书」")
    print("连上之后再去飞书给机器人发「你好」")
    print("按 Ctrl+C 停止 —— 窗口一直开着是正常的")
    print("=" * 56)

    logging.getLogger("Lark").addHandler(_ConnectHints())

    # 提醒调度：每 30 秒查一次有没有到点的提醒
    scheduler.start(lambda cid, msg, card=None: _send(cid, msg, card))

    # 和微信端共用一张飞书多维表格：记忆/人设/课表改动互通
    from app.tools import sync_bitable

    sync_bitable.start()

    # 后台预热语音模型，别让第一条语音干等
    from app.tools import voice

    voice.warmup()

    # 长连接主循环：断了就重建，不让黑窗口"假活着"
    attempt = 0
    while True:
        attempt += 1
        stop_watch = threading.Event()
        RECV_STATE["at"] = time.time()
        threading.Thread(
            target=_watch_connection, args=(client, stop_watch), daemon=True, name="conn-watchdog"
        ).start()
        try:
            client.start()
        except KeyboardInterrupt:
            stop_watch.set()
            logger.info("收到 Ctrl+C，退出")
            raise
        except Exception:
            logger.exception("长连接异常")
        finally:
            stop_watch.set()

        wait = min(30, 2 * attempt)
        logger.warning("长连接已断开，%d 秒后第 %d 次重连", wait, attempt)
        time.sleep(wait)
        try:
            client = lark.ws.Client(
                FEISHU_APP_ID, FEISHU_APP_SECRET, event_handler=handler, log_level=lark.LogLevel.INFO
            )
        except Exception:
            logger.exception("重建长连接失败，稍后继续重试")
            continue


if __name__ == "__main__":
    raise SystemExit(main())
