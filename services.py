"""Bot va Mini App ikkalasi ishlatadigan umumiy mantiq: obuna tekshiruvi, kino yuborish."""
import asyncio
import html
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

import db
import keyboards as kb

SUB_TEXT = (
    "📢 <b>Botdan foydalanish uchun homiy kanallarga obuna bo'ling!</b>\n\n"
    "Quyidagi kanallarga a'zo bo'ling, so'ng <b>«✅ Obunani tekshirish»</b> tugmasini bosing. "
    "Shundan keyin kino darhol yuboriladi. 🍿"
)


async def _is_member(bot: Bot, ch, user_id: int) -> bool:
    try:
        m = await bot.get_chat_member(ch["chat_id"], user_id)
    except Exception as e:  # bot kanalda admin emas / kanal o'chgan — foydalanuvchini to'smaymiz
        logging.warning("Obunani tekshirib bo'lmadi (%s): %s", ch["title"], e)
        return True
    if m.status in ("left", "kicked"):
        return False
    if m.status == "restricted" and not getattr(m, "is_member", True):
        return False
    return True


async def unsubscribed(bot: Bot, user_id: int) -> list:
    """Foydalanuvchi obuna bo'lmagan kanallar ro'yxati (bo'sh bo'lsa — hammasiga obuna)."""
    if await db.get_setting("sub_required", "1") != "1":
        return []
    channels = await db.list_channels()
    if not channels:
        return []
    ok = await asyncio.gather(*[_is_member(bot, ch, user_id) for ch in channels])
    return [ch for ch, good in zip(channels, ok) if not good]


async def ask_subscribe(bot: Bot, chat_id: int, channels: list, movie_id: int = 0) -> None:
    await bot.send_message(chat_id, SUB_TEXT, reply_markup=kb.sub_kb(channels, movie_id))


def caption(movie, bot_username: str) -> str:
    e = html.escape
    head = f"🎬 <b>{e(movie['title'])}</b>" + (f" ({e(movie['year'])})" if movie["year"] else "")
    lines = [head, ""]
    if movie["genre"]:
        lines.append(f"🎭 <b>Janr:</b> {e(movie['genre'])}")
    if movie["country"]:
        lines.append(f"🌍 <b>Davlat:</b> {e(movie['country'])}")
    if movie["language"]:
        lines.append(f"🗣 <b>Til:</b> {e(movie['language'])}")
    if movie["quality"]:
        lines.append(f"📺 <b>Sifat:</b> {e(movie['quality'])}")
    lines.append(f"👁 <b>Ko'rishlar:</b> {movie['views']}")
    lines.append(f"🔢 <b>Kod:</b> <code>{movie['id']}</code>")
    footer = f"\n🤖 @{bot_username}"
    base = "\n".join(lines) + footer
    if movie["description"]:
        room = 1000 - len(base) - 10
        if room > 40:
            d = movie["description"].strip()
            d = d if len(d) <= room else d[: room - 1].rstrip() + "…"
            base = "\n".join(lines) + f"\n\n📝 {e(d)}\n" + footer
    return base[:1024]


async def send_movie(bot: Bot, chat_id: int, user_id: int, movie) -> None:
    me = await bot.me()
    protect = await db.get_setting("protect_content", "0") == "1"
    fav = await db.is_fav(user_id, movie["id"])
    markup = kb.movie_kb(movie["id"], fav, me.username, movie["title"])
    cap = caption(movie, me.username)
    if movie["file_type"] == "video":
        await bot.send_video(chat_id, movie["file_id"], caption=cap, reply_markup=markup,
                             protect_content=protect, supports_streaming=True)
    else:
        await bot.send_document(chat_id, movie["file_id"], caption=cap, reply_markup=markup,
                                protect_content=protect)
    await db.add_view(user_id, movie["id"])


async def deliver(bot: Bot, chat_id: int, user_id: int, movie_id: int) -> str:
    """Kinoni yuboradi. Natija: 'ok' | 'sub' | 'nf' (topilmadi) | 'err' (yuborib bo'lmadi)."""
    movie = await db.get_movie(movie_id)
    if not movie:
        return "nf"
    bad = await unsubscribed(bot, user_id)
    if bad:
        await ask_subscribe(bot, chat_id, bad, movie_id)
        return "sub"
    try:
        await send_movie(bot, chat_id, user_id, movie)
    except (TelegramBadRequest, TelegramForbiddenError) as e:
        logging.error("Kino #%s yuborilmadi: %s", movie_id, e)
        return "err"
    return "ok"
