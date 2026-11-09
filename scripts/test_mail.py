"""邮箱连接诊断：试多组「服务器 × 账号 × 凭据」，找出能登上的组合。"""
from __future__ import annotations

import imaplib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def load_env() -> dict[str, str]:
    values: dict[str, str] = {}
    env_file = ROOT / ".env"
    if env_file.exists():
        for raw in env_file.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return values


def try_login(host: str, user: str, pwd: str) -> str:
    try:
        conn = imaplib.IMAP4_SSL(host, 993, timeout=12)
        try:
            conn.xatom("ID", '("name" "campus-assistant" "version" "1.0")')
        except Exception:
            pass
        conn.login(user, pwd)
        typ, box = conn.select("INBOX")
        count = box[0].decode() if typ == "OK" and box and box[0] else "?"
        conn.logout()
        return f"OK 收件箱 {count} 封"
    except Exception as exc:
        return f"FAIL {type(exc).__name__}: {str(exc)[:60]}"


def main() -> int:
    env = load_env()
    account = env.get("MAIL_ACCOUNT", "")
    passwords = {
        "统一认证密码": env.get("MAIL_PASSWORD", ""),
        "网易授权码": env.get("MAIL_AUTH_CODE", ""),
    }
    accounts = [account, account.split("@")[0]] if account else []
    hosts = ["imap.163.com", "imap.hdu.edu.cn", "mail.hdu.edu.cn"]

    found = False
    for host in hosts:
        for user in accounts:
            for label, pwd in passwords.items():
                if not pwd:
                    continue
                result = try_login(host, user, pwd)
                mark = "  <<< 成功" if result.startswith("OK") else ""
                print(f"{host:18s} | {user:22s} | {label:8s} | {result}{mark}")
                if result.startswith("OK"):
                    found = True
    if not found:
        print("\n全部组合都失败。可能是密码不对，或者学校邮箱要求用别的服务器。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
