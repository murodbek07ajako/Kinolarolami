"""Sozlamalar (.env fayldan o'qiladi)."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def _load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_env(BASE_DIR / ".env")

BOT_TOKEN: str = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS: set[int] = {
    int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x.lstrip("-").isdigit()
}
# Mini App chatidagi «Adminga murojaat» tugmasi shu foydalanuvchining lichkasini ochadi (@siz, @ belgisiz ham bo'ladi).
# Bo'sh qoldirsangiz — birinchi bosh adminning ID si ishlatiladi.
ADMIN_CONTACT: str = os.getenv("ADMIN_CONTACT", "").strip().lstrip("@")
BOT_TITLE: str = os.getenv("BOT_TITLE", "MEGA CINEMA").strip()
PORT: int = int(os.getenv("PORT", "8080"))
DB_PATH: str = os.getenv("DB_PATH", str(BASE_DIR / "data" / "bot.db"))

# WEBAPP_URL berilmasa, Render o'zi beradigan RENDER_EXTERNAL_URL ishlatiladi
_raw_url = (os.getenv("WEBAPP_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").strip().rstrip("/")
WEBAPP_URL_RAW: str = _raw_url
# Telegram Mini App faqat HTTPS ni qabul qiladi
WEBAPP_URL: str = _raw_url if _raw_url.startswith("https://") else ""

WEBAPP_DIR: Path = BASE_DIR
POSTER_DIR: Path = BASE_DIR / "data" / "posters"
