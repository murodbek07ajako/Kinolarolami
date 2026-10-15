"""Mini App serveri: statik sahifa + JSON API (Telegram initData bilan himoyalangan)."""
import hashlib
import hmac
import html
import json
import logging
import re
import time
from urllib.parse import parse_qsl

from aiohttp import web

import config
import db
import broadcast
import services as svc

SETTING_DEFAULTS = {"protect_content": "0", "sub_required": "1"}
DRAFT_LIMITS = {"title": 120, "description": 800, "year": 4, "genre": 100, "country": 60, "language": 60, "quality": 20}


def verify_init_data(init_data: str, token: str, max_age: int = 7 * 86400) -> dict | None:
    """Telegram WebApp initData imzosini tekshiradi. To'g'ri bo'lsa user dict qaytaradi."""
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        recv_hash = pairs.pop("hash", None)
        if not recv_hash:
            return None
        check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
        secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, recv_hash):
            return None
        if time.time() - int(pairs.get("auth_date", "0")) > max_age:
            return None
        user = json.loads(pairs["user"])
        return user if isinstance(user, dict) and "id" in user else None
    except Exception:
        return None


def _auth(request: web.Request) -> dict:
    user = verify_init_data(request.headers.get("X-Init-Data", ""), config.BOT_TOKEN)
    if not user:
        raise web.HTTPUnauthorized(reason="auth")
    return user


def _admin(request: web.Request) -> dict:
    """Faqat ADMIN_IDS dagilar uchun (initData imzosi tekshirilgan foydalanuvchi bo'yicha)."""
    user = _auth(request)
    if user["id"] not in config.ADMIN_IDS:
        raise web.HTTPForbidden(reason="admin")
    return user


def _bad(msg: str) -> web.Response:
    return web.json_response({"error": msg}, status=400)


def _brief(r) -> dict:
    return {
        "id": r["id"], "title": r["title"], "year": r["year"], "genre": r["genre"],
        "country": r["country"], "quality": r["quality"], "views": r["views"],
        "poster": bool(r["poster_id"]),
    }


@web.middleware
async def errors(request: web.Request, handler):
    try:
        return await handler(request)
    except web.HTTPException as e:
        if request.path.startswith("/api/"):
            return web.json_response({"error": e.reason}, status=e.status)
        raise
    except ValueError:
        return web.json_response({"error": "bad request"}, status=400)
    except Exception:
        logging.exception("API xatosi: %s", request.path)
        return web.json_response({"error": "server"}, status=500)


# ───────────── Sahifalar ─────────────
async def index(request: web.Request):
    return web.FileResponse(config.WEBAPP_DIR / "index.html", headers={"Cache-Control": "no-cache"})


async def health(request: web.Request):
    return web.json_response({"ok": True})


# ───────────── Foydalanuvchi API ─────────────
async def api_me(request: web.Request):
    user = _auth(request)
    name = (user.get("first_name") or "").strip() or "do'st"
    return web.json_response({
        "name": name,
        "favs": await db.fav_ids(user["id"]),
        "admin": user["id"] in config.ADMIN_IDS,
    })


async def api_movies(request: web.Request):
    user = _auth(request)
    q = request.query
    sort = q.get("sort", "new")
    offset = max(int(q.get("offset", "0") or 0), 0)
    limit = min(max(int(q.get("limit", "20") or 20), 1), 50)
    fav_user = user["id"] if sort == "fav" else None
    rows, total = await db.search_movies(
        q.get("q", "")[:80], q.get("genre", "")[:40], "new" if sort == "fav" else sort,
        limit, offset, fav_user,
    )
    data = {"items": [_brief(r) for r in rows], "total": total}
    if offset == 0:
        data["genres"] = await db.genres()
    return web.json_response(data)


async def api_movie(request: web.Request):
    user = _auth(request)
    movie = await db.get_movie(int(request.match_info["id"]))
    if not movie or (movie["status"] != "live" and user["id"] not in config.ADMIN_IDS):
        raise web.HTTPNotFound(reason="not found")
    d = _brief(movie)
    d["description"] = movie["description"]
    d["language"] = movie["language"]
    return web.json_response(d)


async def api_send(request: web.Request):
    user = _auth(request)
    body = await request.json()
    mid = int(body.get("id", 0))
    status = await svc.deliver(request.app["bot"], user["id"], user["id"], mid)
    return web.json_response({"ok": status == "ok", "status": status})


