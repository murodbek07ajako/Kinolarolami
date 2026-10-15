# 🎬 Kino Bot (Telegram bot + Mini App)

## Imkoniyatlar
- Chiroyli uzun salomlashuv + «🎬 Katalog» (Mini App) tugmasi
- Kino kodi yoki nomi bilan qidirish
- Mini App katalog: poster, janr filtri, Yangi / Top / Yil / Saralangan, qidiruv, cheksiz skroll
- Saralanganlar, Top, Yangi, Tasodifiy kino, do'stlarga ulashish (`t.me/bot?start=m_5`)
- Majburiy obuna (bir nechta kanal) + «Tekshirish» tugmasi
- Admin panel `/admin`: kino qo'shish/o'chirish, statistika, ommaviy xabar, kanallar, ban/unban, sozlamalar
- Kontentni himoyalash (forward va saqlashni taqiqlash) — admin sozlamalarida
- Baza zaxirasi: avtomatik adminga yuboriladi, `/restore` bilan tiklanadi

## Telefondan, bepul joylash
**DEPLOY_UZ.md** faylini o'qing (Render.com + UptimeRobot).

## Ishga tushirish (kompyuterda)
1. @BotFather → `/newbot` → token oling
2. `.env.example` ni `.env` ga nusxalang va to'ldiring (BOT_TOKEN, ADMIN_IDS, WEBAPP_URL)
3. `pip install -r requirements.txt`
4. `python main.py`

## Mini App uchun HTTPS (shart)
Telegram Mini App faqat `https://` manzilni qabul qiladi. Bot ichki serverni `PORT` (8080) da ishga tushiradi.

- **Tez sinash:** `cloudflared tunnel --url http://localhost:8080` → chiqqan `https://....trycloudflare.com` manzilni `.env` dagi `WEBAPP_URL` ga yozing
- **Doimiy:** VPS + domen + Caddy: `caddy reverse-proxy --from sizning-domen.uz --to :8080`

## Admin: kino qo'shish
1. Botga `/admin` → «🎬 Kino qo'shish»
2. Videoni yuboring → nom, tavsif, yil, janr, davlat, til, sifat, poster (ixtiyoriylari «O'tkazib yuborish» bilan)
3. Bot kino kodini beradi

**Katta kinolar:** Bot API orqali 50 MB gacha yuklash mumkin. Kattalarini o'z kanalingizga yuklab, botga **forward** qiling — hajm cheklovi yo'q.

## Majburiy obuna
1. Botni kanalga **admin** qiling
2. `/admin` → «📢 Majburiy obuna» → «Kanal qo'shish» → kanaldan post forward qiling yoki `@username` yuboring

## Eslatma
Faqat o'zingizda tarqatish huquqi bor kontentni joylang.

## Yangi imkoniyatlar

**Kodlar.** Har bo'lim o'z raqamini alohida sanaydi: `s.1, s.2` (serial), `m.1` (multfilm), `a.1` (anime).
Eski bazadagi seriallar ishga tushganda avtomatik shu tartibda qayta raqamlanadi.

**Adminlar** (Mini App → ⚙️ Sozlama → 👥 Adminlar):
- 👑 *Bosh admin* — `.env` dagi `ADMIN_IDS`. Hamma huquq, o'chirib bo'lmaydi.
- ⭐ *Starshiy admin* — hamma huquq, oddiy adminlarni qo'shadi/o'chiradi (uni faqat bosh admin tayinlaydi).
- 👤 *Admin* — faqat belgilangan huquqlar (kino qo'shish, o'chirish, chiqarish, xabar, kanallar, ban, statistika, sozlamalar)
  va faqat belgilangan bo'limlar (kino, serial, anime, multfilm).

**Hammaga xabar** (⚙️ Sozlama → 📣): matn, rasm yoki video (video 49 MB gacha). Hisobot botga keladi.
