"""命令行聊天模式：不启动飞书，直接在终端里跟机器人对话。

用法：
    py -3 scripts/chat.py
输入 exit 或 quit 退出。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.console import setup  # noqa: E402

setup()

from app.agent import handlers  # noqa: E402


def main() -> int:
    print("=" * 56)
    print("校园助手 · 命令行版")
    print("直接打字问，比如：今天有啥课 / 校园卡余额 / 3 楼有空教室吗")
    print("输入 exit 退出")
    print("=" * 56)
    while True:
        try:
            text = input("\n你 > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见")
            return 0
        if not text:
            continue
        if text.lower() in ("exit", "quit", "q"):
            print("再见")
            return 0
        print("\n助手 >")
        print("\n\n".join(handlers.answer_blocks(text)))


if __name__ == "__main__":
    raise SystemExit(main())
