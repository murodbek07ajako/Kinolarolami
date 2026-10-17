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
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BufferedInputFile

import config
import db
import broadcast
import perms
import services as svc
import theatre

SETTING_DEFAULTS = {"protect_content": "0", "sub_required": "1", "theatre_on": "1"}
THEMES = ("default", "newyear", "ramadan", "navruz")  # dizayn rejimlari


async def _theme() -> str:
    t = await db.get_setting("theme", "default")
    return t if t in THEMES else "default"
SINGLE_KINDS = ("kino", "multfilm", "anime")
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


def _admin(request: web.Request, *need: str) -> dict:
    """Admin (bosh/starshiy/oddiy) uchun. `need` berilsa — shulardan kamida bittasi huquqda bo'lishi kerak."""
    user = _auth(request)
    if not perms.is_staff(user["id"]):
        raise web.HTTPForbidden(reason="admin")
    if need and not perms.has_any(user["id"], *need):
        raise web.HTTPForbidden(reason="Sizda bu amal uchun ruxsat yo'q")
    return user


def _kind_ok(user: dict, kind: str) -> None:
    """Shu bo'limga (kino/serial/anime/multfilm) ruxsat bo'lmasa — 403."""
    if not perms.can_kind(user["id"], kind):
        raise web.HTTPForbidden(reason=f"Sizda «{svc.KIND_NAME.get(kind, kind)}» bo'limiga ruxsat yo'q")


def _manager(request: web.Request) -> dict:
    """Adminlarni boshqarish: faqat bosh va starshiy adminlar."""
    user = _auth(request)
    if not perms.can_manage(user["id"]):
        raise web.HTTPForbidden(reason="Adminlarni faqat starshiy admin boshqara oladi")
    return user


def _bad(msg: str) -> web.Response:
    return web.json_response({"error": msg}, status=400)


def _cv(cover_id: str) -> str:
    """Muqova versiyasi (brauzer keshini yangilash uchun). Muqova yo'q bo'lsa — bo'sh."""
    return hashlib.md5(cover_id.encode()).hexdigest()[:8] if cover_id else ""