async def api_fav(request: web.Request):
    user = _auth(request)
    body = await request.json()
    mid = int(body.get("id", 0))
    if not await db.get_movie(mid):
        raise web.HTTPNotFound(reason="not found")
    return web.json_response({"fav": await db.toggle_fav(user["id"], mid)})


async def api_poster(request: web.Request):
    mid = int(request.match_info["id"])
    movie = await db.get_movie(mid)
    if not movie or not movie["poster_id"]:
        raise web.HTTPNotFound(reason="no poster")
    config.POSTER_DIR.mkdir(parents=True, exist_ok=True)
    key = hashlib.md5(movie["poster_id"].encode()).hexdigest()
    path = config.POSTER_DIR / f"{key}.jpg"
    if not path.exists():
        tmp = path.with_suffix(".part")
        await request.app["bot"].download(movie["poster_id"], destination=tmp)
        tmp.replace(path)
    return web.FileResponse(path, headers={"Cache-Control": "public, max-age=86400", "Content-Type": "image/jpeg"})


# ───────────── Admin API ─────────────
async def api_admin_stats(request: web.Request):
    _admin(request)
    s = await db.stats()
    s["top"] = [{"id": t["id"], "title": t["title"], "views": t["views"]} for t in s["top"]]
    return web.json_response(s)


async def _settings_payload() -> dict:
    out = {k: (await db.get_setting(k, default)) == "1" for k, default in SETTING_DEFAULTS.items()}
    out["channels"] = [
        {"id": r["id"], "title": r["title"] or str(r["chat_id"]), "link": r["link"]}
        for r in await db.list_channels()
    ]
    out["sources"] = [
        {"id": r["chat_id"], "title": r["title"] or str(r["chat_id"])}
        for r in await db.list_sources()
    ]
    return out


async def api_admin_settings(request: web.Request):
    _admin(request)
    return web.json_response(await _settings_payload())


async def api_admin_setting(request: web.Request):
    _admin(request)
    body = await request.json()
    key = body.get("key")
    if key not in SETTING_DEFAULTS:
        return _bad("Noma'lum sozlama")
    await db.set_setting(key, "1" if body.get("value") else "0")
    return web.json_response({"ok": True})


async def api_admin_channel_add(request: web.Request):
    _admin(request)
    body = await request.json()
    kind = body.get("kind")
    ref = svc.parse_ref(str(body.get("ref", "")))
    if kind not in ("sub", "source") or ref is None:
        return _bad("Kanalni @username yoki -100... ID ko'rinishida yozing")
    try:
        chat_id, title, link = await svc.resolve_channel(
            request.app["bot"], ref, need_link=(kind == "sub"), channel_only=(kind == "source")
        )
    except ValueError as e:
        return _bad(str(e))
    if kind == "sub":
        await db.add_channel(chat_id, title, link)
    else:
        await db.add_source(chat_id, title)
    return web.json_response(await _settings_payload())


async def api_admin_channel_del(request: web.Request):
    _admin(request)
    body = await request.json()
    kind, ident = body.get("kind"), int(body.get("id", 0))
    if kind == "sub":
        await db.del_channel(ident)
    elif kind == "source":
        await db.del_source(ident)
    else:
        return _bad("Noma'lum tur")
    return web.json_response(await _settings_payload())


async def api_admin_pending(request: web.Request):
    _admin(request)
    return web.json_response({"items": [
        {"id": r["id"], "title": r["title"], "year": r["year"], "genre": r["genre"]}
        for r in await db.pending_movies()
    ]})


async def api_admin_publish(request: web.Request):
    """Kutilayotgan hamma kinoni chiqaradi; notify=true bo'lsa foydalanuvchilarga bitta umumiy xabar ketadi."""
    _admin(request)
    body = await request.json()
    n = await broadcast.publish_all(request.app["bot"], bool(body.get("notify")))
    return web.json_response({"published": n})


async def api_admin_movie_get(request: web.Request):
    _admin(request)
    m = await db.get_movie(int(request.match_info["id"]))
    if not m:
        raise web.HTTPNotFound(reason="not found")
    return web.json_response({"movie": {"id": m["id"], **{k: m[k] or "" for k in db.META_FIELDS}}})


