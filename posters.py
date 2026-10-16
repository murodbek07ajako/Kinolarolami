"""Avtomatik muqova (poster) qidirish.

Kinoga muqova qo'yilmagan bo'lsa (Mini App'da logo chiqib turgan bo'lsa), bot internetdan
«<nom> <multfilm | anime | kino>» deb TIK (9:16 ga yaqin) rasm qidiradi, birinchi yaroqli natijani
Telegramga yuklaydi va muqova qilib saqlaydi.

Manbalar (ketma-ket):
  1. anime  → AniList (kalitsiz, rasmiy)
  2. hammasi → TMDB (rasmiy poster bazasi; .env da TMDB_API_KEY yoki TMDB_TOKEN kerak)
  3. zaxira → DuckDuckGo / Bing rasmlari (kalitsiz, lekin beqarorroq)
Hech narsa topilmasa kino o'zgarmaydi (logo qoladi) va keyingi safar yana urinib ko'riladi.
"""
import asyncio
import html
import json
import logging
import os
import re

import aiohttp
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BufferedInputFile

import broadcast
import db
import perms

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Safari/537.36")
SUFFIX = {"multfilm": "multfilm", "anime": "anime"}  # boshqa hammasi (kino, serial) — «kino»
MAX_IMG = 8 * 1024 * 1024
MIN_IMG = 8 * 1024
TRIES = 15  # nechta nomzod rasm sinab ko'riladi
_sem = asyncio.Semaphore(2)


TMDB_KEY = os.getenv("TMDB_API_KEY", "").strip()
TMDB_TOKEN = os.getenv("TMDB_TOKEN", "").strip()


