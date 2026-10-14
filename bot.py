import asyncio, html, io, os, json, hmac, hashlib, sqlite3, time
from urllib.parse import parse_qsl, quote
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

def q(sql, *a): return db.execute(sql, a).fetchall()
def run(sql, *a):
    c = db.execute(sql, a); db.commit(); return c

def is_admin(m): return m.from_user.id in ADMINS

# ---------- yordamchilar ----------
async def missing_channels(uid):
    bad = []
    for ch in q("SELECT * FROM channels"):
        try:
            m = await bot.get_chat_member(ch["chat_id"], uid)
            if m.status in ("left", "kicked"): bad.append(ch)
        except Exception:
            pass
    return bad

def sub_kb(chs):
    rows = [[InlineKeyboardButton(text=f"📢 {i+1}-kanal", url=c["link"])] for i, c in enumerate(chs)]
    rows.append([InlineKeyboardButton(text="✅ Tekshirish", callback_data="check")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def main_kb():
    if not WEBAPP_URL: return None
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🎬 Kinolar katalogi", web_app=WebAppInfo(url=WEBAPP_URL))]])

async def send_movie(uid, code):
    m = q("SELECT * FROM movies WHERE code=?", code)
    if not m: return False
    m = m[0]
    if not m["file_id"]: return False
    run("UPDATE movies SET views=views+1 WHERE code=?", code)
    rating = f"{m['r_sum']/m['r_cnt']:.1f}" if m["r_cnt"] else "—"
    cap = (f"🎬 <b>{m['title']}</b>\n📅 {m['year']}  🎭 {m['genre']}\n"
           f"⭐ {rating}  👁 {m['views']+1}\n🔢 Kod: {m['code']}")
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"{n}⭐", callback_data=f"rate:{code}:{n}") for n in range(1, 6)]])
    send = bot.send_video if m["ftype"] == "video" else bot.send_document
    await send(uid, m["file_id"], caption=cap, parse_mode="HTML", reply_markup=kb)
    return True

# ---------- foydalanuvchi ----------
@dp.message(CommandStart())
async def start(msg: Message, command: CommandObject):
    uid = msg.from_user.id
    if not q("SELECT 1 FROM users WHERE id=?", uid):
        ref = int(command.args) if command.args and command.args.isdigit() and int(command.args) != uid else None
        run("INSERT INTO users VALUES(?,?,?)", uid, int(time.time()), ref)
    bad = await missing_channels(uid)
    if bad:
        return await msg.answer("Botdan foydalanish uchun kanallarga obuna bo'ling:", reply_markup=sub_kb(bad))
    me = await bot.me()
    name = html.escape(msg.from_user.first_name or "do'st")
    await msg.answer(
        f"🎬 <b>Xush kelibsiz, {name}!</b>\n\n"
        f"✨ <b>{html.escape(me.first_name)}</b> — sevimli kinolaringizni bir joyda topasiz!\n\n"
        "🍿 <b>Bu yerda siz:</b>\n"
        "• 🔎 Kinoni <b>kodi</b> yoki <b>nomi</b> bilan topasiz\n"
        "• 🎞 Katalogdan janr bo'yicha tanlaysiz\n"
        "• ⭐ Kinolarga baho berasiz\n"
        "• ❤️ Sevimlilarni saqlaysiz\n"
        "• 🔥 Yangi kinolardan birinchi bo'lib xabar olasiz\n\n"
        "👇 Boshlash uchun pastdagi tugmani bosing yoki kino kodini yuboring.\n\n"
        f"🎁 Do'stlaringizni taklif qiling:\nt.me/{me.username}?start={uid}",
        parse_mode="HTML", reply_markup=main_kb())

@dp.callback_query(F.data == "check")
async def check(cb: CallbackQuery):
    bad = await missing_channels(cb.from_user.id)
    if bad:
        return await cb.answer("Hali hamma kanalga obuna bo'lmadingiz ❌", show_alert=True)
    await cb.message.delete()
    await cb.message.answer("✅ Rahmat! Endi kino kodini yoki nomini yuboring.", reply_markup=main_kb())

@dp.callback_query(F.data.startswith("get:"))
async def get_cb(cb: CallbackQuery):
    await cb.answer()
    bad = await missing_channels(cb.from_user.id)
    if bad:
        return await cb.message.answer("Avval kanallarga obuna bo'ling:", reply_markup=sub_kb(bad))
    await send_movie(cb.from_user.id, int(cb.data.split(":")[1]))

