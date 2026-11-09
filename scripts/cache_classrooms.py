"""刷新楼栋索引（楼名 → buildingID）。

以前这里会拉每间教室的占用存本地，后来发现不同楼栋的周次数据不一致
（正心楼是第 10 周、教研楼是第 1 周），本地算会错。
现在改成：只建楼栋索引，查空闲时直接调接口 `--status unused`。

用法：
    py -3 scripts/cache_classrooms.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.tools import classroom  # noqa: E402


def main() -> int:
    print("正在刷新楼栋索引（拉全校教室列表，约 1089 条）...")
    result = classroom.refresh_buildings()
    print(f"结果：{result}")
    if result.get("ok"):
        print("\n已索引楼栋：")
        for b in classroom.cached_buildings():
            print(f"  {b}: {classroom.building_label(b)}")
        print("\n以后不需要每天刷新，学校换了楼再跑一次即可。")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
