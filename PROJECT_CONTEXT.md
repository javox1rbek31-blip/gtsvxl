# PROJECT_CONTEXT — TG-Control (Telegram delay-control + Push bot)

Owner: Uzbek speaker, iPhone only. Wants short, simple, step-by-step replies (Uzbek, Latin). Panel UI in Russian; uz switch translates the whole panel (app/i18n_uz.py; data wrapped by h() is never translated).

## Stack / deploy
Python 3.12, aiogram 3 (bot + admin panel), Telethon (main userbot account + Push accounts), SQLite on Railway Volume `/data`.
Flat GitHub repo (`*.py` + Dockerfile copied to `./app/`). Railway Variables: BOT_TOKEN, ADMIN_IDS, API_ID, API_HASH, DATA_DIR=/data, TZ_NAME=Asia/Tashkent.
Deploy = upload all files to GitHub (`/upload/main`); Railway redeploys automatically. Zip: `tgcontrol-deploy.zip` (31 files: 22 .py + Dockerfile, railway.json, requirements.txt, 4 .md, .env.example, .gitignore).

## Hard rules (never break)
1. Never read/store text of client or employee messages — metadata only (ids, time, sender, attachment TYPE: video_note/photo/PDF mime). Two narrow exceptions, text discarded immediately: midbot's "реквизиты отправлены" check (pay feature, off by default) and the «Новый чат» fallback (`_is_opening`, only threads WITHOUT a tracked stavka). Never open PDFs (mime only).
2. Bot writes only to the "Проёб" group and to admins in private chat. To clients: ONLY Push.
3. Secrets in Railway Variables, not in code.

## Domain model
- Lead = forum topic `(chat_id, topic_id)`; status = topic icon emoji. ⚡ and 💊 are always excluded from Push and delay control.
- ALL client messages arrive through a middle bot (relay). Any non-bot human message = staff reply (any employee).
- STAVKA (user rule 2026-10-18): new lead (new topic) → stavka starts; it ends ONLY when 3 round videos (video_note, from anyone) arrived in the thread. While it runs, NOTHING anyone writes (client or staff) counts — no delay timer, no "reply". Missing videos at the deadline → one "ставка" reminder in Проёб (not a delay); stavka continues. Safety cap `botwatch_cap` (180 min) → then normal control.
- Delay = unanswered client message for limit (default 20 working minutes); alert link -> FIRST unanswered message.
- Push: per-folder Push account (main account never pushes), accounts run in parallel; N applies to EACH folder; order today -> yesterday -> prev week -> older; never re-push within 3 h; Stop cancels workers instantly; per-folder quiet hours; media uploaded once per account then reused.
- Alerts to Проёб can be sent by bot / main account / chosen Push account (`alert_via`).
- Roles: view / push / full / owner (ADMIN_IDS).

## Module map (app/)
config (defaults) · db (SQLite) · monitor (delay logic, bet phase) · userbot (Telethon main, event intake, bot-sender detection) · accounts (Push accounts pool) · broadcast (Push audience + run) · notifier (Проёб/admin sending) · services (reports, reminders, backup, health) · breaks · group (Проёб group router: /proyob, /statistika, breaks, stickers) · panel (admin router) · screens (UI text/keyboards) · i18n + i18n_uz (ru/uz; tests guard that every panel phrase has a translation) · features · statuses · links · worktime · context · main.
Tests: `tests/test_logic.py` (170 incl. journal) + `tests/reallib/` (real Telethon/aiogram, run with libs on PYTHONPATH) + `tests/smoke/smoke_*.py` (stub aiogram/Telethon). Environment cannot install aiogram/Telethon -> Telegram-facing code verified only by syntax + stub smoke tests.

## Deploy (qisqa)
Env: BOT_TOKEN, ADMIN_IDS, API_ID, API_HASH, DATA_DIR=/data, TZ_NAME=Asia/Tashkent. Volume: /data. Dockerfile app/*.py ni nusxalaydi. Bot Проёб guruhida admin bo'lishi va /proyob bajarilishi kerak.

## Ставка нового лида — 2026-10-18 (правило владельца)
«Yangi lid kelgach 3 ta dumaloq video tushadi, keyin stavka tugaydi; bo'lmasa stavka davom etadi; shu vaqtda kim yozsa (mijoz yoki boshqa) — e'tiborga olinmaydi. 3 ta video — bot uchun belgi.»
- Начало ставки (`leads.stavka_from`): (S1) живое событие — первое сообщение не-сотрудника в нити, которой ещё нет в `leads`, и `0 < msg_id - topic_id <= 25` (id топика = id служебного «топик создан»); (S3) страховка — `sync_topics` видит свежий топик (`ForumTopic.date`, моложе предела), ставка с даты создания, кружки досчитываются из Telegram (`Userbot.count_bets`: iter_messages(reply_to=topic), только тип video_note). Если Telegram не ответил — ставку не начинаем (обычный контроль). Топик, созданный сотрудником, — не лид.
- Во время ставки (`monitor._absorb_stavka`): любое сообщение не учитывается (ни pending, ни ответ, ни отчёт); кружок от кого угодно → `db.add_bet` (таблица `bets`, один кружок считается один раз — сверка/повтор события не удваивают).
- Конец: `leads.stavka_end` = время N-го кружка (по времени, не по порядку обработки) или предел `botwatch_cap` (180 мин). Принадлежность сообщения к ставке — по его времени (`monitor.stavka_running(lead, need, cap, at_ts)`). Дальше — обычные задержки, только по сообщениям ПОСЛЕ ставки.
- Напоминание «ставка» (`monitor.watch_bots`): если через `botwatch_remind` (46) мин после начала кружков меньше 3 — одно сообщение в Проёб по папке (ссылки + @сотрудники + «ставка»), только в рабочее время, без ⚡/💊 и папок с выключенным контролем; перед отправкой кружки сверяются с Telegram (если Telegram не ответил — пауза 5 мин, ложное не шлём).
- Нити без отслеживаемой ставки (старые лиды, выключатель off): кружок бота при сверке — всегда автоставка; живой кружок бота — сообщение клиента только после N учтённых кружков (таблица `bets`); «Новый чат» распознаётся по началу текста (`opening_enabled`, запасной вариант; в нитях со ставкой текст не читается вообще — `monitor.stavka_owns`).
- Выключатель: 🎛 Функции → «🎬 Ставка нового лида» (`botwatch_enabled`, по умолчанию ВКЛ; при обновлении включается один раз — `features.ensure_stavka_on`). Настройки: 🚨 Проёб → ⚙️ Настройки контроля → «🎬 Ставка: кружки и сроки» (`3 46 180`).
- Проверка на месте: 🚨 Проёб → «🔍 Проверить лид по ссылке» — ставка (кружков x/3, срок, предел), ждёт ли клиент ответа.
- «Новый лид без контакта» не срабатывает, пока идёт ставка (своё напоминание «ставка»).
