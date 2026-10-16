"""Oddiy foydalanuvchilar uchun handlerlar."""
import html

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import default_state
from aiogram.types import CallbackQuery, Message

import config
import db
import keyboards as kb
import services as svc

router = Router()

WELCOME = (
    "🎬 <b>Assalomu alaykum, {name}!</b>\n\n"
    "🍿 <b>{title}</b> botiga xush kelibsiz!\n\n"
    "Bu yerda sevimli kinolaringizni <b>tez, qulay va mutlaqo bepul</b> topib, "
    "to'g'ridan-to'g'ri Telegramning o'zida tomosha qilishingiz mumkin.\n\n"
    "━━━━━━━━━━━━━━━\n"
    "📌 <b>Qanday foydalaniladi?</b>\n\n"
    "🔢 <b>Kod bilan</b> — kino kodini yuboring, kino darhol keladi.\n"
    "🔎 <b>Nomi bilan</b> — kino nomini (yoki janrini) yozing, bot qidirib beradi.\n"
    "🎞 <b>Katalog</b> — pastdagi tugma orqali chiroyli mini-ilovani oching: "
    "poster, janr, saralash va qidiruv — hammasi bir joyda.\n"
    "⭐ <b>Saralanganlar</b> — yoqqan kinolarni saqlab qo'ying, keyin oson topasiz.\n"
    "🔥 <b>Top</b> va 🆕 <b>Yangi</b> — eng ommabop va so'nggi qo'shilgan kinolar.\n"
    "━━━━━━━━━━━━━━━\n\n"
    "💡 <i>Maslahat: kino kodini do'stlaringiz bilan ulashing — ular ham bir zumda topishadi!</i>\n\n"
    "👇 Boshlash uchun quyidagi tugmalardan birini tanlang."
)

HELP = (
    "ℹ️ <b>Yordam</b>\n\n"
    "• Kino kodini yuboring — kino keladi\n"
    "• Kino nomini yozing — qidiruv natijalari chiqadi\n\n"
    "<b>Buyruqlar:</b>\n"
    "/start — bosh menyu\n"
    "/top — eng ko'p ko'rilganlar\n"
    "/new — yangi kinolar\n"
    "/random — tasodifiy kino\n"
    "/favs — saralanganlar\n"
    "/help — yordam"
)


def welcome_text(user) -> str:
    return WELCOME.format(name=html.escape(user.first_name or "do'st"), title=html.escape(config.BOT_TITLE))


async def render(event: Message | CallbackQuery, text: str, markup) -> None:
    """Callbackda xabarni tahrirlaydi (imkoni bo'lmasa — yangisini yuboradi)."""
    if isinstance(event, CallbackQuery):
        if event.message:
            try:
                await event.message.edit_text(text, reply_markup=markup)
                return
            except TelegramBadRequest:
                pass
            await event.message.answer(text, reply_markup=markup)
    else:
        await event.answer(text, reply_markup=markup)


async def gate(bot: Bot, event: Message | CallbackQuery, movie_id: int = 0) -> bool:
    """Obunani tekshiradi. Obuna bo'lmasa so'rov yuboradi va False qaytaradi."""
    bad = await svc.unsubscribed(bot, event.from_user.id)
    if not bad:
        return True
    chat_id = event.chat.id if isinstance(event, Message) else event.message.chat.id
    await svc.ask_subscribe(bot, chat_id, bad, movie_id)
    return False


async def reply_deliver(bot: Bot, event: Message | CallbackQuery, movie_id: int) -> str:
    chat_id = event.chat.id if isinstance(event, Message) else event.message.chat.id
    res = await svc.deliver(bot, chat_id, event.from_user.id, movie_id)
    if res == "nf":
        await bot.send_message(chat_id, "❌ Kino topilmadi. U o'chirilgan bo'lishi mumkin.")
    elif res == "err":
        await bot.send_message(chat_id, "⚠️ Kinoni yuborishda xatolik yuz berdi. Birozdan so'ng qayta urinib ko'ring.")
    return res


# ───────────── Buyruqlar ─────────────
@router.message(CommandStart())
async def cmd_start(m: Message, command: CommandObject, bot: Bot, state: FSMContext):
    await state.clear()
    payload = command.args or ""
    mid = int(payload[2:]) if payload.startswith("m_") and payload[2:].isdigit() else 0
    if not await gate(bot, m, mid):
        return
    if mid:
        res = await svc.deliver(bot, m.chat.id, m.from_user.id, mid)
        if res == "ok":
            return
    await m.answer(welcome_text(m.from_user), reply_markup=kb.main_kb(), disable_web_page_preview=True)


