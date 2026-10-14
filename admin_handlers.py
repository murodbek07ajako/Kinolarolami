"""Admin panel (bot ichida)."""
import html
import os
import re
import tempfile

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup, default_state
from aiogram.types import CallbackQuery, Message, MessageOriginChannel

import backup
import broadcast
import caption_parser as cp
import config
import db
import keyboards as kb
import services as svc

router = Router()
router.message.filter(F.from_user.id.in_(config.ADMIN_IDS))
router.callback_query.filter(F.from_user.id.in_(config.ADMIN_IDS))


class AddMovie(StatesGroup):
    video = State()
    step = State()


class DelMovie(StatesGroup):
    code = State()


class AddChannel(StatesGroup):
    wait = State()


class BanUser(StatesGroup):
    wait = State()


class Broadcast(StatesGroup):
    wait = State()
    confirm = State()


class Restore(StatesGroup):
    wait = State()


class PosterSet(StatesGroup):
    wait = State()


# (kalit, so'rov matni, ixtiyoriymi)
FIELDS = [
    ("title", "🎬 <b>Kino nomini</b> yuboring:", False),
    ("description", "📝 Kino <b>tavsifini</b> yuboring (qisqa mazmun):", True),
    ("year", "📅 <b>Yilini</b> yuboring (masalan: <code>2024</code>):", True),
    ("genre", "🎭 <b>Janrini</b> yuboring. Bir nechta bo'lsa vergul bilan:\n<code>Jangari, Drama</code>", True),
    ("country", "🌍 <b>Davlatini</b> yuboring (masalan: <code>AQSH</code>):", True),
    ("language", "🗣 <b>Tilini</b> yuboring (masalan: <code>O'zbek tilida</code>):", True),
    ("quality", "📺 <b>Sifatini</b> yuboring (masalan: <code>1080p</code>):", True),
    ("poster", "🖼 <b>Poster</b> (rasm) yuboring — katalogda ko'rinadi:", True),
]

SETTING_DEFAULTS = {"protect_content": "0", "sub_required": "1", "auto_post": "1"}


async def panel_text() -> str:
    s = await db.stats()
    return (
        "👨‍💻 <b>Admin panel</b>\n\n"
        f"👥 Foydalanuvchilar: <b>{s['users']}</b>\n"
        f"🎬 Kinolar: <b>{s['movies']}</b>\n"
        f"👁 Ko'rishlar: <b>{s['views']}</b>"
    )


async def show_panel(target: Message | CallbackQuery) -> None:
    text = await panel_text()
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=kb.admin_kb())
            return
        except TelegramBadRequest:
            await target.message.answer(text, reply_markup=kb.admin_kb())
            return
    await target.answer(text, reply_markup=kb.admin_kb())


# ───────────── Panel / bekor qilish ─────────────
@router.message(Command("admin"))
async def cmd_admin(m: Message, state: FSMContext):
    await state.clear()
    await show_panel(m)


@router.message(Command("cancel"), StateFilter("*"))
async def cmd_cancel(m: Message, state: FSMContext):
    await state.clear()
    await db.clear_draft(m.from_user.id)
    await m.answer("❌ Bekor qilindi.")
    await show_panel(m)