async def api_admin_movie_update(request: web.Request):
    _admin(request)
    body = await request.json()
    mid = int(body.get("id", 0))
    if not await db.get_movie(mid):
        raise web.HTTPNotFound(reason="not found")
    try:
        d = _clean_draft(body)
    except ValueError as e:
        return _bad(str(e))
    await db.replace_movie_meta(mid, d)
    return web.json_response({"ok": True})


async def api_admin_movie_del(request: web.Request):
    _admin(request)
    body = await request.json()
    mid = int(body.get("id", 0))
    if not await db.get_movie(mid):
        raise web.HTTPNotFound(reason="not found")
    await db.delete_movie(mid)
    return web.json_response({"ok": True})


def _clean_draft(body: dict) -> dict:
    out = {}
    for key, limit in DRAFT_LIMITS.items():
        value = re.sub(r"[ \t]+", " ", str(body.get(key) or "")).strip()[:limit]
        if value:
            out[key] = value
    if not out.get("title"):
        raise ValueError("Kino nomini kiriting")
    if out.get("year") and not re.fullmatch(r"(18|19|20)\d{2}", out["year"]):
        raise ValueError("Yil noto'g'ri (masalan: 2024)")
    if out.get("genre"):
        out["genre"] = ", ".join(g.strip() for g in out["genre"].split(",") if g.strip())
    return out


async def api_admin_draft_get(request: web.Request):
    user = _admin(request)
    return web.json_response({"draft": await db.get_draft(user["id"])})


async def api_admin_draft_save(request: web.Request):
    """Mini App'da kiritilgan ma'lumotlarni saqlaydi va botda adminga «endi videoni yuboring» deb yozadi."""
    user = _admin(request)
    try:
        draft = _clean_draft(await request.json())
    except ValueError as e:
        return _bad(str(e))
    await db.save_draft(user["id"], draft)
    title = html.escape(draft["title"]) + (f" ({draft['year']})" if draft.get("year") else "")
    text = (
        "✅ <b>Kino ma'lumotlari saqlandi!</b>\n\n"
        f"🎬 <b>{title}</b>\n\n"
        "📤 Endi <b>kinoni (video yoki fayl)</b> shu yerga yuboring — bot uni avtomatik qo'shadi.\n"
        "🖼 Xohlasangiz avval <b>poster rasmini</b> yuboring (yubormasangiz videoning kadri olinadi).\n"
        "❌ Bekor qilish: /cancel"
    )
    notified = True
    try:
        await request.app["bot"].send_message(user["id"], text)
    except Exception as e:
        notified = False
        logging.warning("Draft xabari yuborilmadi: %s", e)
    return web.json_response({"ok": True, "notified": notified})


async def api_admin_draft_clear(request: web.Request):
    user = _admin(request)
    await db.clear_draft(user["id"])
    return web.json_response({"ok": True})


def make_app(bot) -> web.Application:
    app = web.Application(middlewares=[errors])
    app["bot"] = bot
    r = app.router
    r.add_get("/", index)
    r.add_get("/health", health)
    r.add_get("/api/me", api_me)
    r.add_get("/api/movies", api_movies)
    r.add_get(r"/api/movie/{id:\d+}", api_movie)
    r.add_post("/api/send", api_send)
    r.add_post("/api/fav", api_fav)
    r.add_get(r"/api/poster/{id:\d+}", api_poster)
    # admin
    r.add_get("/api/admin/stats", api_admin_stats)
    r.add_get("/api/admin/settings", api_admin_settings)
    r.add_post("/api/admin/setting", api_admin_setting)
    r.add_post("/api/admin/channel", api_admin_channel_add)
    r.add_post("/api/admin/channel/delete", api_admin_channel_del)
    r.add_get("/api/admin/pending", api_admin_pending)
    r.add_post("/api/admin/publish", api_admin_publish)
    r.add_get(r"/api/admin/movie/{id:\d+}", api_admin_movie_get)
    r.add_post("/api/admin/movie/update", api_admin_movie_update)
    r.add_post("/api/admin/movie/delete", api_admin_movie_del)
    r.add_get("/api/admin/draft", api_admin_draft_get)
    r.add_post("/api/admin/draft", api_admin_draft_save)
    r.add_post("/api/admin/draft/clear", api_admin_draft_clear)
    return app


async def start(bot, port: int) -> web.AppRunner:
    runner = web.AppRunner(make_app(bot))
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    return runner
