"""环境自检：不启动机器人，只确认这台机器现在能不能跑。

给 run_bot.bat --check 用。只报「有没有」，不打印任何密钥内容。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.console import setup  # noqa: E402

setup()


def main() -> int:
    from app import config
    from app.ops import backup

    problems: list[str] = []
    print(f"Python      : {sys.version.split()[0]}")

    for module in ("lark_oapi", "PIL", "numpy", "faster_whisper", "imageio_ffmpeg"):
        try:
            __import__(module)
            print(f"依赖 {module:16s}: OK")
        except Exception as exc:
            print(f"依赖 {module:16s}: 缺失（{type(exc).__name__}）")
            problems.append(f"缺依赖 {module}")

    checks = (
        ("飞书 App ID", bool(config.FEISHU_APP_ID)),
        ("飞书 App Secret", bool(config.FEISHU_APP_SECRET)),
        ("功能模型 Key", bool(config.LLM_API_KEY)),
        ("聊天模型 Key", bool(config.CHAT_API_KEY)),
        ("邮箱账号", bool(config.MAIL_ACCOUNT)),
    )
    for label, ok in checks:
        print(f"{label:16s}: {'已配置' if ok else '未配置'}")
        if not ok:
            problems.append(f"{label} 未配置")

    cli = Path(config.HDUHELP_CLI)
    print(f"{'hduhelp-cli':16s}: {'存在' if cli.exists() else '找不到'} {cli}")
    if not cli.exists():
        problems.append("hduhelp-cli 不存在")

    db = Path(config.DB_PATH)
    if db.exists():
        print(f"{'数据库':16s}: {db.stat().st_size / 1024 / 1024:.1f} MB")
    else:
        print(f"{'数据库':16s}: 不存在（首次运行会新建）")

    try:
        path = backup.BACKUP_DIR
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        print(f"{'备份目录':16s}: 可写 {path}")
    except Exception as exc:
        print(f"{'备份目录':16s}: 不可写（{type(exc).__name__}）")
        problems.append("备份目录不可写")

    persona = Path(config.PERSONA_PATH)
    print(f"{'人设文件':16s}: {'存在' if persona.exists() else '缺失'}")

    print()
    if problems:
        print("环境检查未通过：")
        for item in problems:
            print(f"  - {item}")
        return 1
    print("环境检查通过，可以启动机器人。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
