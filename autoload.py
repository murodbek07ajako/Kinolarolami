"""Kanaldan avtomatik yuklash.

Admin o'z kanaliga kino (video/fayl) tashlaydi, izohiga nom, yil, janr, davlat yozadi —
bot postni o'zi o'qib bazaga qo'shadi, posterni videoning Telegram'dagi kadridan oladi
va (sozlama yoqilgan bo'lsa) hammaga tarqatadi.

Shart: bot kanalda ADMIN bo'lishi va kanal «Avto-yuklash kanallari» ro'yxatida turishi kerak.
"""
import html
import logging
import sqlite3

from aiogram import Bot, F, Router
from aiogram.types import Message

import backup
import broadcast
import caption_parser as cp
import db
import services as svc

router = Router()

VIDEO_EXT = (".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v", ".wmv", ".flv", ".ts")


def _media(m: Message):
    """(media, 'video'|'document') yoki (None, '') — kino bo'lmagan fayllarni o'tkazib yuboradi."""
    if m.video:
        return m.video, "video"
    d = m.document
    if d and ((d.mime_type or "").startswith("video/") or (d.file_name or "").lower().endswith(VIDEO_EXT)):
        return d, "document"
    return None, ""


def post_link(m: Message) -> str:
    if m.chat.username:
        return f"https://t.me/{m.chat.username}/{m.message_id}"
    cid = str(m.chat.id)
    return f"https://t.me/c/{cid[4:] if cid.startswith('-100') else cid.lstrip('-')}/{m.message_id}"


async def process(m: Message, bot: Bot, edited: bool) -> None:
    if not await db.is_source(m.chat.id):
        return
    media, ftype = _media(m)
    if not media:
        return

    info = cp.parse_caption(m.caption or "", getattr(media, "file_name", None))
    existing = await db.movie_by_source(m.chat.id, m.message_id)

    if existing:
        # Post izohi tahrirlansa — kino ma'lumotlari ham yangilanadi (fayl va poster tegilmaydi)
        if edited and info.get("title"):
            await db.update_movie_meta(existing["id"], info)
            await svc.notify_admins(
                bot, f"✏️ Yangilandi: <b>{html.escape(info['title'])}</b> (kod: <code>{existing['id']}</code>)"
            )
        return

    if not info.get("title"):
        await svc.notify_admins(
            bot,
            "⚠️ Kanalga kino tashlandi, lekin <b>nomi topilmadi</b> — qo'shilmadi.\n\n"
            "Post izohining birinchi qatoriga kino nomini yozing (yoki postni tahrirlang — bot qayta o'qiydi).\n"
            f"🔗 {post_link(m)}",
        )
        return

    thumb = getattr(getattr(media, "thumbnail", None), "file_id", "") or ""
    quality = info.get("quality") or (cp.quality_from_size(media.width, media.height) if m.video else "")
    data = {
        **info,
        "quality": quality,
        "file_id": media.file_id,
        "file_type": ftype,
        "poster": thumb,
        "src_chat": m.chat.id,
        "src_msg": m.message_id,
    }
    try:
        movie_id = await db.add_movie(data)
    except sqlite3.IntegrityError:  # shu post allaqachon qo'shilgan (parallel yangilanish)
        return

    me = await bot.me()
    lines = [
        "✅ <b>Kanaldan avtomatik qo'shildi!</b>\n",
        f"🎬 <b>{html.escape(data['title'])}</b>" + (f" ({html.escape(data['year'])})" if data.get("year") else ""),
    ]
    for icon, key in (("🎭", "genre"), ("🌍", "country"), ("🗣", "language"), ("📺", "quality")):
        if data.get(key):
            lines.append(f"{icon} {html.escape(data[key])}")
    lines.append(f"\n🔢 Kod: <code>{movie_id}</code>")
    lines.append(f"🔗 <code>t.me/{me.username}?start=m_{movie_id}</code>")
    if not thumb:
        lines.append("\n🖼 Posterni video kadridan olib bo'lmadi: <code>/poster " + str(movie_id) + "</code> orqali rasm yuboring.")
    await svc.notify_admins(bot, "\n".join(lines))
    backup.schedule(bot)

    if await db.get_setting("auto_post", "1") == "1":
        broadcast.schedule_announce(bot, movie_id)
    else:
        logging.info("Avto-tarqatish o'chiq: kino #%s tarqatilmadi", movie_id)


@router.channel_post(F.video | F.document)
async def on_channel_post(m: Message, bot: Bot):
    await process(m, bot, edited=False)


@router.edited_channel_post(F.video | F.document)
async def on_channel_edit(m: Message, bot: Bot):
    await process(m, bot, edited=True)
