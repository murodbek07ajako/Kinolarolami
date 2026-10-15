"""SQLite bilan ishlash (aiosqlite)."""
import json
import shutil
import sqlite3
import time
from pathlib import Path

import aiosqlite

_db: aiosqlite.Connection | None = None
_path: str = ""

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY,
  full_name TEXT,
  username TEXT,
  joined_at INTEGER NOT NULL,
  last_active INTEGER NOT NULL,
  banned INTEGER NOT NULL DEFAULT 0,
  blocked INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS movies(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  year TEXT NOT NULL DEFAULT '',
  genre TEXT NOT NULL DEFAULT '',
  genre_lc TEXT NOT NULL DEFAULT '',
  country TEXT NOT NULL DEFAULT '',
  language TEXT NOT NULL DEFAULT '',
  quality TEXT NOT NULL DEFAULT '',
  file_id TEXT NOT NULL,
  file_type TEXT NOT NULL DEFAULT 'video',
  poster_id TEXT NOT NULL DEFAULT '',
  search_text TEXT NOT NULL DEFAULT '',
  views INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL,
  src_chat INTEGER,
  src_msg INTEGER,
  status TEXT NOT NULL DEFAULT 'live'
);
CREATE TABLE IF NOT EXISTS sources(
  chat_id INTEGER PRIMARY KEY,
  title TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS drafts(
  admin_id INTEGER PRIMARY KEY,
  data TEXT NOT NULL,
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS favorites(
  user_id INTEGER NOT NULL,
  movie_id INTEGER NOT NULL,
  PRIMARY KEY(user_id, movie_id)
);
CREATE TABLE IF NOT EXISTS channels(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id INTEGER UNIQUE NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  link TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS views(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  movie_id INTEGER NOT NULL,
  at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_views_at ON views(at);
CREATE INDEX IF NOT EXISTS idx_movies_views ON movies(views DESC);
CREATE INDEX IF NOT EXISTS idx_users_active ON users(last_active);
"""

_APOS = str.maketrans({"’": "'", "‘": "'", "ʻ": "'", "ʼ": "'", "`": "'", "´": "'"})


def norm(s) -> str:
    """Qidiruv uchun matnni bir xil ko'rinishga keltiradi (o', o‘, oʻ ... bir xil)."""
    return str(s or "").translate(_APOS).casefold().strip()


async def init(path: str) -> None:
    global _db, _path
    _path = path
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    _db = await aiosqlite.connect(path)
    _db.row_factory = aiosqlite.Row
    await _db.execute("PRAGMA journal_mode=WAL")
    await _db.executescript(SCHEMA)
    await _migrate()


async def _migrate() -> None:
    """Eski bazalarni (zaxiradan tiklanganlarini ham) yangi tuzilishga keltiradi."""
    cols = {r["name"] for r in await _all("PRAGMA table_info(movies)")}
    for name in ("src_chat", "src_msg"):
        if name not in cols:
            await _db.execute(f"ALTER TABLE movies ADD COLUMN {name} INTEGER")
    if "status" not in cols:
        await _db.execute("ALTER TABLE movies ADD COLUMN status TEXT NOT NULL DEFAULT 'live'")
    # Bir kanal postidan bitta kino (takroriy qo'shilishdan himoya)
    await _db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_movies_src ON movies(src_chat, src_msg) WHERE src_chat IS NOT NULL"
    )
    await _db.commit()


async def close() -> None:
    global _db
    if _db:
        await _db.close()
        _db = None


# ───────────── Zaxira nusxa / tiklash ─────────────
REQUIRED_TABLES = {"users", "movies", "favorites", "channels", "settings", "views"}


async def backup_to(dest: str) -> None:
    """Butun bazaning yaxlit nusxasini dest faylga yozadi."""
    Path(dest).unlink(missing_ok=True)
    safe = dest.replace("'", "''")
    await _db.execute(f"VACUUM INTO '{safe}'")


def validate_backup(path: str) -> bool:
    """Fayl haqiqiy kino bot bazasi ekanini tekshiradi."""
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            con.execute("SELECT COUNT(*) FROM movies").fetchone()
        finally:
            con.close()
        return REQUIRED_TABLES <= names
    except Exception:
        return False


async def replace_from(src: str) -> None:
    """Joriy bazani src fayl bilan almashtiradi (tiklash)."""
    await close()
    for suffix in ("", "-wal", "-shm"):
        Path(_path + suffix).unlink(missing_ok=True)
    shutil.copyfile(src, _path)
    await init(_path)


async def _one(sql: str, params=()):
    cur = await _db.execute(sql, params)
    row = await cur.fetchone()
    await cur.close()
    return row


async def _all(sql: str, params=()):
    cur = await _db.execute(sql, params)
    rows = await cur.fetchall()
    await cur.close()
    return rows


async def _run(sql: str, params=()) -> int:
    cur = await _db.execute(sql, params)
    await _db.commit()
    last = cur.lastrowid
    await cur.close()
    return last


# ───────────── Foydalanuvchilar ─────────────
async def touch_user(uid: int, full_name: str, username: str | None) -> bool:
    """Foydalanuvchini qo'shadi/yangilaydi. Ban qilingan bo'lsa True qaytaradi."""
    now = int(time.time())
    await _db.execute(
        """INSERT INTO users(id, full_name, username, joined_at, last_active)
           VALUES(?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET full_name=excluded.full_name,
             username=excluded.username, last_active=excluded.last_active, blocked=0""",
        (uid, full_name, username, now, now),
    )
    await _db.commit()
    row = await _one("SELECT banned FROM users WHERE id=?", (uid,))
    return bool(row["banned"])


async def set_ban(uid: int, flag: bool) -> None:
    now = int(time.time())
    await _run(
        """INSERT INTO users(id, full_name, username, joined_at, last_active, banned)
           VALUES(?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET banned=excluded.banned""",
        (uid, "", None, now, now, int(flag)),
    )


async def is_banned(uid: int) -> bool:
    row = await _one("SELECT banned FROM users WHERE id=?", (uid,))
    return bool(row and row["banned"])


async def mark_blocked(uid: int) -> None:
    await _run("UPDATE users SET blocked=1 WHERE id=?", (uid,))


async def broadcast_ids() -> list[int]:
    rows = await _all("SELECT id FROM users WHERE banned=0 AND blocked=0")
    return [r["id"] for r in rows]


# ───────────── Kinolar ─────────────
META_FIELDS = ("title", "description", "year", "genre", "country", "language", "quality")


def _derive(d: dict) -> tuple[str, str]:
    """(qidiruv matni, kichik harfli janrlar) — d dagi meta maydonlardan."""
    genre = d.get("genre", "") or ""
    search = norm(" ".join([d.get("title", ""), genre, d.get("year", ""), d.get("country", ""), d.get("language", "")]))
    genre_lc = ",".join(norm(g) for g in genre.split(",") if g.strip())
    return search, genre_lc


async def add_movie(d: dict) -> int:
    search, genre_lc = _derive(d)
    return await _run(
        """INSERT INTO movies(title, description, year, genre, genre_lc, country, language, quality,
                              file_id, file_type, poster_id, search_text, created_at, src_chat, src_msg, status)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            d["title"], d.get("description", ""), d.get("year", ""), d.get("genre", ""), genre_lc,
            d.get("country", ""), d.get("language", ""), d.get("quality", ""),
            d["file_id"], d.get("file_type", "video"), d.get("poster", ""), search, int(time.time()),
            d.get("src_chat"), d.get("src_msg"), d.get("status", "live"),
        ),
    )


async def update_movie_meta(mid: int, d: dict) -> None:
    """Faqat berilgan meta maydonlarni yangilaydi (fayl va poster tegilmaydi)."""
    movie = await get_movie(mid)
    if not movie:
        return
    cur = {k: movie[k] for k in META_FIELDS}
    cur.update({k: v for k, v in d.items() if k in META_FIELDS and v})
    search, genre_lc = _derive(cur)
    await _run(
        """UPDATE movies SET title=?, description=?, year=?, genre=?, genre_lc=?, country=?,
                             language=?, quality=?, search_text=? WHERE id=?""",
        (cur["title"], cur["description"], cur["year"], cur["genre"], genre_lc, cur["country"],
         cur["language"], cur["quality"], search, mid),
    )


async def set_poster(mid: int, poster_id: str) -> None:
    await _run("UPDATE movies SET poster_id=? WHERE id=?", (poster_id, mid))


async def get_movie(mid: int):
    return await _one("SELECT * FROM movies WHERE id=?", (mid,))


async def pending_movies():
    return await _all("SELECT * FROM movies WHERE status='pending' ORDER BY id")


async def publish_pending() -> list[int]:
    """Barcha kutilayotgan kinolarni chiqaradi (live). Chiqarilgan kinolar id ro'yxatini qaytaradi."""
    ids = [r["id"] for r in await pending_movies()]
    if ids:
        await _run("UPDATE movies SET status='live' WHERE status='pending'")
    return ids


async def movie_by_source(chat_id: int, msg_id: int):
    return await _one("SELECT * FROM movies WHERE src_chat=? AND src_msg=?", (chat_id, msg_id))


async def delete_movie(mid: int) -> None:
    await _db.execute("DELETE FROM movies WHERE id=?", (mid,))
    await _db.execute("DELETE FROM favorites WHERE movie_id=?", (mid,))
    await _db.commit()


_ORDER = {
    "new": "m.id DESC",
    "top": "m.views DESC, m.id DESC",
    "year": "CAST(m.year AS INTEGER) DESC, m.id DESC",
}


async def search_movies(q: str = "", genre: str = "", sort: str = "new",
                        limit: int = 20, offset: int = 0, fav_user: int | None = None,
                        status: str = "live"):
    """(qatorlar, jami) qaytaradi. Odatda faqat chiqarilgan (live) kinolar."""
    frm = "FROM movies m"
    params: list = []
    if fav_user is not None:
        frm += " JOIN favorites f ON f.movie_id=m.id AND f.user_id=?"
        params.append(fav_user)
    where = ["m.status = ?"]
    params.append(status)
    for w in norm(q).split():
        where.append("m.search_text LIKE ?")
        params.append(f"%{w}%")
    if genre:
        where.append("(',' || m.genre_lc || ',') LIKE ?")
        params.append(f"%,{norm(genre)},%")
    w_sql = (" WHERE " + " AND ".join(where)) if where else ""
    total = (await _one(f"SELECT COUNT(*) AS c {frm}{w_sql}", params))["c"]
    order = _ORDER.get(sort, _ORDER["new"])
    rows = await _all(f"SELECT m.* {frm}{w_sql} ORDER BY {order} LIMIT ? OFFSET ?", [*params, limit, offset])
    return rows, total


async def top_movies(limit: int = 10):
    return await _all("SELECT * FROM movies WHERE status='live' ORDER BY views DESC, id DESC LIMIT ?", (limit,))


async def new_movies(limit: int = 10):
    return await _all("SELECT * FROM movies WHERE status='live' ORDER BY id DESC LIMIT ?", (limit,))


async def random_movie():
    return await _one("SELECT * FROM movies WHERE status='live' ORDER BY RANDOM() LIMIT 1")


async def genres(limit: int = 24) -> list[str]:
    rows = await _all("SELECT genre FROM movies WHERE genre != '' AND status='live'")
    count: dict[str, int] = {}
    shown: dict[str, str] = {}
    for r in rows:
        for g in r["genre"].split(","):
            g = g.strip()
            if not g:
                continue
            k = norm(g)
            shown.setdefault(k, g)
            count[k] = count.get(k, 0) + 1
    ordered = sorted(count, key=lambda k: (-count[k], k))
    return [shown[k] for k in ordered[:limit]]


async def add_view(uid: int, mid: int) -> None:
    await _db.execute("INSERT INTO views(user_id, movie_id, at) VALUES(?,?,?)", (uid, mid, int(time.time())))
    await _db.execute("UPDATE movies SET views = views + 1 WHERE id=?", (mid,))
    await _db.commit()


# ───────────── Saralanganlar ─────────────
async def is_fav(uid: int, mid: int) -> bool:
    return bool(await _one("SELECT 1 FROM favorites WHERE user_id=? AND movie_id=?", (uid, mid)))


async def toggle_fav(uid: int, mid: int) -> bool:
    """Yangi holatni qaytaradi: True = saralanganlarda."""
    if await is_fav(uid, mid):
        await _run("DELETE FROM favorites WHERE user_id=? AND movie_id=?", (uid, mid))
        return False
    await _run("INSERT OR IGNORE INTO favorites(user_id, movie_id) VALUES(?,?)", (uid, mid))
    return True


async def fav_ids(uid: int) -> list[int]:
    rows = await _all("SELECT movie_id FROM favorites WHERE user_id=?", (uid,))
    return [r["movie_id"] for r in rows]


async def fav_movies(uid: int, limit: int = 30):
    return await _all(
        "SELECT m.* FROM movies m JOIN favorites f ON f.movie_id=m.id WHERE f.user_id=? AND m.status='live' ORDER BY m.id DESC LIMIT ?",
        (uid, limit),
    )


# ───────────── Kanallar (majburiy obuna) ─────────────
async def add_channel(chat_id: int, title: str, link: str) -> None:
    await _run(
        """INSERT INTO channels(chat_id, title, link) VALUES(?,?,?)
           ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title, link=excluded.link""",
        (chat_id, title, link),
    )


async def list_channels():
    return await _all("SELECT * FROM channels ORDER BY id")


async def del_channel(cid: int) -> None:
    await _run("DELETE FROM channels WHERE id=?", (cid,))


# ───────────── Avto-yuklash kanallari (manba) ─────────────
async def add_source(chat_id: int, title: str) -> None:
    await _run(
        """INSERT INTO sources(chat_id, title) VALUES(?,?)
           ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title""",
        (chat_id, title),
    )


async def list_sources():
    return await _all("SELECT * FROM sources ORDER BY rowid")


async def del_source(chat_id: int) -> None:
    await _run("DELETE FROM sources WHERE chat_id=?", (chat_id,))


async def is_source(chat_id: int) -> bool:
    return bool(await _one("SELECT 1 FROM sources WHERE chat_id=?", (chat_id,)))


# ───────────── Qoralama: Mini App'da kiritilgan, video kutilayotgan kino ─────────────
async def save_draft(admin_id: int, data: dict) -> None:
    await _run(
        """INSERT INTO drafts(admin_id, data, created_at) VALUES(?,?,?)
           ON CONFLICT(admin_id) DO UPDATE SET data=excluded.data, created_at=excluded.created_at""",
        (admin_id, json.dumps(data, ensure_ascii=False), int(time.time())),
    )


async def get_draft(admin_id: int) -> dict | None:
    row = await _one("SELECT data FROM drafts WHERE admin_id=?", (admin_id,))
    if not row:
        return None
    try:
        return json.loads(row["data"])
    except ValueError:
        return None


async def clear_draft(admin_id: int) -> None:
    await _run("DELETE FROM drafts WHERE admin_id=?", (admin_id,))


# ───────────── Sozlamalar ─────────────
async def get_setting(key: str, default: str = "") -> str:
    row = await _one("SELECT value FROM settings WHERE key=?", (key,))
    return row["value"] if row else default


async def set_setting(key: str, value: str) -> None:
    await _run(
        "INSERT INTO settings(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


# ───────────── Statistika ─────────────
async def stats() -> dict:
    now = int(time.time())
    day, week = now - 86400, now - 7 * 86400
    one = lambda sql, p=(): _one(sql, p)  # noqa: E731
    return {
        "users": (await one("SELECT COUNT(*) c FROM users"))["c"],
        "new_day": (await one("SELECT COUNT(*) c FROM users WHERE joined_at>=?", (day,)))["c"],
        "new_week": (await one("SELECT COUNT(*) c FROM users WHERE joined_at>=?", (week,)))["c"],
        "active_day": (await one("SELECT COUNT(*) c FROM users WHERE last_active>=?", (day,)))["c"],
        "banned": (await one("SELECT COUNT(*) c FROM users WHERE banned=1"))["c"],
        "blocked": (await one("SELECT COUNT(*) c FROM users WHERE blocked=1"))["c"],
        "movies": (await one("SELECT COUNT(*) c FROM movies WHERE status='live'"))["c"],
        "pending": (await one("SELECT COUNT(*) c FROM movies WHERE status='pending'"))["c"],
        "views": (await one("SELECT COUNT(*) c FROM views"))["c"],
        "views_day": (await one("SELECT COUNT(*) c FROM views WHERE at>=?", (day,)))["c"],
        "channels": (await one("SELECT COUNT(*) c FROM channels"))["c"],
        "top": [dict(r) for r in await top_movies(5)],
    }
