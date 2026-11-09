"""Windows 控制台编码修正。

中文 Windows 控制台默认 GBK，编不了 `•`（U+2022）和 `💡` 这类字符，
双击 ★1 聊天或跑回归时会直接 UnicodeEncodeError 崩掉。
这里统一切到 UTF-8 并把错误替换掉——宁可显示成问号，也不能中断。
"""
from __future__ import annotations

import subprocess
import sys


def setup() -> None:
    if sys.platform == "win32":
        try:
            # 让控制台能正确显示 UTF-8，不只是 Python 内部编码对
            subprocess.run(
                ["cmd", "/c", "chcp", "65001"],
                capture_output=True,
                check=False,
            )
        except Exception:
            pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