@dp.callback_query(F.data.startswith("rate:"))
async def rate(cb: CallbackQuery):
    _, code, n = cb.data.split(":")
    if q("SELECT 1 FROM rated WHERE uid=? AND code=?", cb.from_user.id, code):
        return await cb.answer("Siz allaqachon baho bergansiz", show_alert=True)
    run("INSERT INTO rated VALUES(?,?)", cb.from_user.id, code)
    run("UPDATE movies SET r_sum=r_sum+?, r_cnt=r_cnt+1 WHERE code=?", int(n), code)
    await cb.answer("Baho uchun rahmat! ⭐")

# ---------- admin ----------
@dp.message(Command("admin"), is_admin)
async def admin_help(msg: Message):
    await msg.answer(
        "🛠 <b>Admin buyruqlari</b>\n\n"
        "🎥 Kino qo'shish: videoni yuboring, izohiga:\n<code>Nomi | Yil | Janr</code>\n\n"
        "/stats — statistika\n/movies — oxirgi kinolar\n/delmovie KOD\n"
        "/addchannel @kanal https://t.me/kanal\n/delchannel @kanal\n"
        "/broadcast — xabarga reply qilib yuboring\n/backup — bazani yuklab olish",
        parse_mode="HTML")

@dp.message(F.video | F.document, is_admin)
async def add_movie(msg: Message):
    f, t = (msg.video.file_id, "video") if msg.video else (msg.document.file_id, "doc")
    parts = [p.strip() for p in (msg.caption or "").split("|")]
    if len(parts) >= 3:
        c = run("INSERT INTO movies(title,year,genre,file_id,ftype) VALUES(?,?,?,?,?)", parts[0], parts[1], parts[2], f, t)
        return await msg.answer(f"✅ Qo'shildi. Kod: <b>{c.lastrowid}</b>", parse_mode="HTML")
    p = q("SELECT code FROM pending WHERE uid=?", msg.from_user.id)
    if not p:
        return await msg.answer("Avval katalogdagi ⚙️ Admin bo'limidan kino ma'lumotlarini kiriting.")
    code = p[0]["code"]
    run("UPDATE movies SET file_id=?, ftype=? WHERE code=?", f, t, code)
    run("DELETE FROM pending WHERE uid=?", msg.from_user.id)
    n = q("SELECT notify FROM movies WHERE code=?", code)[0]["notify"]
    await msg.answer(f"✅ Video qo'shildi. Kod: <b>{code}</b>" + ("\n📣 Obunachilarga xabar yuborilmoqda..." if n else ""), parse_mode="HTML")
    if n: asyncio.create_task(announce(code))

@dp.message(Command("stats"), is_admin)
async def stats(msg: Message):
    day = int(time.time()) - 86400
    await msg.answer(
        f"👥 Foydalanuvchilar: {q('SELECT COUNT(*) c FROM users')[0]['c']}\n"
        f"🆕 24 soatda: {q('SELECT COUNT(*) c FROM users WHERE joined>?', day)[0]['c']}\n"
        f"🎬 Kinolar: {q('SELECT COUNT(*) c FROM movies')[0]['c']}\n"
        f"👁 Ko'rishlar: {q('SELECT COALESCE(SUM(views),0) c FROM movies')[0]['c']}")

@dp.message(Command("movies"), is_admin)
async def movies(msg: Message):
    rows = q("SELECT code,title FROM movies ORDER BY code DESC LIMIT 30")
    await msg.answer("\n".join(f"{r['code']} — {r['title']}" for r in rows) or "Bo'sh")

@dp.message(Command("delmovie"), is_admin)
async def delmovie(msg: Message, command: CommandObject):
    if command.args and command.args.isdigit():
        run("DELETE FROM movies WHERE code=?", int(command.args)); await msg.answer("🗑 O'chirildi")

@dp.message(Command("addchannel"), is_admin)
async def addch(msg: Message, command: CommandObject):
    a = (command.args or "").split()
    if len(a) != 2: return await msg.answer("/addchannel @kanal https://t.me/kanal")
    run("INSERT OR REPLACE INTO channels VALUES(?,?)", a[0], a[1])
    await msg.answer("✅ Kanal qo'shildi (bot kanalda admin bo'lishi shart)")

@dp.message(Command("delchannel"), is_admin)
async def delch(msg: Message, command: CommandObject):
    run("DELETE FROM channels WHERE chat_id=?", command.args); await msg.answer("🗑 O'chirildi")

@dp.message(Command("broadcast"), is_admin)
async def broadcast(msg: Message):
    if not msg.reply_to_message:
        return await msg.answer("Yubormoqchi bo'lgan xabarga reply qilib /broadcast yozing")
    ok = 0
    for u in q("SELECT id FROM users"):
        try:
            await msg.reply_to_message.copy_to(u["id"]); ok += 1
        except Exception: pass
        await asyncio.sleep(0.05)
    await msg.answer(f"✅ Yuborildi: {ok}")

