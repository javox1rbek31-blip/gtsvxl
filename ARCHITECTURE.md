# ARCHITECTURE — qaysi funksiya qayerda (2026-10-18)

Bog'liqlik: aylanma importlar YO'Q (tekshirilgan). Oqim: `main` → modullar; `db`/`config` eng pastda.

| Funksiya | Asosiy fayl | Sozlama (yoqgich) | Boshqalar bilan aloqa |
|---|---|---|---|
| Zaderjka (kechikish) | monitor.py (`handle_message`, `tick`) | delay_enabled, папка `delay_on` | breaks (pauza vaqti), bet-phase (botwatch) |
| «Новый чат» tanish | statuses.looks_like_opening + userbot._is_opening | opening_enabled | monitor: lead yaratadi, taymer qo'ymaydi |
| Stavka nazorati | monitor.watch_bots / in_bet_phase | botwatch_enabled (+count/wait) | zaderjkani stavka davrida to'xtatadi |
| Perерыв (атайду/тут) | breaks.py + group.py | breaks_enabled | zaderjka taymerini pauza qiladi |
| Push | broadcast.py + sender.py + accounts.py | push_enabled | monitor: o'z xabarlarini e'tiborsiz (`ignore_msg`, push akkaunt ID) |
| Proyob'ga yozish | notifier.py | alert_via | monitor/services/breaks shu orqali yozadi |
| Eslatmalar (оплата, новый лид) | services.py | pay_enabled, newlead_enabled | newlead: kutayotgan (pending) lidlarni o'tkazib yuboradi |
| Hisobotlar/zaxira/health | services.py | report/weekly/backup/health_enabled | |
| Panel (admin tugmalari) | panel.py + screens.py + i18n.py | — | ctx orqali hamma servislar |
| Telegram hisob (kuzatuv) | userbot.py | — | monitor'ga faqat metadata uzatadi |

Qoidalar: (1) matn o'qilmaydi — istisnolar: midbot «реквизиты» va «Новый чат» boshi (≤40 belgi, saqlanmaydi); (2) mijozga faqat Push yozadi; (3) hamma yoqgichlar `features.feature_on` (bosh yoqgich + funksiya).

Ma'lum nuqson: panel.py `build_admin_router` ~1200 qatorli bitta yopiq funksiya (`route` ~500 qator). Telegram'da sinamasdan bo'lish xavfli — keyinroq, real sinovdan keyin.

Sinov: `python3 -m unittest discover -s tests` (unit); `tests/smoke/smoke_*.py` (soxta aiogram/Telethon bilan, `PYTHONPATH=tests/smoke/stubs`).
