"""机器人启动入口，给 run_bot.bat 用。

单独一层是为了把控制台编码修好、把崩溃日志落盘，再进 bot.main()。
返回码 0 表示用户主动退出（bat 不再重启），非 0 会被 bat 拉起来。
"""
from __future__ import annotations

import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.console import setup  # noqa: E402

setup()


def main() -> int:
    from app.feishu import bot

    try:
        return bot.main()
    except KeyboardInterrupt:
        return 0
    except Exception:
        traceback.print_exc()
        try:
            from app.ops import audit

            audit.setup_logging()
            audit.logger.exception("机器人进程异常退出")
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
