"""一键重新授权：补齐二课活动读+写、信息流、阳光长跑等权限。

双击 ★3 重新授权.lnk 运行，或在命令行 py -3 scripts/reauth.py
浏览器打开输出的链接 → 用学号登录 → 点同意，完成后回来测。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import HDUHELP_CLI  # noqa: E402

SCOPES = ",".join([
    "academic:grade:read",
    "academic:schedule:read",
    "academic:exam:read",          # 考试安排
    "campuslife:campuscard:read",
    "campuslife:lostfound:read",   # 失物招领
    "knowledge:search:read",
    "campuslife:activity:read",   # 二课活动
    "campuslife:activity:write",  # 报名/取消二课活动
    "feed:timeline:read",         # 首页信息流（天气）
    "subscription:sunrun:read",   # 阳光长跑
    "librarybooking:seat:read",   # 图书馆座位
    "campuslife:electricity:read",# 宿舍电费
])


def main() -> int:
    print("=" * 60)
    print("重新授权：补「二课活动报名」「信息流」「阳光长跑」权限")
    print("=" * 60)
    print(f"申请的权限：\n  {SCOPES.replace(',', chr(10) + '  ')}\n")

    # 之前中断过的授权会挡住新请求，先取消掉
    cancel = subprocess.run(
        [HDUHELP_CLI, "auth", "cancel", "--json"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if "cancelled" in (cancel.stdout or ""):
        print("已取消上一次未完成的授权。\n")

    cmd = [HDUHELP_CLI, "auth", "login", "--force", "--scope", SCOPES,
           "--no-input", "--no-open", "--json"]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")

    payload = None
    for stream in (proc.stdout, proc.stderr):
        text = (stream or "").strip()
        if not text:
            continue
        try:
            payload = json.loads(text)
            break
        except json.JSONDecodeError:
            continue

    if not payload:
        print("调用失败，原始输出：")
        print((proc.stdout or "")[:500])
        print((proc.stderr or "")[:500])
        print("\n把这段输出截图发我。")
        return 1

    if payload.get("code"):
        print(f"调用出错：{payload.get('code')} - {payload.get('message')}")
        print("\n把这段输出截图发我。")
        return 1

    status = payload.get("status")
    url = payload.get("verification_uri_complete") or ""
    code = payload.get("user_code") or ""

    if status in ("authenticated", "success"):
        print("已经授权过了，不需要重复操作。")
        return 0

    if not url:
        print("没有拿到授权链接，原始返回：")
        print(json.dumps(payload, ensure_ascii=False, indent=2)[:800])
        print("\n把这段输出截图发我。")
        return 1

    print("请在浏览器打开下面的链接（10 分钟内有效）：\n")
    print(f"  {url}\n")
    if code:
        print(f"用户码：{code}\n")
    print("浏览器里：用学号 26040827 登录 → 确认权限 → 点「同意授权」。")
    print("授权完成后回飞书问「这周有啥活动」验证。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
