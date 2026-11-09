"""hduhelp-cli 封装：统一调用、超时、错误归一化。"""
from __future__ import annotations

import json
import shutil
import subprocess
from typing import Any

from app.config import CLI_TIMEOUT, HDUHELP_CLI


class HduhelpError(Exception):
    """CLI 调用失败，message 面向用户可读。"""


def _cli_path() -> str:
    if HDUHELP_CLI and shutil.which(HDUHELP_CLI):
        return HDUHELP_CLI
    found = shutil.which("hduhelp-cli")
    if not found:
        raise HduhelpError("找不到 hduhelp-cli，请先安装或检查 HDUHELP_CLI 配置")
    return found


def run_cli(*args: str, timeout: int | None = None) -> dict[str, Any]:
    cmd = [_cli_path(), *args, "--json"]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout or CLI_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        raise HduhelpError(f"查询超时（{timeout or CLI_TIMEOUT}s），请稍后再试") from None

    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()

    # CLI 出错时会把错误 JSON 写到 stderr，两条流都要尝试解析
    payload: dict[str, Any] | None = None
    for candidate in (stdout, stderr):
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            payload = parsed
            break

    if payload is None:
        detail = (stdout or stderr)[:200] or "未知错误"
        raise HduhelpError(f"CLI 调用失败：{detail}")

    code = payload.get("code")
    if code not in (None, 0, "0"):
        raise HduhelpError(_friendly_error(payload))
    return payload


def _friendly_error(payload: dict[str, Any]) -> str:
    code = payload.get("code")
    message = payload.get("message") or payload.get("msg") or "未知错误"
    mapping = {
        "API_711000005": "成绩查询暂未开放（教务处尚未放行），作业演示请改用课表或校园卡",
        "HTTP_401": "登录已过期，请在电脑上重新运行 hduhelp-cli auth login",
        "HTTP_403": "权限不足，需要重新授权对应 scope",
        "API_50000": "HDUHelp 服务端暂时异常，请稍后再试",
    }
    return mapping.get(code, f"查询失败（{code}）：{message}")


def get_schedule_now() -> dict[str, Any]:
    data = run_cli("academic", "schedule", "now").get("data") or {}
    return {"today": data.get("today", []), "tomorrow": data.get("tomorrow", [])}


def get_card_balance() -> float:
    data = run_cli("campuslife", "card", "balance").get("data") or {}
    return float(data.get("balance", 0))


def list_classrooms() -> list[dict[str, Any]]:
    return run_cli("academic", "classroom").get("data") or []


def list_empty_rooms(building_id: str, weekday: int | None = None, sections: list[int] | None = None) -> list[dict[str, Any]]:
    """直接查指定楼栋在给定时段的空闲教室（服务端判断，不用本地算占用）。"""
    args = ["academic", "classroom", "--building-id", building_id, "--status", "unused", "--limit", "200"]
    if weekday:
        args += ["--weekday", str(weekday)]
    for section in sections or []:
        args += ["--section", str(section)]
    return run_cli(*args).get("data") or []


def get_classroom_usage(location_id: str) -> list[dict[str, Any]]:
    return run_cli("academic", "classroom", "usage", "--location-id", location_id).get("data") or []


def get_grade() -> dict[str, Any]:
    return run_cli("academic", "grade").get("data") or {}


def get_sunrun_overview() -> dict[str, Any]:
    """阳光长跑成绩概览。"""
    return run_cli("subscription", "sunrun", "overview").get("data") or {}


def get_sunrun_detail(start: int = 1, end: int = 10) -> list[dict[str, Any]]:
    return run_cli("subscription", "sunrun", "detail", "--start", str(start), "--end", str(end)).get("data") or []


def get_weather() -> dict[str, Any]:
    """从首页信息流取天气（feed 聚合项之一）。"""
    items = run_cli("feed").get("data", {}).get("items") or []
    for item in items:
        if item.get("name") == "weather" and item.get("available"):
            return (item.get("data") or {}).get("weather") or {}
    return {}


def list_activities(status: int = 8, page: int = 1, page_size: int = 10) -> dict[str, Any]:
    """二课活动列表。status: 5全部 8报名中 9即将开始 10签到中 11待签退 12签退中。"""
    return run_cli(
        "campuslife", "activities", "list",
        "--status", str(status), "--page", str(page), "--page-size", str(page_size),
    )


def apply_activity(activity_id: str) -> dict[str, Any]:
    """报名二课活动（写操作，调用前必须让用户确认）。"""
    return run_cli(
        "campuslife", "activities", "registration", "apply",
        "--activity-id", activity_id, "--yes",
    )


def cancel_activity(activity_id: str, reason: str = "取消报名") -> dict[str, Any]:
    return run_cli(
        "campuslife", "activities", "registration", "cancel",
        "--activity-id", activity_id, "--reason", reason, "--yes",
    )


def knowledge_retrieve(query: str, limit: int = 5) -> list[dict[str, Any]]:
    body = json.dumps({"query": query, "limit": limit}, ensure_ascii=False)
    data = run_cli("knowledge", "retrieve", "--data", body).get("data") or {}
    return data.get("items") or data.get("results") or []
