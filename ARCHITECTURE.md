# ARCHITECTURE — qaysi funksiya qayerda (2026-10-18)

Bog'liqlik: aylanma importlar YO'Q (tekshirilgan). Oqim: `main` → modullar; `db`/`config` eng pastda.

| Funksiya | Asosiy fayl | Sozlama (yoqgich) | Boshqalar bilan aloqa |
|---|---|---|---|
| Zaderjka (kechikish) | monitor.py (`handle_message`, `tick`) | delay_enabled, папка `delay_on` | breaks (pauza vaqti); stavka paytidagi xabarlar umuman hisobga olinmaydi |
| Yangi lid stavkasi | monitor.py (`_absorb_stavka`, `discover_topic`, `watch_bots`, `stavka_limits`) + db `leads.stavka_from/stavka_end`, `bets` + userbot.count_bets | botwatch_enabled (+count/remind/cap) | 3 ta dumaloq video kelguncha zaderjka yo'q; kelmasa — «ставка» eslatmasi |
| «Новый чат» tanish (zaxira) | statuses.looks_like_opening + userbot._is_opening | opening_enabled | faqat stavka kuzatilmagan nitlar; stavkada matn umuman o'qilmaydi |
| Lidni tekshirish | screens.lead_check + links.parse_message_link | — | faqat holat va vaqt (matnsiz) |
| Perерыв (атайду/тут) | breaks.py + group.py | breaks_enabled | zaderjka taymerini pauza qiladi |
| Push | broadcast.py + sender.py + accounts.py | push_enabled | monitor: o'z xabarlarini e'tiborsiz (`ignore_msg`, push akkaunt ID) |
| Proyob'ga yozish | notifier.py | alert_via | monitor/services/breaks shu orqali yozadi |
| Eslatmalar (оплата, новый лид) | services.py | pay_enabled, newlead_enabled | newlead: kutayotgan (pending) lidlarni o'tkazib yuboradi |
| Hisobotlar/zaxira/health | services.py | report/weekly/backup/health_enabled | |
| Panel (admin tugmalari) | panel.py + screens.py + i18n.py + i18n_uz.py | lang | ctx orqali hamma servislar; ma'lumot faqat h() orqali (tarjima tegmaydi) |
| Telegram hisob (kuzatuv) | userbot.py | — | monitor'ga faqat metadata uzatadi |

Qoidalar: (1) matn o'qilmaydi — istisnolar: midbot «реквизиты» va stavka kuzatilmagan nitda «Новый чат» boshi (≤40 belgi, saqlanmaydi); (2) mijozga faqat Push yozadi; (3) hamma yoqgichlar `features.feature_on` (bosh yoqgich + funksiya).

Ma'lum nuqson: panel.py `build_admin_router` ~1200 qatorli bitta yopiq funksiya (`route` ~500 qator). Telegram'da sinamasdan bo'lish xavfli — keyinroq, real sinovdan keyin.

Sinov: `python3 -m unittest discover -s tests` (unit); `tests/smoke/smoke_*.py` (soxta aiogram/Telethon bilan, `PYTHONPATH=tests/smoke/stubs`).

## Menyu xaritasi (2026-10-18, qayta tartiblangan — har narsa BITTA joyda)
Bosh menyu: 📣 Push · 🚨 Проёб · 📁 Папки и сотрудники · 📲 Аккаунты · 📅 Лиды · 📈 Отчёты · 🎛 Функции · 📊 Статус · 🌐 Til · 📖 Yordam · 👮 Adminlar
- 📲 Аккаунты: asosiy akkaunt (almashtirish) · Push akkauntlar (pauza/nom/o'chirish/qo'shish) · «Кто пишет в Проёб».
- 🚨 Проёб: zaderjka nazorati (guruh, limit, ish kuni) · «🔍 Проверить лид по ссылке» · «⚙️ Настройки контроля» (sana, hisob usuli, perерыв, «🎬 Ставка: кружки и сроки», «🤖 Бот-посредник», to'lov, yangi lid).
- 📁 Папки: xodimlar, jarima, zaderjka yoq/o'ch, Push akkaunti, Push soni (shu papka), tinch vaqt, chatlar.
- 📣 Push: matn/media/shablon, ishga tushirish, umumiy sozlamalar, «🗂 Push по папкам» (papka yoq/o'ch = karta bilan BIR xil tugma), jurnal.
- 📅 Лиды · 📈 Отчёты (xodimlar, papka bo'yicha lidlar, jarima) · 🎛 Функции · 📊 Статус (🎬 hozir stavkada nechta) · 👮 Админы · 🌐 Til.
Eski «push_folders» ro'yxati papkaning push_on tugmasiga ko'chiriladi (features.normalize_push_folders, har ishga tushishda).
