import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load():
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8-sig").splitlines():
            if line.strip() and not line.lstrip().startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    token = os.environ.get("BOT_TOKEN", "")
    if not token or ":" not in token:
        raise RuntimeError("Укажите BOT_TOKEN в переменных окружения или файле .env")
    path = Path(os.environ.get("BOT_DB", "data/bot.sqlite3"))
    return token, str(path if path.is_absolute() else ROOT / path)
