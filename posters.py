"""Avtomatik muqova (poster) qidirish.

Kinoga muqova qo'yilmagan bo'lsa (Mini App'da logo chiqib turgan bo'lsa), bot internetdan
«<nom> <multfilm | anime | kino>» deb TIK (9:16 ga yaqin) rasm qidiradi, birinchi yaroqli natijani
Telegramga yuklaydi va muqova qilib saqlaydi.

Manbalar (ketma-ket): DuckDuckGo rasmlari → Bing rasmlari. Kalit (API key) kerak emas.
Hech narsa topilmasa kino o'zgarmaydi (logo qoladi) va keyingi safar yana urinib ko'riladi.
"""
import asyncio
import html
import json
import logging
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
TRIES = 6  # nechta nomzod rasm sinab ko'riladi
_sem = asyncio.Semaphore(2)


def build_query(title: str, kind: str) -> str:
    t = re.sub(r"\s*[-—–]\s*\d+\s*-?\s*qism.*$", "", title or "", flags=re.I)
    t = re.sub(r"[\[\(].*?[\]\)]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
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
    return [(x.get("image"), int(x.get("width") or 0), int(x.get("height") or 0))
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
            out.append((d["murl"], 0, 0))  # Bing o'lchamni bermaydi — «tall» filtri o'zi saralaydi
    return out


def _order(cands: list[tuple]) -> list[tuple]:
    """Tik rasmlar oldinga (balandligi eniga ≥ 1.2×), so'ng qolganlari; tartib saqlanadi."""
    seen, tall, rest = set(), [], []
    for url, w, h in cands:
        if url in seen or not url.startswith("http"):
            continue
        seen.add(url)
        (tall if (w and h and h >= w * 1.2 and w >= 250) else rest).append((url, w, h))
    return tall + rest


async def search_images(query: str) -> list[tuple]:
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": UA}) as s:
        for src in (_ddg, _bing):
            try:
                found = _order(await src(s, query))
            except Exception as e:
                logging.warning("Rasm qidirish (%s) xato: %s", src.__name__, e)
                continue
            if found:
                return found
    return []


async def _download(session: aiohttp.ClientSession, url: str) -> bytes | None:
    try:
        async with session.get(url) as r:
            if r.status != 200 or not (r.headers.get("Content-Type", "")).lower().startswith("image/"):
                return None
            data = await r.content.read(MAX_IMG + 1)
    except Exception:
        return None
    if len(data) > MAX_IMG or len(data) < MIN_IMG or not _image_type(data):
        return None
    return data


async def find_cover(bot: Bot, title: str, kind: str) -> str | None:
    """Rasmni topib Telegramga yuklaydi va file_id qaytaradi (topilmasa None)."""
    admins = sorted(perms.notify_ids())
    if not admins:
        return None
    cands = await search_images(build_query(title, kind))
    if not cands:
        return None
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": UA}) as s:
        for url, _, _ in cands[:TRIES]:
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
        fid = await find_cover(bot, mv["title"], mv["kind"])
    if not fid:
        return False
    return await db.set_cover_if_empty(movie_id, fid)


async def ensure_series(bot: Bot, series_id: int) -> bool:
    ser = await db.get_series(series_id)
    if not ser or ser["cover_id"]:
        return False
    async with _sem:
        fid = await find_cover(bot, ser["title"], ser["kind"])
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