@router.message(Command("help"))
async def cmd_help(m: Message):
    await m.answer(HELP, reply_markup=kb.open_catalog_kb())


@router.message(Command("top"))
async def cmd_top(m: Message, bot: Bot):
    await show_top(bot, m)


@router.message(Command("new"))
async def cmd_new(m: Message, bot: Bot):
    await show_new(bot, m)


@router.message(Command("random"))
async def cmd_random(m: Message, bot: Bot):
    await show_random(bot, m)


@router.message(Command("favs"))
async def cmd_favs(m: Message, bot: Bot):
    await show_favs(bot, m)


# ───────────── Ro'yxatlar ─────────────
async def show_top(bot: Bot, ev):
    if not await gate(bot, ev):
        return
    rows = await db.top_movies(10)
    if not rows:
        return await _empty(ev)
    await render(ev, "🔥 <b>Eng ko'p ko'rilgan kinolar</b>\n\nKerakli kinoni tanlang 👇", kb.movies_kb(rows))


async def show_new(bot: Bot, ev):
    if not await gate(bot, ev):
        return
    rows = await db.new_movies(10)
    if not rows:
        return await _empty(ev)
    await render(ev, "🆕 <b>Yangi qo'shilgan kinolar</b>\n\nKerakli kinoni tanlang 👇", kb.movies_kb(rows))


async def show_favs(bot: Bot, ev):
    if not await gate(bot, ev):
        return
    rows = await db.fav_movies(ev.from_user.id)
    if not rows:
        text = "⭐ Saralanganlar ro'yxati hozircha bo'sh.\n\nKino ostidagi «⭐ Saralanganlarga qo'shish» tugmasini bosing."
        return await render(ev, text, kb.movies_kb([]))
    await render(ev, "⭐ <b>Saralangan kinolaringiz</b>\n\nKerakli kinoni tanlang 👇", kb.movies_kb(rows))


async def show_random(bot: Bot, ev):
    if not await gate(bot, ev):
        return
    movie = await db.random_movie()
    if not movie:
        return await _empty(ev)
    await reply_deliver(bot, ev, movie["id"])


async def _empty(ev):
    if isinstance(ev, CallbackQuery):
        await ev.answer("Hozircha kinolar yo'q 😔", show_alert=True)
    else:
        await ev.answer("Hozircha kinolar yo'q 😔")


@router.callback_query(F.data == "top")
async def cb_top(c: CallbackQuery, bot: Bot):
    await c.answer()
    await show_top(bot, c)


@router.callback_query(F.data == "new")
async def cb_new(c: CallbackQuery, bot: Bot):
    await c.answer()
    await show_new(bot, c)


@router.callback_query(F.data == "favs")
async def cb_favs(c: CallbackQuery, bot: Bot):
    await c.answer()
    await show_favs(bot, c)


@router.callback_query(F.data == "rand")
async def cb_rand(c: CallbackQuery, bot: Bot):
    await c.answer()
    await show_random(bot, c)


@router.callback_query(F.data == "menu")
async def cb_menu(c: CallbackQuery):
    await c.answer()
    await render(c, welcome_text(c.from_user), kb.main_kb())


# ───────────── Obuna tekshiruvi ─────────────
@router.callback_query(F.data.startswith("chk:"))
async def cb_check(c: CallbackQuery, bot: Bot):
    mid = int(c.data.split(":")[1] or 0)
    bad = await svc.unsubscribed(bot, c.from_user.id)
    if bad:
        await c.answer("❌ Siz hali hamma kanallarga obuna bo'lmadingiz!", show_alert=True)
        try:
            await c.message.edit_reply_markup(reply_markup=kb.sub_kb(bad, mid))
        except TelegramBadRequest:
            pass
        return
    await c.answer("✅ Rahmat! Obuna tasdiqlandi.")
    chat_id = c.message.chat.id
    try:
        await c.message.delete()
    except TelegramBadRequest:
        pass
    if mid:
        res = await svc.deliver(bot, chat_id, c.from_user.id, mid)
        if res == "ok":
            return
    await bot.send_message(chat_id, welcome_text(c.from_user), reply_markup=kb.main_kb(),
                           disable_web_page_preview=True)


# ───────────── Kino ochish / saralash ─────────────
@router.callback_query(F.data.startswith("m:"))
async def cb_movie(c: CallbackQuery, bot: Bot):
    await c.answer()
    await reply_deliver(bot, c, int(c.data.split(":")[1]))


