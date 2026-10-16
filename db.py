"""SQLite bilan ishlash (aiosqlite)."""
import asyncio
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
  cover_id TEXT NOT NULL DEFAULT '',
  search_text TEXT NOT NULL DEFAULT '',
  views INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL,
  src_chat INTEGER,
  src_msg INTEGER,
  status TEXT NOT NULL DEFAULT 'live',
  kind TEXT NOT NULL DEFAULT 'kino',
  series_id INTEGER,
  ep INTEGER,
  num INTEGER
);
CREATE TABLE IF NOT EXISTS series(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL DEFAULT 'serial',
  title TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  year TEXT NOT NULL DEFAULT '',
  genre TEXT NOT NULL DEFAULT '',
  genre_lc TEXT NOT NULL DEFAULT '',
  country TEXT NOT NULL DEFAULT '',
  language TEXT NOT NULL DEFAULT '',
  poster_id TEXT NOT NULL DEFAULT '',
  cover_id TEXT NOT NULL DEFAULT '',
  search_text TEXT NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL,
  num INTEGER
);
CREATE TABLE IF NOT EXISTS admins(
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL DEFAULT '',
  role TEXT NOT NULL DEFAULT 'admin',
  perms TEXT NOT NULL DEFAULT '',
  sections TEXT NOT NULL DEFAULT '',
  added_by INTEGER,
  added_at INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sources(
  chat_id INTEGER PRIMARY KEY,
  title TEXT NOT NULL DEFAULT '',
  kind TEXT NOT NULL DEFAULT 'kino',
  series_id INTEGER
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
CREATE INDEX IF NOT EXISTS idx_views_user ON views(user_id, at);
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
    await _load_nums()


async def _migrate() -> None:
    """Eski bazalarni (zaxiradan tiklanganlarini ham) yangi tuzilishga keltiradi."""
    cols = {r["name"] for r in await _all("PRAGMA table_info(movies)")}
    for name in ("src_chat", "src_msg"):
        if name not in cols:
            await _db.execute(f"ALTER TABLE movies ADD COLUMN {name} INTEGER")
    if "status" not in cols:
        await _db.execute("ALTER TABLE movies ADD COLUMN status TEXT NOT NULL DEFAULT 'live'")
    for name, ddl in (("kind", "TEXT NOT NULL DEFAULT 'kino'"), ("series_id", "INTEGER"), ("ep", "INTEGER")):
        if name not in cols:
            await _db.execute(f"ALTER TABLE movies ADD COLUMN {name} {ddl}")
    # Yakka kinolarning o'z kodi (1, 2, 3...) — qismlar (serial/multfilm/anime) bu hisobga kirmaydi
    if "num" not in cols:
        await _db.execute("ALTER TABLE movies ADD COLUMN num INTEGER")
    # Eski yagona (barcha turlar uchun umumiy) indeks olib tashlanadi: endi har tur o'z raqamini sanaydi
    await _db.execute("DROP INDEX IF EXISTS idx_movies_num")
    scols = {r["name"] for r in await _all("PRAGMA table_info(sources)")}
    if "kind" not in scols:
        await _db.execute("ALTER TABLE sources ADD COLUMN kind TEXT NOT NULL DEFAULT 'kino'")
    if "series_id" not in scols:
        await _db.execute("ALTER TABLE sources ADD COLUMN series_id INTEGER")
    sercols = {r["name"] for r in await _all("PRAGMA table_info(series)")}
    if "cover_id" not in cols:
        await _db.execute("ALTER TABLE movies ADD COLUMN cover_id TEXT NOT NULL DEFAULT ''")
    if "cover_id" not in sercols:
        await _db.execute("ALTER TABLE series ADD COLUMN cover_id TEXT NOT NULL DEFAULT ''")
    if "num" not in sercols:
        await _db.execute("ALTER TABLE series ADD COLUMN num INTEGER")
    # Raqami yo'q seriallarga HAR TUR uchun alohida tartib raqami beriladi: s.1, s.2 / m.1 / a.1 ...
    for kind in KIND_PREFIX:
        rows = await _all("SELECT id FROM series WHERE kind=? AND (num IS NULL OR num=0) ORDER BY id", (kind,))
        last = (await _one("SELECT COALESCE(MAX(num), 0) AS n FROM series WHERE kind=?", (kind,)))["n"]
        for r in rows:
            last += 1
            await _db.execute("UPDATE series SET num=? WHERE id=?", (last, r["id"]))
    await _db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_series_num ON series(kind, num)")
    await _reconcile_nums()
    await _db.execute("CREATE INDEX IF NOT EXISTS idx_movies_series ON movies(series_id)")
    await _db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_movies_ep ON movies(series_id, ep) WHERE series_id IS NOT NULL"
    )
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


_movie_lock = asyncio.Lock()


async def add_movie(d: dict) -> int:
    search, genre_lc = _derive(d)
    async with _movie_lock:
        num = None
        if not d.get("series_id"):  # yakka kontent: o'z turi ichida tartib raqami (kodi)
            num = await _next_num(d.get("kind", "kino"))
        return await _run(
            """INSERT INTO movies(title, description, year, genre, genre_lc, country, language, quality,
                                  file_id, file_type, poster_id, search_text, created_at, src_chat, src_msg, status,
                                  kind, series_id, ep, cover_id, num)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                d["title"], d.get("description", ""), d.get("year", ""), d.get("genre", ""), genre_lc,
                d.get("country", ""), d.get("language", ""), d.get("quality", ""),
                d["file_id"], d.get("file_type", "video"), d.get("poster", ""), search, int(time.time()),
                d.get("src_chat"), d.get("src_msg"), d.get("status", "live"),
                d.get("kind", "kino"), d.get("series_id") or None, d.get("ep"), d.get("cover", ""), num,
            ),
        )


async def _next_num(kind: str) -> int:
    """Shu tur uchun keyingi bo'sh raqam. Yakka (dona) va qismli kontent BIR hisobdan foydalanadi,
    shuning uchun m.1 ham dona multfilm, ham qismli multfilm bo'la olmaydi."""
    a = (await _one("SELECT COALESCE(MAX(num), 0) AS n FROM movies WHERE series_id IS NULL AND kind=?", (kind,)))["n"]
    b = (await _one("SELECT COALESCE(MAX(num), 0) AS n FROM series WHERE kind=?", (kind,)))["n"]
    return max(a, b) + 1


async def _reconcile_nums() -> None:
    """Eski bazani tuzatadi: raqami yo'q yoki qismli kontent bilan to'qnashgan yakka kontentga
    o'z turi bo'yicha yangi raqam beradi, so'ng (tur, raqam) bo'yicha yagona indeks yaratadi."""
    used = {(r["kind"], r["num"]) for r in await _all("SELECT kind, num FROM series WHERE num IS NOT NULL")}
    keep, need = set(), []
    for r in await _all("SELECT id, kind, num FROM movies WHERE series_id IS NULL ORDER BY id"):
        k = (r["kind"], r["num"])
        if r["num"] and k not in keep and k not in used:
            keep.add(k)
        else:
            need.append(r)
    top: dict[str, int] = {}
    for kind, num in keep | used:
        top[kind] = max(top.get(kind, 0), num)
    for r in need:
        top[r["kind"]] = top.get(r["kind"], 0) + 1
        await _db.execute("UPDATE movies SET num=? WHERE id=?", (top[r["kind"]], r["id"]))
    await _db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_movies_kind_num ON movies(kind, num) "
        "WHERE series_id IS NULL AND num IS NOT NULL"
    )


async def movie_by_num(n: int, kind: str = "kino"):
    """Yakka kontentni foydalanuvchi ko'radigan kodi bo'yicha topadi: kino — 1, 2, 3; multfilm — m.1; anime — a.1."""
    return await _one("SELECT * FROM movies WHERE series_id IS NULL AND kind=? AND num=?", (kind, n))


async def movie_by_code(text: str):
    """'12' (kino) yoki 'm.3' / 'a.3' (dona multfilm / anime) -> yakka kontent qatori yoki None."""
    t = (text or "").strip().lower()
    if t.isdigit():
        return await movie_by_num(int(t)) if len(t) < 10 else None
    c = parse_series_code(t)
    return await movie_by_num(c[2], c[0]) if c else None


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


async def replace_movie_meta(mid: int, d: dict) -> None:
    """Admin tahriri: maydonlar to'liq almashtiriladi (bo'sh qiymat = tozalash)."""
    cur = {k: d.get(k, "") or "" for k in META_FIELDS}
    search, genre_lc = _derive(cur)
    await _run(
        """UPDATE movies SET title=?, description=?, year=?, genre=?, genre_lc=?, country=?,
                             language=?, quality=?, search_text=? WHERE id=?""",
        (cur["title"], cur["description"], cur["year"], cur["genre"], genre_lc, cur["country"],
         cur["language"], cur["quality"], search, mid),
    )


async def set_cover(mid: int, cover_id: str) -> None:
    """Mini App muqovasi (faqat ilova ichida ko'rinadi). Bo'sh qiymat = olib tashlash (logo chiqadi)."""
    await _run("UPDATE movies SET cover_id=? WHERE id=?", (cover_id or "", mid))


async def set_series_cover(sid: int, cover_id: str) -> None:
    await _run("UPDATE series SET cover_id=? WHERE id=?", (cover_id or "", sid))


async def set_poster(mid: int, poster_id: str) -> None:
    await _run("UPDATE movies SET poster_id=? WHERE id=?", (poster_id, mid))


async def get_movie(mid: int):
    return await _one("SELECT * FROM movies WHERE id=?", (mid,))


async def pending_movies():
    return await _all("SELECT * FROM movies WHERE status='pending' ORDER BY id")


def _scope_sql(scope) -> tuple[str, tuple]:
    """Chiqarish doirasi: None/'all' | 'single' | 'single:<tur>' | 's:<serial id>'."""
    if not scope or scope == "all":
        return "", ()
    if scope == "single":
        return " AND series_id IS NULL", ()
    if scope.startswith("single:"):
        return " AND series_id IS NULL AND kind=?", (scope.split(":", 1)[1],)
    if scope.startswith("s:") and scope[2:].isdigit():
        return " AND series_id=?", (int(scope[2:]),)
    raise ValueError("bad scope")


async def publish_pending(scope=None) -> list[int]:
    """Kutilayotgan kinolarni (butun yoki doira bo'yicha) chiqaradi. Chiqarilgan id'lar ro'yxatini qaytaradi."""
    extra, params = _scope_sql(scope)
    ids = [r["id"] for r in await _all(f"SELECT id FROM movies WHERE status='pending'{extra} ORDER BY id", params)]
    if ids:
        await _run(f"UPDATE movies SET status='live' WHERE status='pending'{extra}", params)
    return ids


# ───────────── Qismli kontent (serial / multfilm / anime) ─────────────
KIND_PREFIX = {"serial": "s", "multfilm": "m", "anime": "a"}
PREFIX_KIND = {v: k for k, v in KIND_PREFIX.items()}
KINDS = ("kino", "multfilm", "serial", "anime")


# Har tur o'z raqamini alohida sanaydi (s.1, s.2 | m.1 | a.1). Ichki id (series.id) esa umumiy.
_NUM: dict[int, int] = {}                 # series.id -> turdagi tartib raqami
_BYNUM: dict[tuple[str, int], int] = {}   # (tur, raqam) -> series.id
_series_lock = asyncio.Lock()


async def _load_nums() -> None:
    _NUM.clear()
    _BYNUM.clear()
    for r in await _all("SELECT id, kind, num FROM series"):
        if r["num"]:
            _NUM[r["id"]] = r["num"]
            _BYNUM[(r["kind"], r["num"])] = r["id"]


def movie_code(m) -> str:
    """Yakka kontent kodi: kino — 1, 2, 3; multfilm — m.1; anime — a.1."""
    keys = m.keys()
    if "series_id" in keys and m["series_id"]:
        return series_code(m["kind"], m["series_id"])
    num = (m["num"] if "num" in keys else None) or m["id"]
    pre = KIND_PREFIX.get(m["kind"]) if "kind" in keys else None
    return f"{pre}.{num}" if pre else str(num)


def series_code(kind: str, sid: int) -> str:
    return f"{KIND_PREFIX.get(kind, 's')}.{_NUM.get(sid, sid)}"


def parse_series_code(text: str):
    """'s.12' / 'M.3' -> (tur, series.id yoki 0, raqam) yoki None. Shunday qismli kontent yo'q bo'lsa id=0."""
    import re
    m = re.fullmatch(r"([sma])\s*\.\s*(\d+)", (text or "").strip().lower())
    if not m:
        return None
    kind = PREFIX_KIND[m.group(1)]
    return kind, _BYNUM.get((kind, int(m.group(2))), 0), int(m.group(2))


async def add_series(d: dict) -> int:
    search, genre_lc = _derive(d)
    kind = d.get("kind", "serial")
    async with _movie_lock:  # dona kontent bilan bir qulf: raqamlar to'qnashmasin
        num = await _next_num(kind)
        sid = await _run(
            """INSERT INTO series(kind, title, description, year, genre, genre_lc, country, language,
                                  poster_id, search_text, created_at, num, cover_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (kind, d["title"], d.get("description", ""), d.get("year", ""), d.get("genre", ""),
             genre_lc, d.get("country", ""), d.get("language", ""), d.get("poster", ""), search,
             int(time.time()), num, d.get("cover", "")),
        )
        _NUM[sid] = num
        _BYNUM[(kind, num)] = sid
    return sid


async def get_series(sid: int):
    return await _one("SELECT * FROM series WHERE id=?", (sid,))


async def list_series():
    return await _all(
        """SELECT s.*, (SELECT COUNT(*) FROM movies e WHERE e.series_id=s.id) AS eps,
                  (SELECT MAX(ep) FROM movies e WHERE e.series_id=s.id) AS last_ep
           FROM series s ORDER BY s.id DESC"""
    )


async def update_series_meta(sid: int, d: dict) -> None:
    """Serial ma'lumotlarini to'liq almashtiradi va qismlardagi nusxalarni ham yangilaydi."""
    cur = {k: (d.get(k) or "") for k in META_FIELDS}
    search, genre_lc = _derive(cur)
    await _db.execute(
        """UPDATE series SET title=?, description=?, year=?, genre=?, genre_lc=?, country=?, language=?,
                             search_text=? WHERE id=?""",
        (cur["title"], cur["description"], cur["year"], cur["genre"], genre_lc, cur["country"],
         cur["language"], search, sid),
    )
    await _db.execute(
        """UPDATE movies SET title=?, description=?, year=?, genre=?, genre_lc=?, country=?, language=?,
                             search_text=? WHERE series_id=?""",
        (cur["title"], cur["description"], cur["year"], cur["genre"], genre_lc, cur["country"],
         cur["language"], search, sid),
    )
    await _db.commit()


async def set_series_poster(sid: int, poster_id: str) -> None:
    await _run("UPDATE series SET poster_id=? WHERE id=?", (poster_id, sid))


async def delete_series(sid: int) -> None:
    await _db.execute("DELETE FROM movies WHERE series_id=?", (sid,))
    await _db.execute("DELETE FROM favorites WHERE movie_id=?", (-sid,))
    await _db.execute("DELETE FROM sources WHERE series_id=?", (sid,))
    await _db.execute("DELETE FROM series WHERE id=?", (sid,))
    await _db.commit()
    for key, val in list(_BYNUM.items()):
        if val == sid:
            del _BYNUM[key]
    _NUM.pop(sid, None)


async def episodes(sid: int, live_only: bool = True):
    extra = " AND status='live'" if live_only else ""
    return await _all(f"SELECT * FROM movies WHERE series_id=?{extra} ORDER BY ep", (sid,))


async def set_episode_no(mid: int, ep: int) -> None:
    await _run("UPDATE movies SET ep=? WHERE id=?", (ep, mid))


async def next_episode(sid: int, ep: int):
    """Shu qismdan keyingi chiqarilgan qism (yo'q bo'lsa None)."""
    return await _one(
        "SELECT * FROM movies WHERE series_id=? AND status='live' AND ep>? ORDER BY ep LIMIT 1", (sid, ep))


async def episode_by_no(sid: int, ep: int):
    return await _one("SELECT * FROM movies WHERE series_id=? AND ep=?", (sid, ep))


async def search_series(q: str, limit: int = 5):
    """Botdagi matnli qidiruv uchun: kamida bitta chiqarilgan qismi bor seriallar."""
    where, params = [], []
    for w in norm(q).split():
        where.append("s.search_text LIKE ?")
        params.append(f"%{w}%")
    w_sql = (" AND " + " AND ".join(where)) if where else ""
    return await _all(
        f"""SELECT s.* FROM series s WHERE EXISTS
            (SELECT 1 FROM movies e WHERE e.series_id=s.id AND e.status='live'){w_sql}
            ORDER BY s.id DESC LIMIT ?""",
        [*params, limit],
    )


_ITEMS = """
SELECT 'm' AS t, m.id AS id, m.id AS fk, m.title AS title, m.year AS year, m.genre AS genre, m.genre_lc AS genre_lc,
       m.country AS country, m.quality AS quality, m.views AS views, m.poster_id AS poster_id, m.kind AS kind,
       m.search_text AS search_text, m.created_at AS created_at, 0 AS eps, m.cover_id AS cover_id, m.num AS num
FROM movies m WHERE m.status='live' AND m.series_id IS NULL
UNION ALL
SELECT 's', s.id, -s.id, s.title, s.year, s.genre, s.genre_lc, s.country, '', v.views, s.poster_id, s.kind,
       s.search_text, v.last, v.n, s.cover_id, NULL
FROM series s JOIN (
  SELECT series_id, SUM(views) AS views, MAX(created_at) AS last, COUNT(*) AS n
  FROM movies WHERE status='live' AND series_id IS NOT NULL GROUP BY series_id
) v ON v.series_id = s.id
"""

_ITEM_ORDER = {
    "new": "i.created_at DESC, i.t, i.id DESC",
    "top": "i.views DESC, i.id DESC",
    "year": "CAST(i.year AS INTEGER) DESC, i.id DESC",
}


async def search_items(q: str = "", genre: str = "", sort: str = "new", limit: int = 20, offset: int = 0,
                       fav_user: int | None = None, kind: str = ""):
    """Katalog uchun: kinolar + seriallar aralash. (qatorlar, jami) qaytaradi."""
    frm = "FROM items i"
    params: list = []
    if fav_user is not None:
        frm += " JOIN favorites f ON f.movie_id=i.fk AND f.user_id=?"
        params.append(fav_user)
    where = []
    code = parse_series_code(q)
    if code:
        where.append("((i.t='s' AND i.kind=? AND i.id=?) OR (i.t='m' AND i.kind=? AND i.num=?))")
        params += [code[0], code[1], code[0], code[2]]
    else:
        for w in norm(q).split():
            where.append("i.search_text LIKE ?")
            params.append(f"%{w}%")
    if kind in KINDS:
        where.append("i.kind=?")
        params.append(kind)
    if genre:
        where.append("(',' || i.genre_lc || ',') LIKE ?")
        params.append(f"%,{norm(genre)},%")
    w_sql = (" WHERE " + " AND ".join(where)) if where else ""
    cte = f"WITH items AS ({_ITEMS}) "
    total = (await _one(f"{cte}SELECT COUNT(*) AS c {frm}{w_sql}", params))["c"]
    order = _ITEM_ORDER.get(sort, _ITEM_ORDER["new"])
    rows = await _all(f"{cte}SELECT i.* {frm}{w_sql} ORDER BY {order} LIMIT ? OFFSET ?", [*params, limit, offset])
    return rows, total


async def hero_items(limit: int = 5):
    """Katalog tepasidagi slayder uchun: avval muqovasi borlar, eng yangilaridan."""
    cte = f"WITH items AS ({_ITEMS}) "
    return await _all(
        f"{cte}SELECT i.* FROM items i ORDER BY (i.cover_id != '') DESC, i.created_at DESC LIMIT ?", (limit,))


async def similar_items(t: str, iid: int, kind: str, genre: str, year: str, limit: int = 8):
    """O'xshash kontent: umumiy janrlar (eng muhim), bir xil tur va yaqin yil bo'yicha ball beradi."""
    genres = [g for g in (norm(x) for x in (genre or "").split(",")) if g]
    conds, params = ["i.kind=?"], [kind]
    for g in genres:
        conds.append("(',' || i.genre_lc || ',') LIKE ?")
        params.append(f"%,{g},%")
    sql = (f"WITH items AS ({_ITEMS}) SELECT i.* FROM items i "
           f"WHERE NOT (i.t=? AND i.id=?) AND ({' OR '.join(conds)}) ORDER BY i.views DESC LIMIT 300")
    rows = await _all(sql, [t, iid, *params])
    gset = set(genres)
    y0 = int(year) if str(year or "").isdigit() else None

    def score(r) -> int:
        have = {norm(x) for x in (r["genre_lc"] or "").split(",") if x.strip()}
        sc = 3 * len(gset & have) + (2 if r["kind"] == kind else 0)
        ry = str(r["year"] or "")
        if y0 is not None and ry.isdigit() and abs(int(ry) - y0) <= 3:
            sc += 1
        return sc

    ranked = sorted(rows, key=lambda r: (-score(r), -(r["views"] or 0)))
    return ranked[:limit]


async def watched_episodes(uid: int, sid: int):
    """Foydalanuvchi ko'rgan qismlar (eng oxirgisi birinchi)."""
    return await _all(
        "SELECT v.movie_id AS mid, MAX(v.at) AS at FROM views v JOIN movies m ON m.id=v.movie_id "
        "WHERE v.user_id=? AND m.series_id=? GROUP BY v.movie_id ORDER BY at DESC, mid DESC",
        (uid, sid),
    )


async def pending_groups():
    """Kutilayotganlarni guruhlaydi: [{key,label_kind,series,rows}] — avval yakka kinolar, keyin seriallar."""
    rows = await pending_movies()
    singles: dict[str, list] = {}
    eps: dict[int, list] = {}
    for r in rows:
        if r["series_id"] is None:
            singles.setdefault(r["kind"], []).append(r)
        else:
            eps.setdefault(r["series_id"], []).append(r)
    groups = []
    for kind in KINDS:
        if singles.get(kind):
            groups.append({"key": f"single:{kind}", "kind": kind, "series": None, "rows": singles[kind]})
    for sid, lst in eps.items():
        ser = await get_series(sid)
        if ser:
            groups.append({"key": f"s:{sid}", "kind": ser["kind"], "series": ser,
                           "rows": sorted(lst, key=lambda r: r["ep"] or 0)})
    return groups


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
    where = ["m.status = ?", "m.series_id IS NULL"]
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
    return await _all("SELECT * FROM movies WHERE status='live' AND series_id IS NULL ORDER BY views DESC, id DESC LIMIT ?", (limit,))


async def new_movies(limit: int = 10):
    return await _all("SELECT * FROM movies WHERE status='live' AND series_id IS NULL ORDER BY id DESC LIMIT ?", (limit,))


async def random_movie():
    return await _one("SELECT * FROM movies WHERE status='live' AND series_id IS NULL ORDER BY RANDOM() LIMIT 1")


async def genres(limit: int = 24) -> list[str]:
    rows = await _all(
        "SELECT genre FROM movies WHERE genre != '' AND status='live' AND series_id IS NULL "
        "UNION ALL SELECT genre FROM series WHERE genre != '' AND id IN "
        "(SELECT series_id FROM movies WHERE status='live' AND series_id IS NOT NULL)"
    )
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
        "SELECT m.* FROM movies m JOIN favorites f ON f.movie_id=m.id WHERE f.user_id=? AND m.status='live' AND m.series_id IS NULL ORDER BY m.id DESC LIMIT ?",
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
async def add_source(chat_id: int, title: str, kind: str = "kino", series_id: int | None = None) -> None:
    """Qayta ulansa tur/serial o'zgarmaydi, faqat nomi yangilanadi (berilgan bo'lsa — yangilanadi)."""
    await _run(
        """INSERT INTO sources(chat_id, title, kind, series_id) VALUES(?,?,?,?)
           ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title,
             kind=CASE WHEN excluded.series_id IS NOT NULL OR excluded.kind != 'kino' THEN excluded.kind ELSE sources.kind END,
             series_id=COALESCE(excluded.series_id, sources.series_id)""",
        (chat_id, title, kind, series_id),
    )


async def get_source(chat_id: int):
    return await _one("SELECT * FROM sources WHERE chat_id=?", (chat_id,))


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


# ───────────── Adminlar ─────────────
async def list_admins():
    return await _all("SELECT * FROM admins ORDER BY added_at, id")


async def get_admin(aid: int):
    return await _one("SELECT * FROM admins WHERE id=?", (aid,))


async def save_admin(aid: int, name: str, role: str, perms: str, sections: str, added_by: int | None) -> None:
    await _run(
        """INSERT INTO admins(id, name, role, perms, sections, added_by, added_at) VALUES(?,?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET name=excluded.name, role=excluded.role,
             perms=excluded.perms, sections=excluded.sections""",
        (aid, name, role, perms, sections, added_by, int(time.time())),
    )


async def del_admin(aid: int) -> None:
    await _run("DELETE FROM admins WHERE id=?", (aid,))


async def user_name(uid: int) -> str:
    """Foydalanuvchining bazadagi ismi (botga kirgan bo'lsa)."""
    row = await _one("SELECT full_name, username FROM users WHERE id=?", (uid,))
    if not row:
        return ""
    return (row["full_name"] or "").strip() or (f"@{row['username']}" if row["username"] else "")


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
        "movies": (await one("SELECT COUNT(*) c FROM movies WHERE status='live' AND series_id IS NULL"))["c"],
        "pending": (await one("SELECT COUNT(*) c FROM movies WHERE status='pending'"))["c"],
        "series": (await one(
            "SELECT COUNT(DISTINCT series_id) c FROM movies WHERE status='live' AND series_id IS NOT NULL"))["c"],
        "views": (await one("SELECT COUNT(*) c FROM views"))["c"],
        "views_day": (await one("SELECT COUNT(*) c FROM views WHERE at>=?", (day,)))["c"],
        "channels": (await one("SELECT COUNT(*) c FROM channels"))["c"],
        "top": [dict(r) for r in await top_movies(5)],
    }
