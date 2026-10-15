# TG-Control: kechikishlarni nazorat qiluvchi bot (userbot + Bot API)

Bot lidlarning javobsiz qolgan xabarlarini kuzatadi. Menejer 20 daqiqa (sozlanadi) ichida
javob bermasa, **faqat «Проёб» guruhiga** havolalar va xodimga mention yuboradi:

```
https://t.me/c/3924759399/159109
https://t.me/c/3924759399/155150
@xodim_username РД7 задержка
```

Hammasi iPhone'dan, kompyutersiz sozlanadi. Boshqaruv paneli — shaxsiy chatda, 100% inline tugmalar, rus tilida.

## Xavfsizlik qoidalari (kodda shunday qilingan)

1. **Xabar matni o'qilmaydi va saqlanmaydi.** Faqat metadata: chat ID, xabar ID, vaqt, yuboruvchi ID.
   Bazada matn saqlanadigan ustun yo'q. Yagona istisno: «Проёб» guruhidagi `атайду` / `перерыв` / `тут`
   so'zlari (tanaffus nazorati) — matn tekshiriladi, saqlanmaydi.
2. **Bot faqat «Проёб» guruhiga yozadi** va administratorga shaxsiy chatda javob beradi.
   Boshqa guruhlarga yozish kodda yo'q. Istisno — siz o'zingiz ishga tushirgan rassilka (5-bo'lim).

## Ishlash mantig'i

- Telegram'dagi papkalar (RD, RD2, RD3.1 …) nomi bo'yicha botdagi papkalar bilan bog'lanadi, chatlar avtomatik olinadi.
- Chatda lid yozsa va xodim (yoki userbot akkauntining o'zi) javob bermasa — taymer ishlaydi.
- Taymer **faqat ish vaqtida** (standart 10:00–23:00, Toshkent) hisoblanadi. Tunda kelgan xabar ertasi 10:00 dan sanaladi.
- 26-avgustdan oldingi xabarlar e'tiborga olinmaydi; eski lid qayta yozsa — faol bo'ladi.
- Menejer javob bersa, «Проёб»dagi xabar avtomatik «✅ Выполнено» bo'ladi (yoki tugmani bosing).
- Tanaffus: «Проёб»ga `атайду`/`перерыв` → 30 daqiqa, kuniga 3 marta; `тут` → qaytdi. 30 daqiqa o'tib `тут` bo'lmasa:
  «@xodim, время перерыва вышло!». Tanaffus paytida papka taymeri pauzada
  (agar papkada bir nechta xodim bo'lsa — hammasi tanaffusda bo'lganda, aks holda boshqasi javob bera oladi).
- Bot yiqilsa avtomatik qayta ishga tushadi; har 10 daqiqada chatlar holati Telegram bilan qayta tekshiriladi
  (o'tkazib yuborilgan xabarlar uchun himoya). Muammo bo'lsa sizga shaxsiy chatda xabar keladi.

## 1-qadam. Kerakli ma'lumotlar (iPhone, Safari/Telegram)

| Nima | Qayerdan |
|---|---|
| `BOT_TOKEN` | Telegram → @BotFather → `/newbot` |
| `ADMIN_IDS` | Telegram → @userinfobot → «Id» raqami |
| `API_ID`, `API_HASH` | my.telegram.org → telefon raqam bilan kirish → API development tools |

**Muhim:** @BotFather → `/mybots` → botingiz → *Bot Settings* → *Group Privacy* → **Turn off**.
Aks holda bot «Проёб» guruhidagi `атайду`/`тут` xabarlarini ko'rmaydi. Keyin botni «Проёб» guruhiga qo'shing
(administrator qilsangiz yanada ishonchli).

## 2-qadam. GitHub'ga yuklash (iPhone)

1. github.com → ro'yxatdan o'ting → **New repository** → nomi `tgcontrol`, **Private** → Create.
2. Repozitoriy sahifasida **uploading an existing file** havolasini bosing.
3. Ushbu ZIP'ni Files ilovasida oching (ZIP ustiga bosing), **Choose your files** orqali ichidagi
   **barcha fayllarni birdaniga** tanlang (papkalar yo'q, hammasi bitta daraja) → **Commit changes**.
   `.env.example` va `.gitignore` nuqtadan boshlanadigan fayllar — Files'da ko'rinmasa, ular majburiy emas.

## 3-qadam. Railway'ga o'rnatish

1. railway.com → **Login with GitHub** → **New Project** → **Deploy from GitHub repo** → `tgcontrol`.
2. Service → **Variables**: `BOT_TOKEN`, `ADMIN_IDS`, `API_ID`, `API_HASH`, `DATA_DIR=/data`, `TZ_NAME=Asia/Tashkent`.
3. Loyiha sahifasida **+ Create / New → Volume** → servisga ulang → mount path: `/data`
   (Railway interfeysidagi nomlar o'zgarishi mumkin, ma'nosi shu: servisga `/data` papkasida doimiy disk ulash).
   **Bu majburiy**: baza va akkaunt sessiyasi shu yerda saqlanadi, aks holda har yangilanishda yo'qoladi.
4. Service → **Settings** → *Replicas* = **1** (bir nechta nusxa ishlatmang!).
5. **Deploy** tugagach *Deployments → View logs*: `Бот @... запущен` ko'rinishi kerak. Sizga botdan
   «✅ Бот запущен» xabari keladi. Tarif: hisob muddatsiz ishlashi uchun to'liq tarif kerak bo'ladi (taxminan $5/oy),
   yangi hisobdagi sinov krediti to'g'risida shartlarni Railway'da tekshiring.

Ishga tushish vaqti: yig'ish va ishga tushirish odatda 2–4 daqiqa.

## 4-qadam. Botni sozlash (botda, `/menu`)

1. **🔑 Аккаунт юзербота → Подключить**: telefon raqam → Telegram'dan kelgan kod
   (**raqamlarni probel bilan yozing: `1 2 3 4 5`**, aks holda Telegram kodni bekor qiladi) → 2FA paroli (bo'lsa).
   Bu akkaunt barcha ishchi chatlarda bo'lishi kerak.
2. **📁 Папки и сотрудники → Добавить папку**: nomi Telegram'dagi papka nomi bilan **bir xil** (masalan `RD7`).
   Keyin papkani oching → **Добавить сотрудника** (`@username`).
3. **🔎 Найти чат / ID** → «Проёб» deb yozing → ID'ni oling → **👥 Группа «Проёб»** → ID'ni yuboring.
4. **⏱ Лимит ответа**, **🕒 Рабочий день**, **⚙️ Ещё настройки** — kerak bo'lsa o'zgartiring.
5. **📊 Статус** — hammasi 🟢 ekanini tekshiring.

**ID helper:** botga istalgan xabarni forward qiling — Telegram bergan ID qaytadi (kanal, foydalanuvchi, anonim admin).
Oddiy guruhdan forward qilinganda Telegram guruh ID'sini bermaydi — shu uchun «🔎 Найти чат / ID» bor.

## 5-qadam. Push (faqat qo'lda) va funksiyalar

**Topiklar:** guruh forum (topiklar) bo'lsa, har bir topik = bitta lid. Lid statusi — topik ikonkasidagi emoji
(⚡ va 💊 — Push olmaydi va hisobga olinmaydi, qolgan hammasi oladi). Oddiy chatlarda status — chat nomining boshidagi emoji.

**🎛 Функции и выключатели:** bot butunlay / kechikish nazorati / tanaffus / lidlar hisobi / Push — hammasini
alohida yoqib-o'chirish; «♻️ Сбросить настройки». Kechikish nazoratini yoqqaningizda hisob **noldan** boshlanadi —
eski javobsiz xabarlar «Проёб»ga yog'ilmaydi. ⚡ va 💊 lidlar kechikishda ham, Push'da ham yo'q.

**📣 Push** (avtomatik emas, faqat «🚀 Разослать сейчас» bosilganda):
- 🔢 Количество — nechtaga yuborish (istalgan paytda o'zgartiring, 0 = hammasiga)
- 📅 Период лидов — qaysi sanadan qaysi sanagacha paydo bo'lgan lidlarga
- ⏸ Пауза после сообщения — oxirgi xabaridan (lid yoki menejer) beri N daqiqa/soat/kun o'tmagan lidga yubormaslik (`30`, `2ч`, `1д`)
- 🔁 Повтор Push — bir lidga N kun ichida qayta yubormaslik
- 🏷 Эмодзи / 🚫 Исключённые — qaysi emoji olsin/olmasin
- 📨 Способ — akkaunt orqali yoki bot orqali. Xabarlar orasida 2–3 soniya pauza; FloodWait/PeerFlood bo'lsa to'xtaydi.

Push lidning topigiga (yoki chatiga) yoziladi. Shaxsiy akkauntdan ommaviy yuborish cheklashga olib kelishi mumkin.
Rassilka xabarlari menejer javobi deb hisoblanmaydi.

## Birinchi ishga tushirish

Eski javobsiz xabarlar uchun «Проёб»ga xabar **yuborilmaydi** (kechikish nazorati yoqilgan paytdan boshlanadi).
Lidlar soni esa 26-avgustdan boshlab hisoblanadi (**📅 Лиды** → «Пересчитать»).

## Muammolar

| Belgi | Sabab / yechim |
|---|---|
| Statusda «нужен повторный вход» | Sessiya bekor bo'lgan (Telegram → Qurilmalar'dan o'chirilgan yoki sessiya boshqa joyda ishlatilgan). Akkauntni qayta ulang. |
| «нет папки в Telegram» | Papka nomi Telegram'dagidan farq qiladi. Telegram papkasiga chatlar **ro'yxat bilan** qo'shilgan bo'lishi kerak (faqat «Guruhlar» turi bo'yicha emas). |
| «Проёб»ga xabar kelmayapti | ID noto'g'ri yoki bot guruhda yo'q. Botdan «⚠️ Не удаётся отправить…» xabari keladi. |
| `атайду` ishlamayapti | @BotFather'da Group Privacy o'chirilmagan (1-qadam). Sozlashdan keyin botni guruhdan chiqarib, qayta qo'shing. |
| Xodim taymerni to'xtatmayapti | Xodim `@username`i papkada yozilganidan farq qiladi. Papka kartasida ✅ belgisi bo'lishi kerak. |

**Muhim:** bir xil akkaunt sessiyasini ikki joyda (masalan, kompyuterda va Railway'da) bir vaqtda ishlatmang —
Telegram sessiyani bekor qilishi mumkin. Sessiya Railway Volume'dagi bazada saqlanadi: Railway va GitHub
akkauntlaringizni 2FA bilan himoyalang.

## Fayllar

`main.py` — ishga tushirish va nazorat · `userbot.py` — Telethon (faqat metadata) · `monitor.py` — kechikish mantig'i ·
`breaks.py` — tanaffuslar · `broadcast.py` — rassilka · `panel.py`, `screens.py` — admin-panel · `group.py` — «Проёб» ·
`db.py` — SQLite · `worktime.py` — ish vaqti · `notifier.py` — xabar yuborish.