def clean_title(title: str) -> str:
    t = re.sub(r"\s*[-—–]\s*\d+\s*-?\s*qism.*$", "", title or "", flags=re.I)
    t = re.sub(r"[\[\(].*?[\]\)]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def build_query(title: str, kind: str) -> str:
    t = clean_title(title)
    return f"{t} {SUFFIX.get(kind, 'kino')}"


def _image_type(data: bytes) -> bool:
    return data[:3] == b"\xff\xd8\xff" or data[:8] == b"\x89PNG\r\n\x1a\n" or (
        data[:4] == b"RIFF" and data[8:12] == b"WEBP")


# ───────────── Qidiruv manbalari: [(url, width, height), ...] ─────────────
async def _ddg(session: aiohttp.ClientSession, q: str) -> list[tuple]:
    async with session.get("https://duckduckgo.com/", params={"q": q, "iax": "images", "ia": "images"}) as r:
        page = await r.text()
    m = re.search(r"vqd=([\d-]+)", page) or re.search(r"vqd=[\"']([\d-]+)", page)
    if not m:
        return []
    params = {"l": "us-en", "o": "json", "q": q, "vqd": m.group(1), "f": ",,,,layout:Tall,", "p": "1"}
    async with session.get("https://duckduckgo.com/i.js", params=params,
                           headers={"Referer": "https://duckduckgo.com/"}) as r:
        data = await r.json(content_type=None)
    return [(x.get("image"), int(x.get("width") or 0), int(x.get("height") or 0), x.get("title") or "")
            for x in data.get("results", []) if x.get("image")]


async def _bing(session: aiohttp.ClientSession, q: str) -> list[tuple]:
    params = {"q": q, "qft": "+filterui:aspect-tall", "form": "IRFLTR", "first": "1"}
    async with session.get("https://www.bing.com/images/search", params=params) as r:
        page = await r.text()
    out = []
    for raw in re.findall(r'\bm="(\{.*?\})"', page):
        try:
            d = json.loads(html.unescape(raw))
        except ValueError:
            continue
        if d.get("murl"):
            out.append((d["murl"], 0, 0, d.get("t") or ""))  # Bing o'lchamni bermaydi — «tall» filtri o'zi saralaydi
    return out


def _words(t: str) -> set:
    return {w for w in re.findall(r"\w+", (t or "").lower()) if len(w) > 2}


def _order(cands: list[tuple], title: str = "") -> list[tuple]:
    """Nom so'zlari rasm sarlavhasida bor bo'lganlar, tik (balandligi ≥ 1.3× eni) rasmlar oldinga."""
    seen, out = set(), []
    want = _words(title)
    for url, w, h, t in cands:
        if url in seen or not url.startswith("http"):
            continue
        seen.add(url)
        tall = 0 if (w and h and h >= w * 1.3 and w >= 250) else (1 if not (w and h) else 2)
        match = 0 if (want and want & _words(t)) else 1
        out.append((match, tall, len(out), (url, w, h, t)))
    out.sort(key=lambda x: x[:3])
    return [x[3] for x in out]


def _dims(data: bytes):
    """(eni, bo'yi) — JPEG/PNG/WEBP sarlavhasidan; aniqlab bo'lmasa None."""
    try:
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
        if data[:3] == b"\xff\xd8\xff":
            i = 2
            while i + 9 < len(data):
                if data[i] != 0xFF:
                    i += 1
                    continue
                mk = data[i + 1]
                if mk in (0xD8, 0x01) or 0xD0 <= mk <= 0xD7:
                    i += 2
                    continue
                ln = int.from_bytes(data[i + 2:i + 4], "big")
                if mk in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    return int.from_bytes(data[i + 7:i + 9], "big"), int.from_bytes(data[i + 5:i + 7], "big")
                i += 2 + ln
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            k = data[12:16]
            if k == b"VP8X":
                return 1 + int.from_bytes(data[24:27], "little"), 1 + int.from_bytes(data[27:30], "little")
            if k == b"VP8 ":
                return int.from_bytes(data[26:28], "little") & 0x3FFF, int.from_bytes(data[28:30], "little") & 0x3FFF
            if k == b"VP8L":
                b = int.from_bytes(data[21:25], "little")
                return (b & 0x3FFF) + 1, ((b >> 14) & 0x3FFF) + 1
    except Exception:
        pass
    return None


def is_portrait(data: bytes) -> bool:
    """Faqat tik (9:16 / 2:3 ga yaqin) rasm yaroqli: bo'yi eniga nisbatan 1.3–2.2 marta."""
    d = _dims(data)
    return bool(d and d[0] >= 200 and 1.3 <= d[1] / d[0] <= 2.2)


async def _anilist(session: aiohttp.ClientSession, title: str, year: str) -> list[str]:
    q = ("query($s:String){Page(perPage:6){media(search:$s,type:ANIME,sort:SEARCH_MATCH)"
         "{coverImage{extraLarge large} startDate{year}}}}")
    async with session.post("https://graphql.anilist.co", json={"query": q, "variables": {"s": title}}) as r:
        data = await r.json(content_type=None)
    media = (((data or {}).get("data") or {}).get("Page") or {}).get("media") or []
    if year.isdigit():  # yili mos kelgani oldinga
        media.sort(key=lambda m: (m.get("startDate") or {}).get("year") != int(year))
    return [c for m in media for c in [(m.get("coverImage") or {}).get("extraLarge")
                                      or (m.get("coverImage") or {}).get("large")] if c]


async def _tmdb(session: aiohttp.ClientSession, title: str, year: str, kind: str) -> list[str]:
    if not (TMDB_KEY or TMDB_TOKEN):
        return []
    params = {"query": title, "include_adult": "false", "language": "en-US"}
    headers = {}
    if TMDB_TOKEN:
        headers["Authorization"] = f"Bearer {TMDB_TOKEN}"
    else:
        params["api_key"] = TMDB_KEY
    async with session.get("https://api.themoviedb.org/3/search/multi", params=params, headers=headers) as r:
        data = await r.json(content_type=None)
    res = [x for x in (data or {}).get("results", [])
           if x.get("poster_path") and x.get("media_type") in ("movie", "tv")]

    def rank(x):
        y = (x.get("release_date") or x.get("first_air_date") or "")[:4]
        anim = 16 in (x.get("genre_ids") or [])
        want_tv = kind == "serial"
        return (
            0 if (year.isdigit() and y == year) else 1,
            0 if (anim == (kind in ("multfilm", "anime"))) else 1,
            0 if ((x["media_type"] == "tv") == want_tv) else 1,
        )
    res.sort(key=rank)  # sort barqaror: qidiruv tartibi (mashhurlik) saqlanadi
    return ["https://image.tmdb.org/t/p/w780" + x["poster_path"] for x in res]


async def search_images(query: str, title: str = "", kind: str = "kino", year: str = "") -> list[tuple]:
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": UA}) as s:
        api = []
        if title:
            steps = [(_anilist, (title, year))] if kind == "anime" else []
            steps.append((_tmdb, (title, year, kind)))
            for fn, args in steps:
                try:
                    api += await fn(s, *args)
                except Exception as e:
                    logging.warning("Rasm qidirish (%s) xato: %s", fn.__name__, e)
        if api:
            return [(u, 0, 0, "") for u in api]  # rasmiy bazalar tartibi allaqachon saralangan
        for src in (_ddg, _bing):
            try:
                found = _order(await src(s, query), title)
            except Exception as e:
                logging.warning("Rasm qidirish (%s) xato: %s", src.__name__, e)
                continue
            if found:
                return found
    return []


def _complete(data: bytes) -> bool:
    """Rasm oxirigacha yuklanganini tekshiradi (yarim yuklangan rasm — «yarmi ochilmagan» bo'lib qoladi)."""
    if data[:3] == b"\xff\xd8\xff":
        return data.rstrip(b"\x00")[-2:] == b"\xff\xd9"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return data[-12:-8] == b"\x00\x00\x00\x00" and data[-8:-4] == b"IEND"
    if data[:4] == b"RIFF":
        return int.from_bytes(data[4:8], "little") + 8 <= len(data)
    return False


async def _download(session: aiohttp.ClientSession, url: str) -> bytes | None:
    try:
        async with session.get(url) as r:
            if r.status != 200 or not (r.headers.get("Content-Type", "")).lower().startswith("image/"):
                return None
            buf = bytearray()
            async for chunk in r.content.iter_chunked(64 * 1024):  # hammasi oxirigacha o'qiladi
                buf.extend(chunk)
                if len(buf) > MAX_IMG:
                    return None
            want = int(r.headers.get("Content-Length") or 0)
    except Exception:
        return None
    data = bytes(buf)
    if len(data) < MIN_IMG or not _image_type(data) or (want and len(data) != want) or not _complete(data) or not is_portrait(data):
        return None
    return data


async def find_cover(bot: Bot, title: str, kind: str, year: str = "") -> str | None:
    """Rasmni topib Telegramga yuklaydi va file_id qaytaradi (topilmasa None)."""
    admins = sorted(perms.notify_ids())
    if not admins:
        return None
    cands = await search_images(build_query(title, kind), clean_title(title), kind, year or "")
    if not cands:
        return None
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": UA}) as s:
        for url, *_ in cands[:TRIES]:
            data = await _download(s, url)
            if not data:
                continue
            for chat_id in admins:  # birinchi yetib boradigan adminga yuklaymiz
                try:
                    sent = await bot.send_photo(chat_id, BufferedInputFile(data, filename="cover.jpg"),
                                                disable_notification=True)
                except TelegramAPIError:
                    continue
                try:
                    await bot.delete_message(chat_id, sent.message_id)  # chatni to'ldirmaslik uchun
                except TelegramAPIError:
                    pass
                return sent.photo[-1].file_id
    return None


# ───────────── Bazaga ulash ─────────────
async def ensure_movie(bot: Bot, movie_id: int) -> bool:
    mv = await db.get_movie(movie_id)
    if not mv or mv["cover_id"] or mv["series_id"]:  # qismlarning muqovasi serialda turadi
        return False
    async with _sem:
        fid = await find_cover(bot, mv["title"], mv["kind"], mv["year"])
    if not fid:
        return False
    return await db.set_cover_if_empty(movie_id, fid)


async def ensure_series(bot: Bot, series_id: int) -> bool:
    ser = await db.get_series(series_id)
    if not ser or ser["cover_id"]:
        return False
    async with _sem:
        fid = await find_cover(bot, ser["title"], ser["kind"], ser["year"])
    if not fid:
        return False
    return await db.set_series_cover_if_empty(series_id, fid)


def spawn_movie(bot: Bot, movie_id: int) -> None:
    broadcast.spawn(_safe(ensure_movie(bot, movie_id)))


def spawn_series(bot: Bot, series_id: int) -> None:
    broadcast.spawn(_safe(ensure_series(bot, series_id)))


async def _safe(coro):
    try:
        return await coro
    except Exception:
        logging.exception("Avto-muqova xatosi")
        return False


_backfill_lock = asyncio.Lock()


async def backfill(bot: Bot, notify: bool = True) -> tuple[int, int]:
    """Muqovasiz (logo chiqib turgan) hamma kino/serialni to'ldiradi. (topildi, topilmadi) qaytaradi."""
    if _backfill_lock.locked():
        return 0, 0
    async with _backfill_lock:
        ok = bad = 0
        for r in await db.movies_without_cover():
            if await _safe(ensure_movie(bot, r["id"])):
                ok += 1
            else:
                bad += 1
            await asyncio.sleep(1.5)  # qidiruv saytlarini bloklab qo'ymaslik uchun
        for r in await db.series_without_cover():
            if await _safe(ensure_series(bot, r["id"])):
                ok += 1
            else:
                bad += 1
            await asyncio.sleep(1.5)
    logging.info("Avto-muqova: %s ta topildi, %s ta topilmadi", ok, bad)
    if notify and (ok or bad):
        from services import notify_admins
        await notify_admins(bot, f"🖼 <b>Avto-muqova tugadi</b>\n\n✅ Topilib qo'yildi: {ok}\n"
                                 f"❌ Topilmadi: {bad}" + ("\n\nTopilmaganlarga Mini App'da o'zingiz rasm qo'ying "
                                                         "yoki keyinroq /rasmlar ni qayta bosing." if bad else ""))
    return ok, bad
