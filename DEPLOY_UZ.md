# Telefondan Render.com ga joylash (bepul)

Kerak: telefon, internet, Telegram. Kompyuter shart emas.

## 1. GitHub akkaunti (kod saqlanadigan joy)
1. github.com → Sign up (email, parol, username)
2. Pastdagi «+» → **New repository** → nom: `kinobot` → **Public** → Create repository

## 2. Fayllarni yuklash
1. `kino_bot.zip` ni telefon fayl menejerida **arxivdan chiqaring** (Extract)
2. GitHub repositoriyada **Add file → Upload files**
3. Chiqqan papkadagi **barcha fayllarni** tanlang (papkalar yo'q, hammasi bir joyda) → **Commit changes**

> `.env.example` ko'rinmasa — muhim emas, kerak emas.

## 3. Render.com
1. render.com → Sign up → **GitHub bilan kiring**
2. **New + → Web Service** → `kinobot` repositoriyasini tanlang
3. Sozlamalar:
   - Language: **Python 3**
   - Build Command: `pip install -r requirements.txt`
   - Start Command: `python main.py`
   - Instance Type: **Free**
4. **Environment Variables** bo'limida qo'shing:
   - `BOT_TOKEN` = @BotFather bergan token
   - `ADMIN_IDS` = sizning Telegram ID (@userinfobot dan)
   - `BOT_TITLE` = bot nomi (masalan: Kino Olami)
   - `PYTHON_VERSION` = `3.12.8`
5. **Create Web Service** → 3–5 daqiqa kuting. «Bot ishga tushdi» yozuvi chiqsa tayyor.

Mini App manzili avtomatik olinadi (Render bergan `https://....onrender.com`), qo'lda yozish shart emas.

## 4. Bot uxlab qolmasligi uchun (muhim!)
Bepul Render server 15 daqiqa jim tursa uxlaydi. Uni uyg'oq saqlang:
1. uptimerobot.com → bepul akkaunt
2. **Add New Monitor** → turi: **HTTP(s)**
3. URL: `https://SIZNING-NOM.onrender.com/health`
4. Interval: **5 minutes** → Create

## 5. Baza yo'qolmasligi uchun
Bepul hostingda fayllar server qayta ishga tushganda o'chib ketadi. Shuning uchun:
- Har kino qo'shilgandan 2 daqiqa keyin va har 6 soatda bot sizga **baza fayli** yuboradi (💾 kino_zaxira_....db)
- **Bu fayllarni Telegramdan o'chirmang!**
- Baza yo'qolsa: botga `/restore` yozing → oxirgi zaxira faylini yuboring → hammasi qaytadi

## Muammo bo'lsa
Render sahifasida **Logs** bo'limini oching va oxirgi qizil yozuvni menga yuboring.
