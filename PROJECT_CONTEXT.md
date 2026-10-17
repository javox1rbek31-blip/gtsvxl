# PROJECT_CONTEXT — TG-Control (Telegram delay-control + Push bot)

Owner: Uzbek speaker, iPhone only. Wants short, simple, step-by-step replies (Uzbek, Latin). Panel UI in Russian (+ uz switch).

## Stack / deploy
Python 3.12, aiogram 3 (bot + admin panel), Telethon (main userbot account + Push accounts), SQLite on Railway Volume `/data`.
Flat GitHub repo (`*.py` + Dockerfile copied to `./app/`). Railway Variables: BOT_TOKEN, ADMIN_IDS, API_ID, API_HASH, DATA_DIR=/data, TZ_NAME=Asia/Tashkent.
Deploy = upload all files to GitHub (`/upload/main`); Railway redeploys automatically. Zip: `tgcontrol-deploy.zip` (25 visible files).

## Hard rules (never break)
1. Never read/store text of client or employee messages — metadata only. Sole exception: midbot's "реквизиты отправлены" check (feature off by default), text discarded. Never open PDFs (mime only).
2. Bot writes only to the "Проёб" group and to admins in private chat. To clients: ONLY Push.
3. Secrets in Railway Variables, not in code.

## Domain model
- Lead = forum topic `(chat_id, topic_id)`; status = topic icon emoji. ⚡ and 💊 are always excluded from Push and delay control.
- ALL client messages arrive through a middle bot (relay). Any non-bot human message = staff reply (any employee).
- After "Новый чат" the middle bot sends 3 bets ~13 min apart: round video (video_note, bot) + text (manager). During the bet phase no delay alerts; if bets are missing at the deadline -> "ставка" reminder in Проёб.
- Delay = unanswered client message for limit (default 20 working minutes); alert link -> FIRST unanswered message.
- Push: per-folder Push account (main account never pushes), accounts run in parallel; N applies to EACH folder; order today -> yesterday -> prev week -> older; never re-push within 3 h; Stop cancels workers instantly; per-folder quiet hours; media uploaded once per account then reused.
- Alerts to Проёб can be sent by bot / main account / chosen Push account (`alert_via`).
- Roles: view / push / full / owner (ADMIN_IDS).

## Module map (app/)
config (defaults) · db (SQLite) · monitor (delay logic, bet phase) · userbot (Telethon main, event intake, bot-sender detection) · accounts (Push accounts pool) · broadcast (Push audience + run) · notifier (Проёб/admin sending) · services (reports, reminders, backup, health) · breaks · group (Проёб group router: /proyob, /statistika, breaks, stickers) · panel (admin router) · screens (UI text/keyboards) · i18n (ru/uz) · features · statuses · links · worktime · context · main.
Tests: `tests/test_logic.py` (94). Environment cannot install aiogram/Telethon -> Telegram-facing code verified only by syntax + stub smoke tests.

## Deploy (qisqa)
Env: BOT_TOKEN, ADMIN_IDS, API_ID, API_HASH, DATA_DIR=/data, TZ_NAME=Asia/Tashkent. Volume: /data. Dockerfile app/*.py ni nusxalaydi. Bot Проёб guruhida admin bo'lishi va /proyob bajarilishi kerak.

## «Новый чат» (служебные сообщения нового лида) — 2026-10-17
Бот-посредник в начале нити шлёт «Новый чат - …» и «Автоставки запущены…». Это НЕ сообщение клиента → таймер задержки не запускается, ссылка в алерте ведёт на первое НАСТОЯЩЕЕ сообщение клиента. Нить (lead.first_seen) создаётся с этого момента — от него считается период автоставок (3 кружка по ~13 мин).
Единственное узкое исключение из «текст не читаем» (кроме «реквизиты отправлены»): `Userbot._is_opening` смотрит ТОЛЬКО первые 40 символов у первых ≤3 сообщений нити (не от человека), сравнивает с фразами `statuses.OPENING_PHRASES`, текст не хранится. Выключатель: Функции → «🆕 Распознавание «Новый чат»» (`opening_enabled`, по умолчанию вкл).
Задержка и контроль ставок независимы: во время ставок (3 кружка не пришли и срок не вышел) задержки нет; не пришли к сроку → «ставка» в Проёб; пришли 3 → обычные задержки только по настоящим сообщениям клиента.