def _brief(r) -> dict:
    """Katalog kartochkasi. t='m' — kino, t='s' — qismli (serial/multfilm/anime); fk — saralanganlar kaliti."""
    t = r["t"] if "t" in r.keys() else "m"
    return {
        "t": t, "id": r["id"], "fk": r["fk"] if "fk" in r.keys() else r["id"],
        "kind": r["kind"], "code": db.series_code(r["kind"], r["id"]) if t == "s"
        else db.movie_code(r),
        "eps": r["eps"] if "eps" in r.keys() else 0,
        "title": r["title"], "year": r["year"], "genre": r["genre"],
        "country": r["country"], "quality": r["quality"], "views": r["views"],
        "poster": bool(r["poster_id"]),
        "cv": _cv(r["cover_id"]) if "cover_id" in r.keys() else "",
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


def _contact_link() -> str:
    """«Adminga murojaat» tugmasi ochadigan havola."""
    if config.ADMIN_CONTACT:
        return f"https://t.me/{config.ADMIN_CONTACT}"
    if config.ADMIN_IDS:
        return f"tg://user?id={sorted(config.ADMIN_IDS)[0]}"
    return ""


# ───────────── Umumiy chat ─────────────
CHAT_MAX_LEN = 500
CHAT_COOLDOWN = 2  # soniya: bir foydalanuvchi ketma-ket yozishi orasidagi eng kam vaqt


def _chat_item(r, viewer_id: int) -> dict:
    return {"id": r["id"], "uid": r["user_id"], "name": r["name"] or "Foydalanuvchi", "text": r["text"],
            "at": r["at"], "mine": r["user_id"] == viewer_id, "staff": perms.is_staff(r["user_id"])}


async def api_chat_list(request: web.Request):
    user = _auth(request)
    after = max(int(request.query.get("after", "0") or 0), 0)
    rows = await db.chat_list(after, 50)
    return web.json_response({"items": [_chat_item(r, user["id"]) for r in rows]})


async def api_chat_send(request: web.Request):
    user = _auth(request)
    if await db.is_banned(user["id"]):
        raise web.HTTPForbidden(reason="Siz chatga yoza olmaysiz")
    body = await request.json()
    text = re.sub(r"[ \t]+", " ", str(body.get("text") or "")).strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    if not text:
        return _bad("Xabar yozing")
    if len(text) > CHAT_MAX_LEN:
        return _bad(f"Xabar {CHAT_MAX_LEN} belgidan oshmasligi kerak")
    if time.time() - await db.chat_last_at(user["id"]) < CHAT_COOLDOWN:
        return web.json_response({"error": "Biroz sekinroq yozing 🙂"}, status=429)
    name = " ".join(x for x in (user.get("first_name"), user.get("last_name")) if x).strip()
    name = (name or (f"@{user['username']}" if user.get("username") else "") or "Foydalanuvchi")[:40]
    msg = await db.chat_add(user["id"], name, text)
    return web.json_response({"ok": True, "item": {**msg, "mine": True, "staff": perms.is_staff(user["id"])}})


async def api_chat_delete(request: web.Request):
    _admin(request)
    body = await request.json()
    await db.chat_delete(int(body.get("id", 0)))
    return web.json_response({"ok": True})


# ───────────── Foydalanuvchi API ─────────────
async def api_me(request: web.Request):
    user = _auth(request)
    name = (user.get("first_name") or "").strip() or "do'st"
    return web.json_response({
        "name": name,
        "theme": await _theme(),
        "favs": await db.fav_ids(user["id"]),
        "uid": user["id"],
        "theatre": theatre.is_on(),
        "theatre_ready": theatre.available(),
        "admin": perms.is_staff(user["id"]),
        "contact": _contact_link(),
        **perms.info(user["id"]),
    })


async def api_movies(request: web.Request):
    user = _auth(request)
    q = request.query
    sort = q.get("sort", "new")
    offset = max(int(q.get("offset", "0") or 0), 0)
    limit = min(max(int(q.get("limit", "20") or 20), 1), 50)
    fav_user = user["id"] if sort == "fav" else None
    rows, total = await db.search_items(
        q.get("q", "")[:80], q.get("genre", "")[:40], "new" if sort == "fav" else sort,
        limit, offset, fav_user, q.get("kind", ""),
    )
    data = {"items": [_brief(r) for r in rows], "total": total}
    if offset == 0:
        data["genres"] = await db.genres()
    return web.json_response(data)


async def api_movie(request: web.Request):
    user = _auth(request)
    movie = await db.get_movie(int(request.match_info["id"]))
    if not movie or (movie["status"] != "live" and not perms.is_staff(user["id"])):
        raise web.HTTPNotFound(reason="not found")
    d = _brief(movie)
    d["description"] = movie["description"]
    d["language"] = movie["language"]
    return web.json_response(d)


async def api_series(request: web.Request):
    """Qismli kontent kartochkasi: ma'lumot + chiqarilgan qismlar ro'yxati."""
    user = _auth(request)
    ser = await db.get_series(int(request.match_info["id"]))
    if not ser:
        raise web.HTTPNotFound(reason="not found")
    is_admin = perms.is_staff(user["id"])
    eps = await db.episodes(ser["id"], live_only=not is_admin)
    if not eps:
        raise web.HTTPNotFound(reason="not found")
    watched = await db.watched_episodes(user["id"], ser["id"])
    return web.json_response({
        "last": watched[0]["mid"] if watched else None,
        "seen": [w["mid"] for w in watched],
        "t": "s", "id": ser["id"], "fk": -ser["id"], "kind": ser["kind"],
        "code": db.series_code(ser["kind"], ser["id"]),
        "title": ser["title"], "year": ser["year"], "genre": ser["genre"], "country": ser["country"],
        "language": ser["language"], "description": ser["description"], "quality": "",
        "views": sum(e["views"] for e in eps), "poster": bool(ser["poster_id"]),
        "cv": _cv(ser["cover_id"]),
        "eps": [{"id": e["id"], "ep": e["ep"], "views": e["views"], "live": e["status"] == "live"} for e in eps],
    })


async def api_hero(request: web.Request):
    """Katalog tepasidagi slayder (5 ta)."""
    _auth(request)
    rows = await db.hero_items(5)
    return web.json_response({"items": [_brief(r) for r in rows]})


async def api_similar(request: web.Request):
    """Kino oynasidagi «O'xshash kontent»."""
    _auth(request)
    t, iid = request.match_info["t"], int(request.match_info["id"])
    src = await (db.get_series(iid) if t == "s" else db.get_movie(iid))
    if not src:
        raise web.HTTPNotFound(reason="not found")
    rows = await db.similar_items(t, iid, src["kind"], src["genre"], src["year"], 8)
    return web.json_response({"items": [_brief(r) for r in rows]})


async def api_send(request: web.Request):
    user = _auth(request)
    body = await request.json()
    mid = int(body.get("id", 0))
    status = await svc.deliver(request.app["bot"], user["id"], user["id"], mid)
    return web.json_response({"ok": status == "ok", "status": status})


async def api_fav(request: web.Request):
    user = _auth(request)
    body = await request.json()
    key = int(body.get("id", 0))  # kino: musbat id, qismli kontent: manfiy (-serial id)
    exists = await db.get_series(-key) if key < 0 else await db.get_movie(key)
    if not exists:
        raise web.HTTPNotFound(reason="not found")
    return web.json_response({"fav": await db.toggle_fav(user["id"], key)})


async def _poster_response(request: web.Request, poster_id: str):
    config.POSTER_DIR.mkdir(parents=True, exist_ok=True)
    key = hashlib.md5(poster_id.encode()).hexdigest()
    path = config.POSTER_DIR / f"{key}.jpg"
    if not path.exists():
        tmp = path.with_suffix(".part")
        await request.app["bot"].download(poster_id, destination=tmp)
        tmp.replace(path)
    return web.FileResponse(path, headers={"Cache-Control": "public, max-age=86400", "Content-Type": "image/jpeg"})


async def api_poster(request: web.Request):
    movie = await db.get_movie(int(request.match_info["id"]))
    if not movie or not movie["poster_id"]:
        raise web.HTTPNotFound(reason="no poster")
    return await _poster_response(request, movie["poster_id"])


async def api_sposter(request: web.Request):
    ser = await db.get_series(int(request.match_info["id"]))
    if not ser or not ser["poster_id"]:
        raise web.HTTPNotFound(reason="no poster")
    return await _poster_response(request, ser["poster_id"])


async def api_cover(request: web.Request):
    movie = await db.get_movie(int(request.match_info["id"]))
    if not movie or not movie["cover_id"]:
        raise web.HTTPNotFound(reason="no cover")
    return await _poster_response(request, movie["cover_id"])


async def api_scover(request: web.Request):
    ser = await db.get_series(int(request.match_info["id"]))
    if not ser or not ser["cover_id"]:
        raise web.HTTPNotFound(reason="no cover")
    return await _poster_response(request, ser["cover_id"])


async def logo(request: web.Request):
    """Muqova qo'yilmagan joylarda ko'rinadigan logo."""
    return web.FileResponse(config.WEBAPP_DIR / "logo.png", headers={"Cache-Control": "public, max-age=604800"})


# ───────────── Admin API ─────────────
async def api_admin_stats(request: web.Request):
    _admin(request, "stats")
    s = await db.stats()
    s["top"] = [{"id": t["id"], "code": db.movie_code(t), "title": t["title"], "views": t["views"]} for t in s["top"]]
    return web.json_response(s)


async def _settings_payload() -> dict:
    out = {k: (await db.get_setting(k, default)) == "1" for k, default in SETTING_DEFAULTS.items()}
    out["theme"] = await _theme()
    out["channels"] = [
        {"id": r["id"], "title": r["title"] or str(r["chat_id"]), "link": r["link"]}
        for r in await db.list_channels()
    ]
    out["sources"] = []
    for r in await db.list_sources():
        ser = await db.get_series(r["series_id"]) if r["series_id"] else None
        out["sources"].append({
            "id": r["chat_id"], "title": r["title"] or str(r["chat_id"]), "kind": r["kind"],
            "series_id": r["series_id"],
            "series": ({"title": ser["title"], "code": db.series_code(ser["kind"], ser["id"])} if ser else None),
        })
    return out


async def api_admin_settings(request: web.Request):
    _admin(request, "settings", "channels")
    return web.json_response(await _settings_payload())


async def api_admin_setting(request: web.Request):
    _admin(request, "settings")
    body = await request.json()
    key = body.get("key")
    if key not in SETTING_DEFAULTS:
        return _bad("Noma'lum sozlama")
    await db.set_setting(key, "1" if body.get("value") else "0")
    if key == "theatre_on":
        theatre.set_enabled(bool(body.get("value")))
    return web.json_response({"ok": True})


async def api_admin_theme(request: web.Request):
    """Dizayn rejimini tanlash — hamma foydalanuvchida shu dizayn ko'rinadi."""
    _admin(request, "settings")
    body = await request.json()
    theme = body.get("theme")
    if theme not in THEMES:
        return _bad("Noma'lum dizayn")
    await db.set_setting("theme", theme)
    return web.json_response({"ok": True, "theme": theme})


async def api_admin_channel_add(request: web.Request):
    """Majburiy obuna kanali qo'shish."""
    _admin(request, "channels")
    body = await request.json()
    ref = svc.parse_ref(str(body.get("ref", "")))
    if body.get("kind") != "sub" or ref is None:
        return _bad("Kanalni @username yoki -100... ID ko'rinishida yozing")
    try:
        chat_id, title, link = await svc.resolve_channel(request.app["bot"], ref, need_link=True)
    except ValueError as e:
        return _bad(str(e))
    await db.add_channel(chat_id, title, link)
    return web.json_response(await _settings_payload())


async def api_admin_source_add(request: web.Request):
    """Avto-yuklash kanalini ulaydi. kind: kino|multfilm|anime|serial; parts=true yoki serial → qismli
    (bunda serial ma'lumotlari shu yerda bir marta kiritiladi, qismlar kanaldan raqam bo'yicha olinadi)."""
    user = _admin(request, "channels")
    body = await request.json()
    ref = svc.parse_ref(str(body.get("ref", "")))
    kind = body.get("kind")
    if kind not in db.KINDS:
        return _bad("Turini tanlang")
    _kind_ok(user, kind)
    if ref is None:
        return _bad("Kanalni @username yoki -100... ID ko'rinishida yozing")
    parts = bool(body.get("parts")) or kind == "serial"
    meta = None
    if parts:
        try:
            meta = _clean_meta(body)
        except ValueError as e:
            return _bad(str(e))
    try:
        chat_id, title, _ = await svc.resolve_channel(request.app["bot"], ref, need_link=False, channel_only=True)
    except ValueError as e:
        return _bad(str(e))
    if parts:
        old = await db.get_source(chat_id)
        if old and old["series_id"] and await db.get_series(old["series_id"]):
            return _bad("Bu kanal allaqachon boshqa serialga ulangan. Avval uni olib tashlang.")
        sid = await db.add_series({**meta, "kind": kind})
        await db.add_source(chat_id, title, kind, sid)
    else:
        await db.add_source(chat_id, title, kind, None)
    return web.json_response(await _settings_payload())


async def api_admin_channel_del(request: web.Request):
    _admin(request, "channels")
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
    """Qo'shishga tayyor kontent: guruhlar (kino/multfilm/anime ro'yxatlari va har serial alohida)."""
    user = _admin(request, "publish")
    groups = []
    for g in await db.pending_groups():
        if not perms.can_kind(user["id"], g["kind"]):
            continue
        ser = g["series"]
        groups.append({
            "key": g["key"], "kind": g["kind"], "count": len(g["rows"]),
            "series": ({"id": ser["id"], "title": ser["title"], "code": db.series_code(ser["kind"], ser["id"])}
                       if ser else None),
            "items": [{"id": r["id"], "code": db.movie_code(r), "title": r["title"], "year": r["year"], "genre": r["genre"], "ep": r["ep"]}
                      for r in g["rows"]],
        })
    return web.json_response({"groups": groups, "total": sum(g["count"] for g in groups)})


async def api_admin_publish(request: web.Request):
    """Kutilayotganlarni chiqaradi. scope: all | single | single:<tur> | s:<serial id>.
    notify=true bo'lsa foydalanuvchilarga bitta umumiy xabar ketadi."""
    user = _admin(request, "publish")
    body = await request.json()
    bot, notify, scope = request.app["bot"], bool(body.get("notify")), str(body.get("scope") or "all")
    try:
        if perms.is_full(user["id"]):
            n = await broadcast.publish_all(bot, notify, scope)
        else:  # cheklangan admin faqat o'z bo'limlaridagilarni chiqaradi
            keys = [g["key"] for g in await db.pending_groups() if perms.can_kind(user["id"], g["kind"])]
            if scope == "all":
                targets = keys
            elif scope == "single":
                targets = [k for k in keys if k.startswith("single:")]
            elif scope in keys:
                targets = [scope]
            else:
                return _bad("Bu bo'limga ruxsat yo'q yoki kutilayotgan yo'q")
            n = 0
            for k in targets:
                n += await broadcast.publish_all(bot, notify, k)
    except ValueError:
        return _bad("Noto'g'ri doira")
    return web.json_response({"published": n})


async def api_admin_movie_get(request: web.Request):
    user = _admin(request, "add")
    m = await db.get_movie(int(request.match_info["id"]))
    if not m:
        raise web.HTTPNotFound(reason="not found")
    _kind_ok(user, m["kind"])
    return web.json_response({"movie": {"id": m["id"], "code": db.movie_code(m), **{k: m[k] or "" for k in db.META_FIELDS}, "cv": _cv(m["cover_id"])}})


async def api_admin_series_get(request: web.Request):
    user = _admin(request, "add")
    ser = await db.get_series(int(request.match_info["id"]))
    if not ser:
        raise web.HTTPNotFound(reason="not found")
    _kind_ok(user, ser["kind"])
    eps = await db.episodes(ser["id"], live_only=False)
    return web.json_response({
        "series": {"id": ser["id"], "kind": ser["kind"], "code": db.series_code(ser["kind"], ser["id"]),
                   **{k: (ser[k] if k in ser.keys() else "") or "" for k in db.META_FIELDS}, "cv": _cv(ser["cover_id"])},
        "eps": [{"id": e["id"], "ep": e["ep"], "live": e["status"] == "live"} for e in eps],
    })


async def api_admin_series_update(request: web.Request):
    user = _admin(request, "add")
    body = await request.json()
    sid = int(body.get("id", 0))
    ser = await db.get_series(sid)
    if not ser:
        raise web.HTTPNotFound(reason="not found")
    _kind_ok(user, ser["kind"])
    try:
        d = _clean_meta(body)
    except ValueError as e:
        return _bad(str(e))
    await db.update_series_meta(sid, d)
    if "cover" in d:
        await db.set_series_cover(sid, d["cover"])
    return web.json_response({"ok": True})


async def api_admin_series_del(request: web.Request):
    user = _admin(request, "delete")
    body = await request.json()
    sid = int(body.get("id", 0))
    ser = await db.get_series(sid)
    if not ser:
        raise web.HTTPNotFound(reason="not found")
    _kind_ok(user, ser["kind"])
    await db.delete_series(sid)
    return web.json_response({"ok": True})


async def api_admin_series_list(request: web.Request):
    """Qo'lda qism qo'shishda serial tanlash ro'yxati."""
    user = _admin(request, "add")
    return web.json_response({"items": [
        {"id": r["id"], "kind": r["kind"], "code": db.series_code(r["kind"], r["id"]), "title": r["title"],
         "eps": r["eps"], "last_ep": r["last_ep"] or 0}
        for r in await db.list_series() if perms.can_kind(user["id"], r["kind"])
    ]})


async def api_admin_movie_update(request: web.Request):
    user = _admin(request, "add")
    body = await request.json()
    mid = int(body.get("id", 0))
    mv = await db.get_movie(mid)
    if not mv:
        raise web.HTTPNotFound(reason="not found")
    _kind_ok(user, mv["kind"])
    try:
        d = _clean_meta(body)
    except ValueError as e:
        return _bad(str(e))
    await db.replace_movie_meta(mid, d)
    if "cover" in d:
        await db.set_cover(mid, d["cover"])
    return web.json_response({"ok": True})


async def api_admin_movie_del(request: web.Request):
    user = _admin(request, "delete")
    body = await request.json()
    mid = int(body.get("id", 0))
    mv = await db.get_movie(mid)
    if not mv:
        raise web.HTTPNotFound(reason="not found")
    _kind_ok(user, mv["kind"])
    await db.delete_movie(mid)
    return web.json_response({"ok": True})


def _clean_cover(value) -> str:
    v = str(value or "").strip()
    if v and not re.fullmatch(r"[A-Za-z0-9_-]{20,250}", v):
        raise ValueError("Muqova rasmi noto'g'ri. Qaytadan yuklang")
    return v


def _clean_meta(body: dict) -> dict:
    out = {}
    for key, limit in DRAFT_LIMITS.items():
        value = re.sub(r"[ \t]+", " ", str(body.get(key) or "")).strip()[:limit]
        if value:
            out[key] = value
    if "cover" in body:  # bor bo'lsa: file_id (almashtirish) yoki bo'sh (olib tashlash)
        out["cover"] = _clean_cover(body.get("cover"))
    if not out.get("title"):
        raise ValueError("Nomini kiriting")
    if out.get("year") and not re.fullmatch(r"(18|19|20)\d{2}", out["year"]):
        raise ValueError("Yil noto'g'ri (masalan: 2024)")
    if out.get("genre"):
        out["genre"] = ", ".join(g.strip() for g in out["genre"].split(",") if g.strip())
    return out


async def _clean_draft(body: dict, user: dict) -> dict:
    """Qo'lda qo'shish qoralamasi. Yakka kino/multfilm/anime yoki qismli kontentning bitta qismi."""
    kind = body.get("kind") or "kino"
    if kind not in db.KINDS:
        raise ValueError("Turini tanlang")
    _kind_ok(user, kind)
    parts = bool(body.get("parts")) or kind == "serial"
    if not parts:
        out = _clean_meta(body)
        out["kind"] = kind
        pi = body.get("premiere_idx")
        if pi not in (None, ""):
            try:
                pi = int(pi)
            except (TypeError, ValueError):
                raise ValueError("Premyera vaqti noto'g'ri")
            if not 0 <= pi < theatre.SLOTS:
                raise ValueError("Premyera vaqti noto'g'ri")
            if not theatre.available():
                raise ValueError("Premyera uchun teatr ulanmagan (API_ID / API_HASH)")
            if await db.q_one("SELECT 1 FROM movies WHERE status='premiere' AND premiere_at=?", (theatre.premiere_ts(pi),)):
                raise ValueError("Bu vaqtga boshqa premyera qo'yilgan")
            out["premiere_idx"] = pi
        return out
    try:
        ep = int(body.get("ep") or 0)
    except (TypeError, ValueError):
        ep = 0
    if not 1 <= ep <= 9999:
        raise ValueError("Qism raqamini kiriting (masalan: 1)")
    sid = int(body.get("series_id") or 0)
    if sid:
        ser = await db.get_series(sid)
        if not ser:
            raise ValueError("Serial topilmadi")
        _kind_ok(user, ser["kind"])
    else:  # yangi serial yaratiladi (qismi chiqmaguncha katalogda ko'rinmaydi)
        meta = _clean_meta(body)
        sid = await db.add_series({**meta, "kind": kind})
        ser = await db.get_series(sid)
    if await db.episode_by_no(sid, ep):
        raise ValueError(f"{ep}-qism allaqachon bor")
    return {"title": ser["title"], "kind": ser["kind"], "series_id": sid, "ep": ep}


async def api_admin_draft_get(request: web.Request):
    user = _admin(request, "add")
    return web.json_response({"draft": await db.get_draft(user["id"])})


async def api_admin_draft_save(request: web.Request):
    """Mini App'da kiritilgan ma'lumotlarni saqlaydi va botda adminga «endi videoni yuboring» deb yozadi."""
    user = _admin(request, "add")
    try:
        draft = await _clean_draft(await request.json(), user)
    except ValueError as e:
        return _bad(str(e))
    await db.save_draft(user["id"], draft)
    title = html.escape(draft["title"]) + (f" ({draft['year']})" if draft.get("year") else "")
    if draft.get("series_id"):
        title += f" — {draft['ep']}-qism"
    if draft.get("premiere_idx") is not None:
        title += f"\n⭐ Premyera: ertaga {theatre.premiere_slots()[draft['premiere_idx']]['time']} da teatrda"
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
    user = _admin(request, "add")
    await db.clear_draft(user["id"])
    return web.json_response({"ok": True})


# ───────────── Adminlarni boshqarish (Sozlamalar → Adminlar) ─────────────
async def _name_of(bot, uid: int) -> str:
    """Ismi: avval bazadan (botga kirgan bo'lsa), keyin Telegramdan, bo'lmasa ID."""
    name = await db.user_name(uid)
    if name:
        return name
    try:
        chat = await bot.get_chat(uid)
        name = (chat.full_name or "").strip() or (f"@{chat.username}" if chat.username else "")
        if name:
            return name
    except Exception:
        pass
    return f"ID {uid}"


async def _admins_payload(request: web.Request, viewer_id: int) -> dict:
    bot, viewer = request.app["bot"], perms.role(viewer_id)
    items = [{"id": oid, "name": await _name_of(bot, oid), "role": "owner", "label": perms.ROLE_LABEL["owner"],
              "info": perms.summary("owner", [], []), "perms": [], "sections": [], "editable": False}
             for oid in sorted(config.ADMIN_IDS)]
    for a in await db.list_admins():
        role = "senior" if a["role"] == "senior" else "admin"
        pl = [p for p in (a["perms"] or "").split(",") if p in perms.PERMS]
        sl = [x for x in (a["sections"] or "").split(",") if x in perms.SECTIONS]
        items.append({
            "id": a["id"], "name": a["name"] or f"ID {a['id']}", "role": role, "label": perms.ROLE_LABEL[role],
            "info": perms.summary(role, pl, sl), "perms": pl, "sections": sl,
            "editable": a["id"] != viewer_id and (viewer == "owner" or role == "admin"),
        })
    return {"admins": items, "perm_labels": perms.PERMS, "section_labels": perms.SECTIONS,
            "can_senior": viewer == "owner"}


async def api_admin_admins(request: web.Request):
    user = _manager(request)
    return web.json_response(await _admins_payload(request, user["id"]))


async def api_admin_admin_save(request: web.Request):
    user = _manager(request)
    body = await request.json()
    try:
        aid = int(str(body.get("id", "")).strip())
    except ValueError:
        return _bad("Telegram ID raqamini kiriting (masalan: 123456789)")
    if aid <= 0:
        return _bad("ID noto'g'ri")
    viewer = perms.role(user["id"])
    if aid in config.ADMIN_IDS:
        return _bad("Bu bosh admin — uni o'zgartirib bo'lmaydi")
    if aid == user["id"]:
        return _bad("O'zingizning huquqlaringizni o'zgartira olmaysiz")
    role = "senior" if body.get("role") == "senior" else "admin"
    old = await db.get_admin(aid)
    if (role == "senior" or (old and old["role"] == "senior")) and viewer != "owner":
        return _bad("Starshiy adminni faqat bosh admin tayinlaydi va o'zgartiradi")
    plist = [p for p in perms.PERMS if p in (body.get("perms") or [])]
    slist = [x for x in perms.SECTIONS if x in (body.get("sections") or [])]
    if role == "admin":
        if not plist:
            return _bad("Kamida bitta huquq belgilang")
        if set(plist) & {"add", "delete", "publish"} and not slist:
            return _bad("Kamida bitta bo'lim belgilang (kino, serial, anime, multfilm)")
    else:
        plist, slist = [], []
    bot = request.app["bot"]
    name = (old["name"] if old and old["name"] else "") or await _name_of(bot, aid)
    await db.save_admin(aid, name, role, ",".join(plist), ",".join(slist), user["id"])
    await perms.reload()
    notified = True
    if not old:
        text = (
            "🎉 <b>Siz admin etib tayinlandingiz!</b>\n\n"
            f"Turi: <b>{perms.ROLE_LABEL[role]}</b>\n"
            f"{html.escape(perms.summary(role, plist, slist))}\n\n"
            "Admin panelni ochish uchun /admin yozing yoki katalogdagi ⚙️ tugmasini bosing."
        )
        try:
            await bot.send_message(aid, text)
        except Exception:
            notified = False
    return web.json_response({"ok": True, "notified": notified, **await _admins_payload(request, user["id"])})


async def api_admin_admin_del(request: web.Request):
    user = _manager(request)
    body = await request.json()
    try:
        aid = int(body.get("id", 0))
    except (TypeError, ValueError):
        return _bad("ID noto'g'ri")
    if aid in config.ADMIN_IDS:
        return _bad("Bosh adminni o'chirib bo'lmaydi")
    if aid == user["id"]:
        return _bad("O'zingizni o'chira olmaysiz")
    old = await db.get_admin(aid)
    if not old:
        raise web.HTTPNotFound(reason="not found")
    if old["role"] == "senior" and perms.role(user["id"]) != "owner":
        return _bad("Starshiy adminni faqat bosh admin o'chiradi")
    await db.del_admin(aid)
    await perms.reload()
    return web.json_response({"ok": True, **await _admins_payload(request, user["id"])})


# ───────────── Muqova rasmi yuklash ─────────────
MAX_COVER = 10 * 1024 * 1024


async def api_admin_cover_upload(request: web.Request):
    """Rasmni Telegramga yuklab, file_id qaytaradi (poster kabi saqlanadi). Kontentga formani saqlaganda ulanadi."""
    user = _admin(request, "add")
    data = None
    reader = await request.multipart()
    async for part in reader:
        if part.name == "file" and part.filename:
            if not (part.headers.get("Content-Type") or "").lower().startswith("image/"):
                return _bad("Faqat rasm yuklash mumkin")
            buf = bytearray()
            while True:
                chunk = await part.read_chunk(256 * 1024)
                if not chunk:
                    break
                buf.extend(chunk)
                if len(buf) > MAX_COVER:
                    return _bad("Rasm 10 MB dan katta bo'lmasligi kerak")
            data = bytes(buf) or None
    if not data:
        return _bad("Rasm tanlang")
    try:
        sent = await request.app["bot"].send_photo(
            user["id"], BufferedInputFile(data, filename="cover.jpg"),
            caption="🖼 Muqova rasmi saqlandi", disable_notification=True)
    except TelegramAPIError as e:
        logging.warning("Muqova yuklanmadi: %s", e)
        return _bad("Rasm Telegramga yuklanmadi. Botga /start yozganingizni tekshiring.")
    return web.json_response({"ok": True, "cover": sent.photo[-1].file_id})


# ───────────── Hammaga xabar (matn / rasm / video) ─────────────
MAX_VIDEO = 49 * 1024 * 1024   # Telegram bot yuklash chegarasi ~50 MB
MAX_PHOTO = 10 * 1024 * 1024


async def api_admin_broadcast(request: web.Request):
    """Mini App'dan: matn + (ixtiyoriy) rasm yoki video. Avval adminning o'ziga yuboriladi, so'ng hammaga nusxalanadi."""
    user = _admin(request, "broadcast")
    text, media, fname, ctype = "", None, "media", ""
    reader = await request.multipart()
    async for part in reader:
        if part.name == "text":
            text = (await part.text()).strip()
        elif part.name == "file" and part.filename:
            fname, ctype = part.filename, (part.headers.get("Content-Type") or "").lower()
            buf = bytearray()
            while True:
                chunk = await part.read_chunk(256 * 1024)
                if not chunk:
                    break
                buf.extend(chunk)
                if len(buf) > MAX_VIDEO:
                    return _bad("Fayl 49 MB dan katta bo'lmasligi kerak")
            media = bytes(buf) or None
    if not text and not media:
        return _bad("Matn yozing yoki rasm/video tanlang")
    caption = html.escape(text)
    bot = request.app["bot"]
    try:
        if media:
            if len(caption) > 1024:
                return _bad("Rasm/video izohi 1024 belgidan oshmasligi kerak")
            file = BufferedInputFile(media, filename=fname)
            if ctype.startswith("image/"):
                if len(media) > MAX_PHOTO:
                    return _bad("Rasm 10 MB dan katta bo'lmasligi kerak")
                sent = await bot.send_photo(user["id"], file, caption=caption or None)
            elif ctype.startswith("video/"):
                sent = await bot.send_video(user["id"], file, caption=caption or None, supports_streaming=True)
            else:
                return _bad("Faqat rasm yoki video yuborish mumkin")
        else:
            if len(caption) > 4000:
                return _bad("Matn 4000 belgidan oshmasligi kerak")
            sent = await bot.send_message(user["id"], caption)
    except TelegramAPIError as e:
        logging.warning("Ommaviy xabar tayyorlanmadi: %s", e)
        return _bad("Telegram xabarni qabul qilmadi. Avval botga /start yozganingizni tekshiring.")
    total = len(await db.broadcast_ids())
    broadcast.spawn(broadcast.copy_all(bot, user["id"], user["id"], sent.message_id))
    return web.json_response({"ok": True, "total": total})


# ───────────── Teatr ─────────────
TH_CHAT_MAX = 300


def _th_on() -> None:
    if not theatre.available():
        raise web.HTTPServiceUnavailable(reason="Teatr hali ulanmagan")
    if not theatre.enabled():
        raise web.HTTPServiceUnavailable(reason="Teatr hozircha yopiq")


def _auth_q(request: web.Request) -> dict:
    """<video src> sarlavha yubora olmaydi, shuning uchun initData ?a= orqali keladi."""
    user = verify_init_data(request.query.get("a", ""), config.BOT_TOKEN)
    if not user:
        raise web.HTTPUnauthorized(reason="auth")
    return user


def _uname(user: dict) -> str:
    name = " ".join(x for x in (user.get("first_name"), user.get("last_name")) if x).strip()
    return (name or (f"@{user['username']}" if user.get("username") else "") or "Foydalanuvchi")[:40]


async def api_th_state(request: web.Request):
    _th_on()
    user = _auth(request)
    st = await theatre.state()
    if request.query.get("w"):
        theatre.seen("pub", user["id"])
    st["viewers"] = theatre.viewers("pub")
    return web.json_response(st)


async def api_th_plan(request: web.Request):
    _th_on()
    _auth(request)
    return web.json_response(await theatre.plan_view())


async def api_th_video(request: web.Request):
    _th_on()
    user = _auth_q(request)
    if await db.is_banned(user["id"]) and not perms.is_staff(user["id"]):
        raise web.HTTPForbidden(reason="ban")
    mid = int(request.match_info["id"])
    row = await db.get_movie(mid)
    if not row:
        raise web.HTTPNotFound(reason="not found")
    rid = request.query.get("r", "")
    if rid:
        room = await db.th_room_get(rid)
        if not room or room["movie_id"] != mid:
            raise web.HTTPNotFound(reason="not found")
        if not await db.th_member_has(rid, user["id"]):
            raise web.HTTPForbidden(reason="locked")
    elif not (await theatre.in_today_plan(mid) or perms.is_staff(user["id"])):
        raise web.HTTPForbidden(reason="forbidden")
    return await theatre.serve(request, row)


async def _th_chat_room(user: dict, room: str, for_post: bool) -> str | None:
    if room == "pub":
        key = await theatre.current_key()
        if key == "pub:closed":
            if for_post:
                raise web.HTTPBadRequest(reason="Teatr hozir yopiq")
            return None
        return key
    r = await db.th_room_get(room)
    if not r:
        raise web.HTTPNotFound(reason="Xona topilmadi")
    if not (await db.th_member_has(r["id"], user["id"]) or perms.is_staff(user["id"])):
        raise web.HTTPForbidden(reason="locked")
    return r["id"]


def _th_chat_item(r, viewer_id: int) -> dict:
    return {"id": r["id"], "uid": r["user_id"], "name": r["name"] or "Foydalanuvchi", "text": r["text"],
            "at": r["at"], "mine": r["user_id"] == viewer_id, "staff": perms.is_staff(r["user_id"])}


async def api_th_chat_list(request: web.Request):
    _th_on()
    user = _auth(request)
    key = await _th_chat_room(user, request.query.get("room", "pub"), False)
    if key is None:
        return web.json_response({"key": "pub:closed", "items": []})
    after = max(int(request.query.get("after", "0") or 0), 0)
    rows = await db.th_chat_list(key, after, 50)
    return web.json_response({"key": key, "items": [_th_chat_item(r, user["id"]) for r in rows]})


async def api_th_chat_send(request: web.Request):
    _th_on()
    user = _auth(request)
    if await db.is_banned(user["id"]):
        raise web.HTTPForbidden(reason="Siz chatga yoza olmaysiz")
    body = await request.json()
    key = await _th_chat_room(user, str(body.get("room") or "pub"), True)
    text = re.sub(r"[ \t]+", " ", str(body.get("text") or "")).strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    if not text:
        return _bad("Xabar yozing")
    if len(text) > TH_CHAT_MAX:
        return _bad(f"Xabar {TH_CHAT_MAX} belgidan oshmasligi kerak")
    if time.time() - await db.th_chat_last_at(user["id"]) < CHAT_COOLDOWN:
        return web.json_response({"error": "Biroz sekinroq yozing 🙂"}, status=429)
    msg = await db.th_chat_add(key, user["id"], _uname(user), text)
    return web.json_response({"ok": True, "key": key, "item": {**msg, "mine": True, "staff": perms.is_staff(user["id"])}})


async def api_th_chat_delete(request: web.Request):
    _admin(request)
    body = await request.json()
    await db.th_chat_delete(int(body.get("id", 0)))
    return web.json_response({"ok": True})


# — Shaxsiy xonalar —
async def api_th_rooms(request: web.Request):
    _th_on()
    user = _auth(request)
    rows = await db.th_rooms_list(int(time.time()) - 3 * 3600)
    return web.json_response({"items": [
        {"id": r["id"], "title": r["title"], "locked": bool(r["pw"]), "owner": r["owner_name"],
         "movie": r["mtitle"], "viewers": theatre.viewers(r["id"]), "mine": r["owner"] == user["id"]}
        for r in rows]})


async def api_th_room_create(request: web.Request):
    _th_on()
    user = _auth(request)
    body = await request.json()
    title = re.sub(r"\s+", " ", str(body.get("title") or "")).strip()[:40]
    if len(title) < 2:
        return _bad("Xonaga nom bering (kamida 2 belgi)")
    mv = await db.get_movie(int(body.get("movie_id") or 0))
    if not mv or (mv["status"] != "live" and not perms.is_staff(user["id"])):
        return _bad("Kinoni tanlang")
    if await db.th_rooms_of(user["id"], int(time.time()) - 6 * 3600) >= 2:
        return _bad("Bir vaqtda ko'pi bilan 2 ta xona ochish mumkin")
    if not await theatre.ensure_meta(mv):
        return _bad("Bu kino teatr uchun mos emas (format yoki hajmi). Boshqasini tanlang")
    pw = str(body.get("password") or "").strip()[:20]
    salt = theatre.secrets.token_hex(8) if pw else ""
    rid = theatre.new_room_id()
    while await db.th_room_get(rid):
        rid = theatre.new_room_id()
    await db.th_room_add({"id": rid, "owner": user["id"], "owner_name": _uname(user), "title": title,
                          "movie_id": mv["id"], "salt": salt, "pw": theatre.hash_pw(salt, pw)})
    return web.json_response({"ok": True, "id": rid})


async def api_th_room_join(request: web.Request):
    _th_on()
    user = _auth(request)
    body = await request.json()
    rid = str(body.get("id") or "").strip().upper()
    room = await db.th_room_get(rid)
    if not room:
        return _bad("Bunday xona topilmadi")
    if not await db.th_member_has(rid, user["id"]) and room["pw"]:
        if not theatre.pw_allowed(user["id"], rid):
            return web.json_response({"error": "Juda ko'p urinish. Birozdan keyin qayta urinib ko'ring"}, status=429)
        if not theatre.check_pw(room, str(body.get("password") or "")):
            theatre.pw_failed(user["id"], rid)
            return _bad("Parol noto'g'ri")
    await db.th_member_add(rid, user["id"])
    return web.json_response({"ok": True, "id": rid})


async def api_th_room_get(request: web.Request):
    _th_on()
    user = _auth(request)
    rid = request.match_info["id"].upper()
    room = await db.th_room_get(rid)
    if not room:
        raise web.HTTPNotFound(reason="Xona topilmadi yoki yopilgan")
    if not await db.th_member_has(rid, user["id"]):
        if room["pw"]:
            raise web.HTTPForbidden(reason="locked")
        await db.th_member_add(rid, user["id"])
    mv = await db.get_movie(room["movie_id"])
    now = time.time()
    theatre.seen(rid, user["id"])
    if now - room["last_active"] > 60:
        await db.th_room_touch(rid)
    return web.json_response({
        "room": {"id": rid, "title": room["title"], "owner": room["owner_name"], "host": room["owner"] == user["id"],
                 "locked": bool(room["pw"]), "playing": bool(room["playing"]), "pos": theatre.room_pos(room, now),
                 "now": now,
                 "movie": {"id": mv["id"], "title": mv["title"], "duration": mv["duration"],
                           "ep": mv["ep"] if mv["series_id"] else None}},
        "viewers": theatre.viewers(rid),
    })


async def api_th_room_state(request: web.Request):
    user = _auth(request)
    rid = request.match_info["id"].upper()
    room = await db.th_room_get(rid)
    if not room:
        raise web.HTTPNotFound(reason="Xona topilmadi")
    if room["owner"] != user["id"]:
        raise web.HTTPForbidden(reason="Faqat xona egasi boshqaradi")
    body = await request.json()
    pos = max(float(body.get("pos") or 0), 0.0)
    await db.th_room_state(rid, bool(body.get("playing")), pos, time.time())
    return web.json_response({"ok": True})


async def api_th_room_delete(request: web.Request):
    user = _auth(request)
    rid = request.match_info["id"].upper()
    room = await db.th_room_get(rid)
    if not room:
        raise web.HTTPNotFound(reason="Xona topilmadi")
    if room["owner"] != user["id"] and not perms.is_staff(user["id"]):
        raise web.HTTPForbidden(reason="Faqat xona egasi yopa oladi")
    await db.th_room_del(rid)
    return web.json_response({"ok": True})


# — Premyera (admin) —
async def api_admin_th(request: web.Request):
    user = _admin(request, "publish")
    pend = [r for r in await db.q_all(
        "SELECT id, title, year, kind FROM movies WHERE status='pending' AND series_id IS NULL ORDER BY id DESC LIMIT 100")
        if perms.can_kind(user["id"], r["kind"])]
    sched = await db.q_all(
        "SELECT id, title, premiere_at FROM movies WHERE status='premiere' ORDER BY premiere_at")
    return web.json_response({
        "ready": theatre.available(), "day": theatre.tomorrow(), "slots": theatre.premiere_slots(),
        "pending": [{"id": r["id"], "title": r["title"], "year": r["year"]} for r in pend],
        "scheduled": [{"id": r["id"], "title": r["title"], "time": theatre.hhmm(r["premiere_at"]),
                       "day": theatre.day_of(r["premiere_at"]), "started": r["premiere_at"] <= time.time()}
                      for r in sched],
    })


async def api_admin_th_premiere(request: web.Request):
    user = _admin(request, "publish")
    if not theatre.available():
        return _bad("Teatr ulanmagan (API_ID / API_HASH ni tekshiring)")
    body = await request.json()
    mv = await db.get_movie(int(body.get("movie_id") or 0))
    idx = int(body.get("idx", -1))
    if not mv or mv["status"] != "pending" or mv["series_id"]:
        return _bad("Premyera uchun «kutilayotgan» yakka kino tanlang")
    _kind_ok(user, mv["kind"])
    if not 0 <= idx < theatre.SLOTS:
        return _bad("Vaqtni tanlang")
    if not await theatre.ensure_meta(mv):
        return _bad("Bu video teatrga mos emas (mp4 emas, juda uzun yoki ochilmadi)")
    ts = theatre.premiere_ts(idx)
    if await db.q_one("SELECT 1 FROM movies WHERE status='premiere' AND premiere_at=?", (ts,)):
        return _bad("Bu vaqtga boshqa premyera qo'yilgan")
    await db.q_run("UPDATE movies SET status='premiere', premiere_at=? WHERE id=? AND status='pending'", (ts, mv["id"]))
    return web.json_response({"ok": True})


async def api_admin_th_rebuild(request: web.Request):
    _admin(request, "publish")
    if not theatre.available():
        return _bad("Teatr ulanmagan")
    n = await theatre.rebuild_today()
    return web.json_response({"ok": True, "slots": n})


async def api_admin_th_cancel(request: web.Request):
    user = _admin(request, "publish")
    body = await request.json()
    mv = await db.get_movie(int(body.get("movie_id") or 0))
    if not mv or mv["status"] != "premiere":
        raise web.HTTPNotFound(reason="not found")
    _kind_ok(user, mv["kind"])
    if (mv["premiere_at"] or 0) <= time.time():
        return _bad("Premyera boshlangan — bekor qilib bo'lmaydi")
    await db.q_run("UPDATE movies SET status='pending', premiere_at=NULL WHERE id=?", (mv["id"],))
    return web.json_response({"ok": True})


def make_app(bot) -> web.Application:
    app = web.Application(middlewares=[errors], client_max_size=60 * 1024 * 1024)
    app["bot"] = bot
    r = app.router
    r.add_get("/", index)
    r.add_get("/health", health)
    r.add_get("/logo.png", logo)
    r.add_get("/api/me", api_me)
    r.add_get("/api/movies", api_movies)
    r.add_get(r"/api/movie/{id:\d+}", api_movie)
    r.add_get(r"/api/series/{id:\d+}", api_series)
    r.add_get(r"/api/sposter/{id:\d+}", api_sposter)
    r.add_get(r"/api/cover/{id:\d+}", api_cover)
    r.add_get(r"/api/scover/{id:\d+}", api_scover)
    r.add_get("/api/hero", api_hero)
    r.add_get(r"/api/similar/{t:[ms]}/{id:\d+}", api_similar)
    r.add_get("/api/chat", api_chat_list)
    r.add_post("/api/chat", api_chat_send)
    r.add_post("/api/chat/delete", api_chat_delete)
    r.add_get("/api/theatre/state", api_th_state)
    r.add_get("/api/theatre/plan", api_th_plan)
    r.add_get(r"/api/theatre/v/{id:\d+}", api_th_video)
    r.add_get("/api/theatre/chat", api_th_chat_list)
    r.add_post("/api/theatre/chat", api_th_chat_send)
    r.add_post("/api/theatre/chat/delete", api_th_chat_delete)
    r.add_get("/api/theatre/rooms", api_th_rooms)
    r.add_post("/api/theatre/room", api_th_room_create)
    r.add_post("/api/theatre/room/join", api_th_room_join)
    r.add_get("/api/theatre/room/{id}", api_th_room_get)
    r.add_post("/api/theatre/room/{id}/state", api_th_room_state)
    r.add_post("/api/theatre/room/{id}/delete", api_th_room_delete)
    r.add_get("/api/admin/theatre", api_admin_th)
    r.add_post("/api/admin/theatre/premiere", api_admin_th_premiere)
    r.add_post("/api/admin/theatre/cancel", api_admin_th_cancel)
    r.add_post("/api/admin/theatre/rebuild", api_admin_th_rebuild)
    r.add_post("/api/send", api_send)
    r.add_post("/api/fav", api_fav)
    r.add_get(r"/api/poster/{id:\d+}", api_poster)
    # admin
    r.add_get("/api/admin/stats", api_admin_stats)
    r.add_get("/api/admin/settings", api_admin_settings)
    r.add_post("/api/admin/setting", api_admin_setting)
    r.add_post("/api/admin/theme", api_admin_theme)
    r.add_post("/api/admin/channel", api_admin_channel_add)
    r.add_post("/api/admin/source", api_admin_source_add)
    r.add_get("/api/admin/series", api_admin_series_list)
    r.add_get(r"/api/admin/series/{id:\d+}", api_admin_series_get)
    r.add_post("/api/admin/series/update", api_admin_series_update)
    r.add_post("/api/admin/series/delete", api_admin_series_del)
    r.add_post("/api/admin/channel/delete", api_admin_channel_del)
    r.add_get("/api/admin/pending", api_admin_pending)
    r.add_post("/api/admin/publish", api_admin_publish)
    r.add_get(r"/api/admin/movie/{id:\d+}", api_admin_movie_get)
    r.add_post("/api/admin/movie/update", api_admin_movie_update)
    r.add_post("/api/admin/movie/delete", api_admin_movie_del)
    r.add_get("/api/admin/draft", api_admin_draft_get)
    r.add_post("/api/admin/draft", api_admin_draft_save)
    r.add_post("/api/admin/draft/clear", api_admin_draft_clear)
    r.add_get("/api/admin/admins", api_admin_admins)
    r.add_post("/api/admin/admin", api_admin_admin_save)
    r.add_post("/api/admin/admin/delete", api_admin_admin_del)
    r.add_post("/api/admin/broadcast", api_admin_broadcast)
    r.add_post("/api/admin/cover", api_admin_cover_upload)
    return app


async def start(bot, port: int) -> web.AppRunner:
    runner = web.AppRunner(make_app(bot))
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    return runner