@router.callback_query(F.data.startswith("fav:"))
async def cb_fav(c: CallbackQuery, bot: Bot):
    mid = int(c.data.split(":")[1])
    movie = await db.get_movie(mid)
    if not movie:
        return await c.answer("Kino topilmadi", show_alert=True)
    now = await db.toggle_fav(c.from_user.id, mid)
    await c.answer("⭐ Saralanganlarga qo'shildi" if now else "💔 Saralanganlardan olib tashlandi")
    me = await bot.me()
    try:
        await c.message.edit_reply_markup(reply_markup=kb.movie_kb(mid, now, me.username, movie["title"]))
    except TelegramBadRequest:
        pass


# ───────────── Matn: kod yoki qidiruv ─────────────
@router.message(StateFilter(default_state), F.text & ~F.text.startswith("/"))
async def on_text(m: Message, bot: Bot):
    q = m.text.strip()
    code = db.parse_series_code(q)
    if code:
        if not await gate(bot, m):
            return
        ser = await db.get_series(code[1])
        eps = await db.episodes(ser["id"]) if ser and ser["kind"] == code[0] else []
        if not eps:
            await m.answer(
                f"❌ <code>{html.escape(q)}</code> kodli {svc.KIND_NAME.get(code[0], 'kontent')} topilmadi.\n\n"
                f"Kodni tekshirib qayta yuboring yoki katalogdan {svc.KIND_NAME.get(code[0], 'kontent')} qidiring.",
                reply_markup=kb.open_catalog_kb(),
            )
            return
        c = db.series_code(ser["kind"], ser["id"])
        await m.answer(
            f"{svc.KIND_EMOJI.get(ser['kind'], '📺')} <b>{html.escape(ser['title'])}</b> — "
            f"<b>{len(eps)}</b> ta qism bor.\n\n"
            f"Qismlarni ko'rish uchun <b>katalogni</b> oching: u yerda qidiruvga "
            f"<code>{c}</code> deb yozing va kerakli qismni tanlang. 👇",
            reply_markup=kb.catalog_query_kb(c),
        )
        return
    if q.isdigit():
        if not await gate(bot, m, int(q)):
            return
        movie = await db.get_movie(int(q))
        # Oddiy raqam faqat yakka kinoga tegishli: serial/multfilm/anime qismlari (series_id bor) kod bilan ochilmaydi
        if movie and movie["series_id"]:
            movie = None
        if not movie:
            await m.answer(
                f"❌ <code>{html.escape(q)}</code> kodli kino topilmadi.\n\n"
                "Kodni tekshirib qayta yuboring yoki kino nomini yozib qidiring.",
                reply_markup=kb.open_catalog_kb(),
            )
            return
        await reply_deliver(bot, m, movie["id"])
        return

    if not await gate(bot, m):
        return
    if len(q) < 2:
        await m.answer("🔎 Qidirish uchun kamida 2 ta belgi yozing.")
        return
    rows, total = await db.search_movies(q, limit=10)
    series = await db.search_series(q, limit=5)
    if series:
        lines = "\n".join(
            f"{svc.KIND_EMOJI.get(x['kind'], '📺')} <b>{html.escape(x['title'])}</b> — kod: <code>"
            f"{db.series_code(x['kind'], x['id'])}</code>" for x in series)
        if not rows:
            await m.answer(
                f"🔎 <b>«{html.escape(q[:60])}»</b> bo'yicha qismli kontent topildi:\n\n{lines}\n\n"
                "Qismlarini katalogdan ko'ring (qidiruvga kodni yozing) 👇",
                reply_markup=kb.catalog_query_kb(db.series_code(series[0]["kind"], series[0]["id"])),
            )
            return
        await m.answer(f"📺 Qismli kontent:\n\n{lines}\n\nQismlarini katalogdan ko'ring.",
                       reply_markup=kb.catalog_query_kb(db.series_code(series[0]["kind"], series[0]["id"])))
    if not rows:
        await m.answer(
            f"😔 <b>«{html.escape(q[:60])}»</b> bo'yicha hech narsa topilmadi.\n\n"
            "Boshqacha yozib ko'ring yoki katalogdan qidiring.",
            reply_markup=kb.open_catalog_kb(),
        )
        return
    more = f"\n\n<i>Yana {total - len(rows)} ta natija bor — katalogdan ko'ring.</i>" if total > len(rows) else ""
    await m.answer(
        f"🔎 <b>«{html.escape(q[:60])}»</b> bo'yicha topildi: <b>{total}</b> ta{more}\n\nKinoni tanlang 👇",
        reply_markup=kb.movies_kb(rows, menu=False),
    )
