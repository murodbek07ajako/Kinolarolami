"""Ommaviy yuborish: admin xabari va yangi kino e'loni uchun umumiy mexanizm."""
import asyncio
import html
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter

import db
import keyboards as kb
import services as svc

# Bir vaqtning o'zida faqat bitta ommaviy yuborish ishlaydi (Telegram limiti: ~30 xabar/soniya)
lock = asyncio.Lock()
_tasks: set[asyncio.Task] = set()
_announced: set[int] = set()


def spawn(coro) -> asyncio.Task:
    """Fonda vazifa ishga tushiradi (havolani saqlab turadi, shunda u o'chib ketmaydi)."""
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task


async def run(bot: Bot, send) -> tuple[int, int, int, int]:
    """Barcha faol foydalanuvchilarga `await send(user_id)` ni bajaradi.
    (yetkazildi, bloklagan, xatolik, jami) qaytaradi."""
    ids = await db.broadcast_ids()
    ok = blocked = failed = 0
    for uid in ids:
        done = False
        for _ in range(3):
            try:
                await send(uid)
                ok += 1
                done = True
                break
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
            except TelegramForbiddenError:
                blocked += 1
                await db.mark_blocked(uid)
                done = True
                break
            except TelegramAPIError:
                failed += 1
                done = True
                break
        if not done:
            failed += 1
        await asyncio.sleep(0.04)  # ~25 xabar/soniya
    return ok, blocked, failed, len(ids)


def announce_text(movie) -> str:
    e = html.escape
    lines = ["🆕 <b>Yangi kino qo'shildi!</b>\n"]
    lines.append(f"🎬 <b>{e(movie['title'])}</b>" + (f" ({e(movie['year'])})" if movie["year"] else ""))
    if movie["genre"]:
        lines.append(f"🎭 {e(movie['genre'])}")
    if movie["country"]:
        lines.append(f"🌍 {e(movie['country'])}")
    if movie["language"]:
        lines.append(f"🗣 {e(movie['language'])}")
    if movie["quality"]:
        lines.append(f"📺 {e(movie['quality'])}")
    text = "\n".join(lines)
    desc = (movie["description"] or "").strip()
    if desc:
        desc = desc if len(desc) <= 300 else desc[:299].rstrip() + "…"
        text += f"\n\n📝 {e(desc)}"
    text += "\n\n👇 Tomosha qilish uchun tugmani bosing"
    return text[:1024]


async def announce(bot: Bot, movie_id: int) -> None:
    """Yangi kinoni poster bilan barcha foydalanuvchilarga yuboradi, so'ng adminlarga hisobot beradi."""
    movie = await db.get_movie(movie_id)
    if not movie:
        return
    text = announce_text(movie)
    markup = kb.announce_kb(movie_id)

    async def send(uid: int) -> None:
        if movie["poster_id"]:
            try:
                await bot.send_photo(uid, movie["poster_id"], caption=text, reply_markup=markup)
                return
            except TelegramBadRequest:  # poster yaroqsiz bo'lsa — oddiy matn yuboramiz
                pass
        await bot.send_message(uid, text, reply_markup=markup)

    async with lock:
        ok, blocked, failed, total = await run(bot, send)
    await svc.notify_admins(
        bot,
        f"📣 <b>{html.escape(movie['title'])}</b> tarqatildi\n\n"
        f"📨 Yetkazildi: <b>{ok}</b>\n😶 Bloklagan: <b>{blocked}</b>\n⚠️ Xatolik: <b>{failed}</b>\n👥 Jami: <b>{total}</b>",
    )


def announce_many_text(movies) -> str:
    e = html.escape
    lines = [f"🆕 <b>{len(movies)} ta yangi kino qo'shildi!</b>\n"]
    for m in movies[:12]:
        lines.append(
            f"• <b>{e(m['title'])}</b>" + (f" ({e(m['year'])})" if m["year"] else "")
            + (f" — {e(m['genre'])}" if m["genre"] else "")
        )
    if len(movies) > 12:
        lines.append(f"…va yana {len(movies) - 12} ta")
    lines.append("\n👇 Tomosha qilish uchun katalogni oching")
    return "\n".join(lines)[:4000]


async def announce_many(bot: Bot, movie_ids: list[int]) -> None:
    """Bir nechta yangi kino haqida BITTA umumiy xabar yuboradi (bittasi bo'lsa — poster bilan oddiy e'lon)."""
    movies = [m for m in [await db.get_movie(i) for i in movie_ids] if m]
    if not movies:
        return
    if len(movies) == 1:
        await announce(bot, movies[0]["id"])
        return
    text = announce_many_text(movies)
    markup = kb.announce_many_kb()

    async def send(uid: int) -> None:
        await bot.send_message(uid, text, reply_markup=markup)

    async with lock:
        ok, blocked, failed, total = await run(bot, send)
    await svc.notify_admins(
        bot,
        f"📣 <b>{len(movies)} ta kino</b> e'lon qilindi\n\n"
        f"📨 Yetkazildi: <b>{ok}</b>\n😶 Bloklagan: <b>{blocked}</b>\n⚠️ Xatolik: <b>{failed}</b>\n👥 Jami: <b>{total}</b>",
    )


async def publish_all(bot: Bot, notify: bool) -> int:
    """Kutilayotgan hamma kinoni chiqaradi. notify=True bo'lsa foydalanuvchilarga bitta umumiy xabar ketadi."""
    ids = await db.publish_pending()
    if ids and notify:
        _announced.update(ids)

        async def _job():
            try:
                await announce_many(bot, ids)
            except Exception:
                logging.exception("E'lon yuborishda xatolik")

        spawn(_job())
    return len(ids)


def schedule_announce(bot: Bot, movie_id: int) -> bool:
    """Fonda tarqatishni boshlaydi. Shu kino allaqachon tarqatilgan bo'lsa False qaytaradi."""
    if movie_id in _announced:
        return False
    _announced.add(movie_id)

    async def _job():
        try:
            await announce(bot, movie_id)
        except Exception:
            logging.exception("Kinoni tarqatishda xatolik (#%s)", movie_id)

    spawn(_job())
    return True
