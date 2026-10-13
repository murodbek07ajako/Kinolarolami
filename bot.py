import asyncio, html, os, json, hmac, hashlib, sqlite3, time
from urllib.parse import parse_qsl
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart, CommandObject
from aiogram.types import (Message, CallbackQuery, InlineKeyboardMarkup,
                           InlineKeyboardButton, WebAppInfo, FSInputFile)

TOKEN = os.environ["BOT_TOKEN"]
ADMINS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}
WEBAPP_URL = os.getenv("WEBAPP_URL", "")
PORT = int(os.getenv("PORT", 8080))
DB_PATH = os.getenv("DB_PATH", "data.db")

bot = Bot(TOKEN)
dp = Dispatcher()

db = sqlite3.connect(DB_PATH)
db.row_factory = sqlite3.Row
db.executescript("""
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, joined INTEGER, ref_by INTEGER);
CREATE TABLE IF NOT EXISTS movies(code INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, year TEXT,
  genre TEXT, file_id TEXT, ftype TEXT, views INTEGER DEFAULT 0,
  r_sum INTEGER DEFAULT 0, r_cnt INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS channels(chat_id TEXT PRIMARY KEY, link TEXT);
CREATE TABLE IF NOT EXISTS rated(uid INTEGER, code INTEGER, PRIMARY KEY(uid, code));
""")

for col in ("descr TEXT DEFAULT ''", "poster TEXT DEFAULT ''", "trailer TEXT DEFAULT ''", "notify INTEGER DEFAULT 0"):
    try: db.execute(f"ALTER TABLE movies ADD COLUMN {col}")
    except sqlite3.OperationalError: pass
db.execute("CREATE TABLE IF NOT EXISTS pending(uid INTEGER PRIMARY KEY, code INTEGER)")
db.commit()