@dp.message(Command("backup"), is_admin)
async def backup(msg: Message):
    db.commit()
    await msg.answer_document(FSInputFile(DB_PATH), caption="Baza nusxasi")

# ---------- qidiruv ----------
@dp.message(F.text, ~F.text.startswith("/"))
async def search(msg: Message):
    uid = msg.from_user.id
    bad = await missing_channels(uid)
    if bad:
        return await msg.answer("Avval kanallarga obuna bo'ling:", reply_markup=sub_kb(bad))
    t = msg.text.strip()
    if t.isdigit():
        if not await send_movie(uid, int(t)):
            await msg.answer("❌ Bunday kodli kino topilmadi")
        return
    rows = q("SELECT code,title,year FROM movies WHERE title LIKE ? AND file_id!='' LIMIT 10", f"%{t}%")
    if not rows:
        return await msg.answer("❌ Hech narsa topilmadi")
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text=f"{r['title']} ({r['year']})", callback_data=f"get:{r['code']}")] for r in rows])
    await msg.answer("🔎 Topilganlar:", reply_markup=kb)

# ---------- Mini App serveri ----------
def valid_init(init):
    d = dict(parse_qsl(init)); h = d.pop("hash", "")
    s = "\n".join(f"{k}={v}" for k, v in sorted(d.items()))
    key = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(hmac.new(key, s.encode(), hashlib.sha256).hexdigest(), h):
        return None
    return json.loads(d["user"]) if "user" in d else None

async def index(_): return web.FileResponse("webapp/index.html")
async def health(_): return web.Response(text="ok")

async def announce(code):
    m = q("SELECT * FROM movies WHERE code=?", code)[0]
    text = (f"🔥 <b>Yangi kino: {html.escape(m['title'])}</b> ({html.escape(m['year'])})\n"
            f"🎭 {html.escape(m['genre'])}\n\n{html.escape(m['descr'] or '')}\n")
    if m["trailer"]: text += f"\n▶️ Treyler: {m['trailer']}\n"
    text += "\n🍿 Yangi kinoni ko'rishni xohlaysizmi?"
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🍿 Ha, ko'rish", callback_data=f"get:{code}")]])
    ok = 0
    for u in q("SELECT id FROM users"):
        try:
            try:
                if not m["poster"]: raise ValueError
                await bot.send_photo(u["id"], m["poster"], caption=text[:1000], parse_mode="HTML", reply_markup=kb)
            except Exception:
                await bot.send_message(u["id"], text, parse_mode="HTML", reply_markup=kb)
            ok += 1
        except Exception: pass
        await asyncio.sleep(0.05)
    run("UPDATE movies SET notify=0 WHERE code=?", code)
    for a in ADMINS:
        try: await bot.send_message(a, f"📣 «{m['title']}» haqida xabar {ok} kishiga yuborildi")
        except Exception: pass

async def broadcast_text(text):
    for u in q("SELECT id FROM users"):
        try: await bot.send_message(u["id"], text)
        except Exception: pass
        await asyncio.sleep(0.05)

async def api_movies(req):
    u = valid_init(req.headers.get("X-Init", ""))
    adm = bool(u and u["id"] in ADMINS)
    s, g = req.query.get("q", ""), req.query.get("genre", "")
    cond = "" if adm else "AND file_id!=''"
    rows = q(f"SELECT * FROM movies WHERE title LIKE ? AND genre LIKE ? {cond} ORDER BY code DESC LIMIT 100", f"%{s}%", f"%{g}%")
    out = [{"code": r["code"], "title": r["title"], "year": r["year"], "genre": r["genre"],
            "views": r["views"], "descr": r["descr"], "poster": r["poster"], "trailer": r["trailer"],
            "draft": r["file_id"] == "",
            "rating": round(r["r_sum"]/r["r_cnt"], 1) if r["r_cnt"] else 0} for r in rows]
    genres = sorted({x.strip() for r in q("SELECT genre FROM movies WHERE file_id!=''") for x in r["genre"].split(",") if x.strip()})
    return web.json_response({"movies": out, "genres": genres, "admin": adm})

