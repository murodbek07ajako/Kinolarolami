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

SETTING_DEFAULTS = {"protect_content": "0", "sub_required": "1"}
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
        "kind": r["kind"], "code": db.series_code(r["kind"], r["id"]) if t == "s" else str(r["id"]),
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


# ───────────── Foydalanuvchi API ─────────────
async def api_me(request: web.Request):
    user = _auth(request)
    name = (user.get("first_name") or "").strip() or "do'st"
    return web.json_response({
        "name": name,
        "favs": await db.fav_ids(user["id"]),
        "admin": perms.is_staff(user["id"]),
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
    return web.json_response({
        "t": "s", "id": ser["id"], "fk": -ser["id"], "kind": ser["kind"],
        "code": db.series_code(ser["kind"], ser["id"]),
        "title": ser["title"], "year": ser["year"], "genre": ser["genre"], "country": ser["country"],
        "language": ser["language"], "description": ser["description"], "quality": "",
        "views": sum(e["views"] for e in eps), "poster": bool(ser["poster_id"]),
        "cv": _cv(ser["cover_id"]),
        "eps": [{"id": e["id"], "ep": e["ep"], "views": e["views"], "live": e["status"] == "live"} for e in eps],
    })


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
    s["top"] = [{"id": t["id"], "title": t["title"], "views": t["views"]} for t in s["top"]]
    return web.json_response(s)


async def _settings_payload() -> dict:
    out = {k: (await db.get_setting(k, default)) == "1" for k, default in SETTING_DEFAULTS.items()}
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
    return web.json_response({"ok": True})


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
            "items": [{"id": r["id"], "title": r["title"], "year": r["year"], "genre": r["genre"], "ep": r["ep"]}
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
    return web.json_response({"movie": {"id": m["id"], **{k: m[k] or "" for k in db.META_FIELDS}, "cv": _cv(m["cover_id"])}})


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
    r.add_post("/api/send", api_send)
    r.add_post("/api/fav", api_fav)
    r.add_get(r"/api/poster/{id:\d+}", api_poster)
    # admin
    r.add_get("/api/admin/stats", api_admin_stats)
    r.add_get("/api/admin/settings", api_admin_settings)
    r.add_post("/api/admin/setting", api_admin_setting)
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
