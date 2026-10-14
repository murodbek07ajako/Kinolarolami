"""Mini App serveri: statik sahifa + JSON API (Telegram initData bilan himoyalangan)."""
import hashlib
import hmac
import json
import logging
import time
from urllib.parse import parse_qsl

from aiohttp import web

import config
import db
import services as svc


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


# ───────────── API ─────────────
async def api_me(request: web.Request):
    user = _auth(request)
    name = (user.get("first_name") or "").strip() or "do'st"
    return web.json_response({"name": name, "favs": await db.fav_ids(user["id"])})


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
    _auth(request)
    movie = await db.get_movie(int(request.match_info["id"]))
    if not movie:
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


def make_app(bot) -> web.Application:
    app = web.Application(middlewares=[errors])
    app["bot"] = bot
    app.router.add_get("/", index)
    app.router.add_get("/health", health)
    app.router.add_get("/api/me", api_me)
    app.router.add_get("/api/movies", api_movies)
    app.router.add_get(r"/api/movie/{id:\d+}", api_movie)
    app.router.add_post("/api/send", api_send)
    app.router.add_post("/api/fav", api_fav)
    app.router.add_get(r"/api/poster/{id:\d+}", api_poster)
    return app


async def start(bot, port: int) -> web.AppRunner:
    runner = web.AppRunner(make_app(bot))
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    return runner