async def api_admin(req):
    b = await req.json()
    u = valid_init(b.get("initData", ""))
    if not u or u["id"] not in ADMINS:
        return web.json_response({"ok": False, "error": "auth"}, status=403)
    a, J = b.get("action"), (lambda **k: web.json_response({"ok": True, **k}))
    if a == "stats":
        day = int(time.time()) - 86400
        return J(users=q("SELECT COUNT(*) c FROM users")[0]["c"],
                 new=q("SELECT COUNT(*) c FROM users WHERE joined>?", day)[0]["c"],
                 movies=q("SELECT COUNT(*) c FROM movies")[0]["c"],
                 views=q("SELECT COALESCE(SUM(views),0) c FROM movies")[0]["c"],
                 channels=[dict(r) for r in q("SELECT * FROM channels")])
    if a == "save":
        f = [str(b.get(k, "")).strip() for k in ("title", "year", "genre", "descr", "poster", "trailer")]
        if not f[0]: return web.json_response({"ok": False, "error": "Nomi kerak"})
        n = 1 if b.get("notify") else 0
        if b.get("code"):
            run("UPDATE movies SET title=?,year=?,genre=?,descr=?,poster=?,trailer=?,notify=? WHERE code=?", *f, n, int(b["code"]))
            return J(code=int(b["code"]), need_video=False)
        c = run("INSERT INTO movies(title,year,genre,descr,poster,trailer,notify,file_id,ftype) VALUES(?,?,?,?,?,?,?,'','video')", *f, n)
        run("INSERT OR REPLACE INTO pending VALUES(?,?)", u["id"], c.lastrowid)
        try: await bot.send_message(u["id"], f"✅ «{f[0]}» saqlandi (kod {c.lastrowid}).\n🎥 Endi shu yerga kino videosini yuboring.")
        except Exception: pass
        return J(code=c.lastrowid, need_video=True)
    if a == "video":
        run("INSERT OR REPLACE INTO pending VALUES(?,?)", u["id"], int(b["code"]))
        await bot.send_message(u["id"], "🎥 Endi shu yerga kino videosini yuboring.")
        return J()
    if a == "delete":
        run("DELETE FROM movies WHERE code=?", int(b["code"])); return J()
    if a == "notify":
        run("UPDATE movies SET notify=1 WHERE code=?", int(b["code"]))
        asyncio.create_task(announce(int(b["code"]))); return J()
    if a == "addch":
        run("INSERT OR REPLACE INTO channels VALUES(?,?)", str(b["chat_id"]).strip(), str(b["link"]).strip()); return J()
    if a == "delch":
        run("DELETE FROM channels WHERE chat_id=?", b["chat_id"]); return J()
    if a == "broadcast":
        asyncio.create_task(broadcast_text(str(b["text"]))); return J()
    return web.json_response({"ok": False, "error": "unknown"})

async def api_send(req):
    body = await req.json()
    user = valid_init(body.get("initData", ""))
    if not user: return web.json_response({"ok": False, "error": "auth"}, status=403)
    if await missing_channels(user["id"]):
        return web.json_response({"ok": False, "error": "sub"})
    return web.json_response({"ok": await send_movie(user["id"], int(body["code"]))})

CACHE = {}

async def api_play(req):
    b = await req.json()
    u = valid_init(b.get("initData", ""))
    if not u: return web.json_response({"ok": False, "error": "auth"}, status=403)
    if await missing_channels(u["id"]):
        return web.json_response({"ok": False, "error": "sub"})
    code = int(b["code"])
    m = q("SELECT file_id FROM movies WHERE code=?", code)
    if not m or not m[0]["file_id"]: return web.json_response({"ok": False, "error": "none"})
    try:
        await bot.get_file(m[0]["file_id"])
    except Exception:
        return web.json_response({"ok": False, "error": "big"})
    run("UPDATE movies SET views=views+1 WHERE code=?", code)
    return web.json_response({"ok": True, "url": f"/api/stream?code={code}&init={quote(b['initData'])}"})

async def api_stream(req):
    if not valid_init(req.query.get("init", "")): return web.Response(status=403)
    code = int(req.query["code"])
    if code not in CACHE:
        m = q("SELECT file_id FROM movies WHERE code=?", code)
        if not m: return web.Response(status=404)
        buf = io.BytesIO()
        await bot.download(m[0]["file_id"], destination=buf)
        if len(CACHE) >= 2: CACHE.pop(next(iter(CACHE)))
        CACHE[code] = buf.getvalue()
    data = CACHE[code]; size = len(data)
    start, end, status = 0, size - 1, 200
    rng = req.headers.get("Range")
    if rng and rng.startswith("bytes="):
        a, _, z = rng[6:].partition("-")
        start = int(a) if a else 0
        end = min(int(z), size - 1) if z else size - 1
        status = 206
    h = {"Accept-Ranges": "bytes", "Content-Type": "video/mp4"}
    if status == 206: h["Content-Range"] = f"bytes {start}-{end}/{size}"
    return web.Response(body=data[start:end + 1], status=status, headers=h)

async def main():
    app = web.Application()
    app.add_routes([web.get("/", index), web.get("/health", health),
                    web.get("/api/movies", api_movies), web.post("/api/admin", api_admin), web.post("/api/send", api_send),
                    web.post("/api/play", api_play), web.get("/api/stream", api_stream)])
    runner = web.AppRunner(app); await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
