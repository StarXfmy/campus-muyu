"""配置读取：优先环境变量，其次 .env 文件。"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()

FEISHU_APP_ID = os.getenv("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.getenv("FEISHU_APP_SECRET", "")

LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "glm-4-flash")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4")
LLM_TIMEOUT = int(os.getenv("LLM_TIMEOUT", "15"))

# 聊天模型独立配置：不填就回落到功能模型（智谱）
CHAT_API_KEY = os.getenv("CHAT_API_KEY", "") or LLM_API_KEY
CHAT_MODEL = os.getenv("CHAT_MODEL", "glm-4-air")
CHAT_BASE_URL = os.getenv("CHAT_BASE_URL", "") or LLM_BASE_URL

MAIL_ACCOUNT = os.getenv("MAIL_ACCOUNT", "")
MAIL_PASSWORD = os.getenv("MAIL_PASSWORD", "")
MAIL_IMAP_HOST = os.getenv("MAIL_IMAP_HOST", "imap.163.com")
MAIL_IMAP_PORT = int(os.getenv("MAIL_IMAP_PORT", "993"))

# 发信默认关着：代码写好了，但要用户自己把 .env 里的开关打开才会真的发
MAIL_SMTP_ENABLED = os.getenv("MAIL_SMTP_ENABLED", "0").strip().lower() in ("1", "true", "yes", "on")
MAIL_SMTP_HOST = os.getenv("MAIL_SMTP_HOST", "smtp.163.com")
MAIL_SMTP_PORT = int(os.getenv("MAIL_SMTP_PORT", "465"))
# 发件人显示名（收件人看到的署名）
MAIL_SENDER_NAME = os.getenv("MAIL_SENDER_NAME", "柳星宇")

# 微信那边只有这个人会被叫「主人」，其他人一律叫「你」；飞书不看这个
WECHAT_OWNER_ID = os.getenv("WECHAT_OWNER_ID", "")

HDUHELP_CLI = os.getenv("HDUHELP_CLI", r"C:\Users\lenovo\bin\hduhelp-cli.exe")
CLI_TIMEOUT = int(os.getenv("CLI_TIMEOUT", "25"))

# Qoder Agent SDK 兜底 agent：默认关。打开后规则未命中会先试 SDK，失败自动回退现有 agent。
SDK_AGENT_ENABLED = os.getenv("SDK_AGENT_ENABLED", "0").strip().lower() in ("1", "true", "yes", "on")
SDK_AGENT_MODEL = os.getenv("SDK_AGENT_MODEL", "dfmodel")
SDK_AGENT_PAT = os.getenv("QODERCN_PERSONAL_ACCESS_TOKEN", "")

DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "campus.db"

# 机器人人设：改项目根目录的 persona.txt 即可，下一条消息就生效（不用重启）
PERSONA_PATH = ROOT / "persona.txt"
_persona_cache = {"mtime": 0.0, "text": ""}


def load_persona() -> str:
    try:
        mtime = PERSONA_PATH.stat().st_mtime
    except OSError:
        return ""
    if mtime != _persona_cache["mtime"]:
        _persona_cache["text"] = PERSONA_PATH.read_text(encoding="utf-8").strip()
        _persona_cache["mtime"] = mtime
    return _persona_cache["text"]

# 空教室缓存覆盖的楼栋（用户上课的校区：正心楼 ZX / 创智楼 CZ / 青越楼 QY）
CACHE_BUILDINGS = os.getenv("CACHE_BUILDINGS", "ZX,CZ,QY").split(",")
CACHE_WEEK = int(os.getenv("CACHE_WEEK", "1"))