@router.callback_query(F.data == "adm:cancel", StateFilter("*"))
async def cb_cancel(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await c.answer("Bekor qilindi")
    await show_panel(c)


@router.callback_query(F.data == "adm:panel", StateFilter("*"))
async def cb_panel(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await c.answer()
    await show_panel(c)


# ───────────── Statistika ─────────────
@router.callback_query(F.data == "adm:stats")
async def cb_stats(c: CallbackQuery):
    s = await db.stats()
    top = "\n".join(
        f"{i}. {html.escape(t['title'])} — <b>{t['views']}</b> 👁 (kod: <code>{t['id']}</code>)"
        for i, t in enumerate(s["top"], 1)
    ) or "—"
    text = (
        "📊 <b>Statistika</b>\n\n"
        f"👥 Jami foydalanuvchilar: <b>{s['users']}</b>\n"
        f"🆕 Oxirgi 24 soatda qo'shilgan: <b>{s['new_day']}</b>\n"
        f"📅 Oxirgi 7 kunda qo'shilgan: <b>{s['new_week']}</b>\n"
        f"🟢 Oxirgi 24 soatda faol: <b>{s['active_day']}</b>\n"
        f"🚫 Ban qilinganlar: <b>{s['banned']}</b>\n"
        f"😶 Botni bloklaganlar: <b>{s['blocked']}</b>\n\n"
        f"🎬 Kinolar soni: <b>{s['movies']}</b>\n"
        f"👁 Jami ko'rishlar: <b>{s['views']}</b> (bugun: <b>{s['views_day']}</b>)\n"
        f"📢 Majburiy kanallar: <b>{s['channels']}</b>\n\n"
        f"🏆 <b>Top 5 kino:</b>\n{top}"
    )
    await c.answer()
    await c.message.edit_text(text, reply_markup=kb.back_admin_kb())


# ───────────── Kino qo'shish (bot ichida, qadam-baqadam) ─────────────
@router.callback_query(F.data == "adm:add")
async def add_start(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(AddMovie.video)
    await c.answer()
    await c.message.answer(
        "🎞 <b>Kinoni yuboring</b> (video yoki fayl).\n\n"
        "💡 Katta hajmli kinolar uchun: kinoni avval o'z kanalingizga yuklang, "
        "so'ng shu yerga <b>forward</b> qiling.\n"
        "🤖 Yoki «📥 Avto-yuklash kanali» ni ulang — kanalga tashlasangiz bot o'zi qo'shadi.",
        reply_markup=kb.cancel_kb(),
    )


def _thumb_id(media) -> str:
    return getattr(getattr(media, "thumbnail", None), "file_id", "") or ""


@router.message(AddMovie.video, F.video | F.document)
async def add_video(m: Message, state: FSMContext):
    media = m.video or m.document
    data = {"file_id": media.file_id, "file_type": "video" if m.video else "document",
            "thumb": _thumb_id(media), "step": 0}
    if m.video:
        data["auto_quality"] = cp.quality_from_size(m.video.width, m.video.height)
    await state.update_data(**data)
    await state.set_state(AddMovie.step)
    await ask(m, 0)


@router.message(AddMovie.video)
async def add_video_wrong(m: Message):
    await m.answer("❗ Iltimos, <b>video</b> yoki <b>fayl</b> yuboring.", reply_markup=kb.cancel_kb())


async def ask(m: Message, idx: int) -> None:
    _, text, optional = FIELDS[idx]
    n = f"<i>({idx + 1}/{len(FIELDS)})</i>\n"
    await m.answer(n + text, reply_markup=kb.skip_kb() if optional else kb.cancel_kb())


async def advance(m: Message, state: FSMContext, key: str, value: str) -> None:
    await state.update_data(**{key: value})
    data = await state.get_data()
    idx = data["step"] + 1
    if idx >= len(FIELDS):
        await finish_add(m, state)
        return
    await state.update_data(step=idx)
    await ask(m, idx)


@router.message(AddMovie.step)
async def add_step(m: Message, state: FSMContext):
    data = await state.get_data()
    key = FIELDS[data["step"]][0]
    if key == "poster":
        if not m.photo:
            await m.answer("❗ Rasm (foto) yuboring yoki o'tkazib yuboring.", reply_markup=kb.skip_kb())
            return
        value = m.photo[-1].file_id
    else:
        if not m.text:
            await m.answer("❗ Matn yuboring.")
            return
        value = m.text.strip()
        if key == "year" and not re.fullmatch(r"(18|19|20)\d{2}", value):
            await m.answer("❗ Yilni to'g'ri kiriting (masalan: <code>2024</code>).")
            return
        if key == "title" and len(value) > 120:
            await m.answer("❗ Nom juda uzun (120 belgigacha).")
            return
    await advance(m, state, key, value)


@router.callback_query(F.data == "adm:skip", AddMovie.step)
async def add_skip(c: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    key, _, optional = FIELDS[data["step"]]
    if not optional:
        return await c.answer("Bu maydon majburiy", show_alert=True)
    await c.answer()
    await advance(c.message, state, key, "")


async def announce_saved(m: Message, movie_id: int, data: dict) -> None:
    me = await m.bot.me()
    title = html.escape(data["title"]) + (f" ({html.escape(data['year'])})" if data.get("year") else "")
    text = (
        "✅ <b>Kino qo'shildi!</b>\n\n"
        f"🎬 <b>{title}</b>\n\n"
        f"🔢 Kino kodi: <code>{movie_id}</code>\n"
        f"🔗 Havola: <code>t.me/{me.username}?start=m_{movie_id}</code>"
    )
    await m.answer(text, reply_markup=kb.added_kb(movie_id))
    backup.schedule(m.bot)  # baza nusxasini adminga yuboradi (yo'qolib qolmasligi uchun)


async def finish_add(m: Message, state: FSMContext) -> None:
    data = await state.get_data()
    if not data.get("poster"):
        data["poster"] = data.get("thumb", "")  # poster berilmasa — videoning o'z kadri
    if not data.get("quality"):
        data["quality"] = data.get("auto_quality", "")
    movie_id = await db.add_movie(data)
    await state.clear()
    await announce_saved(m, movie_id, data)


# ───────────── Kino qo'shish (Mini App qoralamasi + video) ─────────────
@router.message(StateFilter(default_state), F.video | F.document)
async def draft_video(m: Message):
    """Mini App'da ma'lumot kiritilgan bo'lsa — yuborilgan video shu kinoga biriktiriladi."""
    draft = await db.get_draft(m.from_user.id)
    if not draft:
        await m.answer(
            "ℹ️ Kino qo'shish uchun avval katalogdagi ⚙️ <b>Admin</b> bo'limida ma'lumotlarni kiriting, "
            "yoki /admin → «🎬 Kino qo'shish» ni bosing."
        )
        return
    media = m.video or m.document
    data = {
        **draft,
        "file_id": media.file_id,
        "file_type": "video" if m.video else "document",
    }
    if not data.get("poster"):
        data["poster"] = _thumb_id(media)
    if not data.get("quality") and m.video:
        data["quality"] = cp.quality_from_size(m.video.width, m.video.height)
    movie_id = await db.add_movie(data)
    await db.clear_draft(m.from_user.id)
    await announce_saved(m, movie_id, data)


@router.message(StateFilter(default_state), F.photo)
async def draft_photo(m: Message):
    """Qoralama bor paytda rasm yuborilsa — u kino posteri bo'ladi."""
    draft = await db.get_draft(m.from_user.id)
    if not draft:
        return
    draft["poster"] = m.photo[-1].file_id
    await db.save_draft(m.from_user.id, draft)
    await m.answer("🖼 Poster qabul qilindi. Endi <b>kinoni</b> (video yoki fayl) yuboring.")


# ───────────── Posterni o'zgartirish ─────────────
@router.message(Command("poster"))
async def cmd_poster(m: Message, command: CommandObject, state: FSMContext):
    arg = (command.args or "").strip()
    if not arg.isdigit():
        return await m.answer("Foydalanish: <code>/poster 12</code> (12 — kino kodi)")
    movie = await db.get_movie(int(arg))
    if not movie:
        return await m.answer("❌ Bunday kodli kino topilmadi.")
    await state.set_state(PosterSet.wait)
    await state.update_data(movie_id=movie["id"])
    await m.answer(f"🖼 <b>{html.escape(movie['title'])}</b> uchun yangi poster (rasm) yuboring:",
                   reply_markup=kb.cancel_kb())


@router.message(PosterSet.wait, F.photo)
async def poster_set(m: Message, state: FSMContext):
    data = await state.get_data()
    await db.set_poster(data["movie_id"], m.photo[-1].file_id)
    await state.clear()
    await m.answer("✅ Poster yangilandi.", reply_markup=kb.back_admin_kb())


@router.message(PosterSet.wait)
async def poster_wrong(m: Message):
    await m.answer("❗ Rasm (foto) yuboring yoki /cancel.")


# ───────────── Hammaga tarqatish ─────────────
@router.callback_query(F.data.startswith("adm:push:"))
async def cb_push(c: CallbackQuery, bot: Bot):
    mid = int(c.data.split(":")[2])
    if not await db.get_movie(mid):
        return await c.answer("Kino topilmadi", show_alert=True)
    if broadcast.schedule_announce(bot, mid):
        await c.answer("📣 Tarqatish boshlandi. Tugagach hisobot keladi.", show_alert=True)
    else:
        await c.answer("Bu kino allaqachon tarqatilgan.", show_alert=True)


# ───────────── Kino o'chirish / ro'yxat ─────────────
@router.callback_query(F.data == "adm:del")
async def del_start(c: CallbackQuery, state: FSMContext):
    await state.set_state(DelMovie.code)
    await c.answer()
    await c.message.answer("🗑 O'chirmoqchi bo'lgan <b>kino kodini</b> yuboring:", reply_markup=kb.cancel_kb())


@router.message(DelMovie.code)
async def del_code(m: Message, state: FSMContext):
    if not (m.text and m.text.strip().isdigit()):
        return await m.answer("❗ Faqat raqam (kino kodi) yuboring.")
    movie = await db.get_movie(int(m.text.strip()))
    if not movie:
        return await m.answer("❌ Bunday kodli kino topilmadi. Qayta yuboring:")
    await state.clear()
    await m.answer(
        f"❓ <b>{html.escape(movie['title'])}</b> (kod: <code>{movie['id']}</code>) o'chirilsinmi?",
        reply_markup=kb.confirm_del_kb(movie["id"]),
    )


@router.callback_query(F.data.startswith("adm:delok:"))
async def del_ok(c: CallbackQuery):
    await db.delete_movie(int(c.data.split(":")[2]))
    await c.answer("🗑 O'chirildi")
    await c.message.edit_text("🗑 Kino o'chirildi.", reply_markup=kb.back_admin_kb())


@router.callback_query(F.data == "adm:list")
async def cb_list(c: CallbackQuery):
    rows, total = await db.search_movies(limit=40)
    lines = [f"<code>{r['id']}</code> — {html.escape(r['title'])}" + (f" ({r['year']})" if r["year"] else "")
             + f" • 👁{r['views']}" for r in rows]
    text = f"📋 <b>Kinolar</b> (jami: {total}, oxirgi {len(rows)} ta)\n\n" + ("\n".join(lines) or "Hozircha bo'sh.")
    await c.answer()
    await c.message.edit_text(text[:4000], reply_markup=kb.back_admin_kb())


# ───────────── Kanallar: majburiy obuna va avto-yuklash manbalari ─────────────
def _parse_chat_ref(m: Message):
    if isinstance(m.forward_origin, MessageOriginChannel):
        return m.forward_origin.chat.id
    return svc.parse_ref(m.text) if m.text else None


async def channels_text() -> tuple[str, list]:
    chans = await db.list_channels()
    if not chans:
        return "📢 <b>Majburiy obuna</b>\n\nHozircha kanal qo'shilmagan.", chans
    lines = [f"{i}. {html.escape(ch['title'])} — {ch['link']}" for i, ch in enumerate(chans, 1)]
    return "📢 <b>Majburiy obuna kanallari</b>\n\n" + "\n".join(lines) + "\n\n<i>O'chirish uchun kanal tugmasini bosing.</i>", chans


SOURCE_HELP = (
    "📥 <b>Avto-yuklash kanallari</b>\n\n"
    "Shu kanallarga tashlangan kino (video/fayl) bot tomonidan <b>avtomatik</b> bazaga qo'shiladi "
    "va foydalanuvchilarga tarqatiladi.\n\n"
    "📝 <b>Post izohi (caption) shunday bo'lsin:</b>\n"
    "<code>Qasoskor\nYili: 2023\nJanri: Jangari, Drama\nDavlati: AQSH\nTili: O'zbek tilida</code>\n"
    "<i>Faqat birinchi qator (nom) shart, qolganlari ixtiyoriy.</i>\n\n"
)


async def sources_text() -> tuple[str, list]:
    srcs = await db.list_sources()
    if not srcs:
        return SOURCE_HELP + "Hozircha kanal ulanmagan.", srcs
    lines = [f"{i}. {html.escape(s['title'] or str(s['chat_id']))}" for i, s in enumerate(srcs, 1)]
    return SOURCE_HELP + "<b>Ulangan kanallar:</b>\n" + "\n".join(lines), srcs


@router.callback_query(F.data == "adm:ch")
async def cb_channels(c: CallbackQuery):
    text, chans = await channels_text()
    await c.answer()
    await c.message.edit_text(text, reply_markup=kb.channels_kb(chans), disable_web_page_preview=True)


@router.callback_query(F.data.startswith("adm:chdel:"))
async def cb_chdel(c: CallbackQuery):
    await db.del_channel(int(c.data.split(":")[2]))
    await c.answer("🗑 O'chirildi")
    text, chans = await channels_text()
    await c.message.edit_text(text, reply_markup=kb.channels_kb(chans), disable_web_page_preview=True)


@router.callback_query(F.data == "adm:src")
async def cb_sources(c: CallbackQuery):
    text, srcs = await sources_text()
    await c.answer()
    await c.message.edit_text(text, reply_markup=kb.sources_kb(srcs))


@router.callback_query(F.data.startswith("adm:srcdel:"))
async def cb_srcdel(c: CallbackQuery):
    await db.del_source(int(c.data.split(":")[2]))
    await c.answer("🗑 Olib tashlandi")
    text, srcs = await sources_text()
    await c.message.edit_text(text, reply_markup=kb.sources_kb(srcs))


@router.callback_query(F.data == "adm:chadd")
async def ch_add_start(c: CallbackQuery, state: FSMContext):
    await state.set_state(AddChannel.wait)
    await state.update_data(kind="sub")
    await c.answer()
    await c.message.answer(
        "📢 Kanalni qo'shish uchun:\n\n"
        "1️⃣ Botni kanalga <b>admin</b> qiling\n"
        "2️⃣ Kanaldan istalgan postni shu yerga <b>forward</b> qiling, "
        "yoki kanal <code>@username</code> ini yuboring.",
        reply_markup=kb.cancel_kb(),
    )


@router.callback_query(F.data == "adm:srcadd")
async def src_add_start(c: CallbackQuery, state: FSMContext):
    await state.set_state(AddChannel.wait)
    await state.update_data(kind="source")
    await c.answer()
    await c.message.answer(
        "📥 Kino tashlaydigan <b>o'z kanalingizni</b> ulang:\n\n"
        "1️⃣ Botni kanalga <b>admin</b> qiling (postlarni o'qishi uchun)\n"
        "2️⃣ Kanaldan istalgan postni shu yerga <b>forward</b> qiling, "
        "yoki kanal <code>@username</code> ini yuboring.",
        reply_markup=kb.cancel_kb(),
    )


@router.message(AddChannel.wait)
async def ch_add(m: Message, state: FSMContext, bot: Bot):
    ref = _parse_chat_ref(m)
    if ref is None:
        return await m.answer("❗ Kanaldan post forward qiling yoki <code>@username</code> yuboring.")
    kind = (await state.get_data()).get("kind", "sub")
    try:
        chat_id, title, link = await svc.resolve_channel(
            bot, ref, need_link=(kind == "sub"), channel_only=(kind == "source")
        )
    except ValueError as e:
        return await m.answer(f"❌ {html.escape(str(e))}")
    await state.clear()
    if kind == "sub":
        await db.add_channel(chat_id, title, link)
        text, chans = await channels_text()
        await m.answer("✅ Kanal qo'shildi!\n\n" + text, reply_markup=kb.channels_kb(chans),
                       disable_web_page_preview=True)
    else:
        await db.add_source(chat_id, title)
        text, srcs = await sources_text()
        await m.answer("✅ Kanal ulandi! Endi shu kanalga kino tashlasangiz bot o'zi qo'shadi.\n\n" + text,
                       reply_markup=kb.sources_kb(srcs))


# ───────────── Ban / Unban ─────────────
@router.callback_query(F.data == "adm:ban")
async def ban_start(c: CallbackQuery, state: FSMContext):
    await state.set_state(BanUser.wait)
    await c.answer()
    await c.message.answer(
        "🚫 Foydalanuvchi <b>ID</b> raqamini yuboring.\nBan qilingan bo'lsa — ban olib tashlanadi.",
        reply_markup=kb.cancel_kb(),
    )


@router.message(BanUser.wait)
async def ban_do(m: Message, state: FSMContext):
    if not (m.text and m.text.strip().lstrip("-").isdigit()):
        return await m.answer("❗ Faqat ID raqam yuboring.")
    uid = int(m.text.strip())
    if uid in config.ADMIN_IDS:
        return await m.answer("😅 Adminni ban qilib bo'lmaydi.")
    banned = await db.is_banned(uid)
    await db.set_ban(uid, not banned)
    await state.clear()
    await m.answer(
        f"{'✅ Ban olib tashlandi' if banned else '🚫 Ban qilindi'}: <code>{uid}</code>",
        reply_markup=kb.back_admin_kb(),
    )


# ───────────── Sozlamalar ─────────────
async def settings_markup():
    protect = await db.get_setting("protect_content", "0") == "1"
    sub = await db.get_setting("sub_required", "1") == "1"
    auto = await db.get_setting("auto_post", "1") == "1"
    return kb.settings_kb(protect, sub, auto)


SETTINGS_TEXT = (
    "⚙️ <b>Sozlamalar</b>\n\n"
    "🔒 <b>Kontentni himoyalash</b> — yoqilsa, kinoni forward qilish va saqlab olish taqiqlanadi.\n"
    "📢 <b>Majburiy obuna</b> — o'chirilsa, kanallar tekshirilmaydi.\n"
    "📣 <b>Avto-tarqatish</b> — kanaldan yangi kino qo'shilganda hamma foydalanuvchiga e'lon yuboriladi."
)


@router.callback_query(F.data == "adm:set")
async def cb_settings(c: CallbackQuery):
    await c.answer()
    await c.message.edit_text(SETTINGS_TEXT, reply_markup=await settings_markup())


@router.callback_query(F.data.startswith("adm:tgl:"))
async def cb_toggle(c: CallbackQuery):
    key = c.data.split(":")[2]
    if key not in SETTING_DEFAULTS:
        return await c.answer()
    cur = await db.get_setting(key, SETTING_DEFAULTS[key])
    await db.set_setting(key, "0" if cur == "1" else "1")
    await c.answer("Saqlandi ✅")
    await c.message.edit_reply_markup(reply_markup=await settings_markup())


# ───────────── Zaxira / tiklash ─────────────
@router.callback_query(F.data == "adm:backup")
async def cb_backup(c: CallbackQuery, bot: Bot):
    await c.answer("Tayyorlanmoqda...")
    n = await backup.send_backup(bot, [c.from_user.id])
    if not n:
        await c.message.answer("⚠️ Zaxirani yuborib bo'lmadi.")


@router.message(Command("restore"), StateFilter("*"))
async def cmd_restore(m: Message, state: FSMContext):
    await state.set_state(Restore.wait)
    await m.answer(
        "♻️ <b>Bazani tiklash</b>\n\nBot yuborgan zaxira faylini (<code>kino_zaxira_....db</code>) shu yerga yuboring.\n\n"
        "⚠️ Joriy ma'lumotlar zaxiradagisi bilan almashtiriladi.",
        reply_markup=kb.cancel_kb(),
    )


@router.callback_query(F.data == "adm:restore")
async def cb_restore(c: CallbackQuery, state: FSMContext):
    await c.answer()
    await cmd_restore(c.message, state)


@router.message(Restore.wait, F.document)
async def do_restore(m: Message, state: FSMContext, bot: Bot):
    if m.document.file_size and m.document.file_size > 20 * 1024 * 1024:
        return await m.answer("❌ Fayl juda katta (20 MB gacha bo'lishi kerak).")
    tmp = os.path.join(tempfile.gettempdir(), "kino_restore.db")
    await bot.download(m.document, destination=tmp)
    if not db.validate_backup(tmp):
        return await m.answer("❌ Bu fayl kino bot zaxirasi emas. To'g'ri faylni yuboring.")
    await db.replace_from(tmp)
    await state.clear()
    s = await db.stats()
    await m.answer(
        "✅ <b>Baza tiklandi!</b>\n\n"
        f"🎬 Kinolar: <b>{s['movies']}</b>\n👥 Foydalanuvchilar: <b>{s['users']}</b>",
        reply_markup=kb.back_admin_kb(),
    )


@router.message(Restore.wait)
async def restore_wrong(m: Message):
    await m.answer("❗ Zaxira <b>faylini</b> yuboring yoki /cancel.")


# ───────────── Ommaviy xabar ─────────────
@router.callback_query(F.data == "adm:bc")
async def bc_start(c: CallbackQuery, state: FSMContext):
    await state.set_state(Broadcast.wait)
    await c.answer()
    await c.message.answer(
        "📣 Foydalanuvchilarga yuboriladigan <b>xabarni</b> yuboring.\n"
        "Matn, rasm, video — istalgani bo'ladi (tugmalar bilan ham).",
        reply_markup=kb.cancel_kb(),
    )


@router.message(Broadcast.wait)
async def bc_preview(m: Message, state: FSMContext):
    await state.update_data(src_chat=m.chat.id, src_msg=m.message_id)
    await state.set_state(Broadcast.confirm)
    total = len(await db.broadcast_ids())
    await m.answer(f"👆 Xabar shunday ko'rinadi.\n\n<b>{total}</b> ta foydalanuvchiga yuborilsinmi?",
                   reply_markup=kb.broadcast_confirm_kb())


@router.callback_query(F.data == "adm:bcok", Broadcast.confirm)
async def bc_go(c: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    await state.clear()
    await c.answer("Yuborish boshlandi 🚀")
    await c.message.edit_text("🚀 Xabar yuborish boshlandi. Tugagach hisobot keladi.")
    broadcast.spawn(run_broadcast(bot, c.from_user.id, data["src_chat"], data["src_msg"]))


async def run_broadcast(bot: Bot, admin_id: int, src_chat: int, src_msg: int) -> None:
    async def send(uid: int) -> None:
        await bot.copy_message(uid, src_chat, src_msg)

    async with broadcast.lock:
        ok, blocked, failed, total = await broadcast.run(bot, send)
    await bot.send_message(
        admin_id,
        "✅ <b>Xabar yuborish tugadi</b>\n\n"
        f"📨 Yetkazildi: <b>{ok}</b>\n"
        f"😶 Botni bloklagan: <b>{blocked}</b>\n"
        f"⚠️ Xatolik: <b>{failed}</b>\n"
        f"👥 Jami: <b>{total}</b>",
        reply_markup=kb.back_admin_kb(),
    )
