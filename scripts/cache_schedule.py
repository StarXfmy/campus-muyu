"""拉整学期课表写入缓存。

用法：
    py -3 scripts/cache_schedule.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.tools import schedule_store  # noqa: E402


def main() -> int:
    print("正在拉取整学期课表...")
    result = schedule_store.refresh()
    print(f"结果：{result}")
    if result.get("ok"):
        print("\n本周课程预览：")
        for item in schedule_store.this_week()[:5]:
            print(f"  {schedule_store.WEEKDAY_CN.get(item['day'])} 第{item['start_section']}-{item['end_section']}节 {item['course']}")
        print("\n以后每学期开学跑一次即可。")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
