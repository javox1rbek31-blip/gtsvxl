# DEBUG_NOTES

## Confirmed root causes (fixed in code, covered by tests)
- Push repeated to already-pushed leads after FloodWait stop: repeat filter was 0 -> now always >=180 min (`MIN_REPEAT_MINUTES`).
- Stop button slow: only checked flag between sends -> `Broadcaster.stop()` now cancels worker tasks.
- Push count split between folders -> now N per folder.
- Delay not closed when another/unknown employee replied -> any non-bot human counts as reply (`from_bot is False`).
- Alert link pointed at latest message -> `first_lead_msg_id` stored; alert links to first unanswered.
- Delay fired during bets / false "ставка" warnings -> bet phase logic (`in_bet_phase`, deadline = first + (n-1)*13 + wait min); bot video_note ignored for timers; warning based on bets count by deadline.
- Production crash `prompt() got multiple values for 'target'` on acc:add -> fixed.
- i18n replaced word parts (e.g. "минут") -> whole-word regex.

## Tests performed
94 unit tests OK; panel smoke (stubbed aiogram) OK; userbot smoke OK; all callback routes have handlers.

## NOT verified (no access to real Telegram/Railway from dev env)
Account login for Push, media Push, parallel pushes, `/proyob`, sticker reply, bot-vs-human sender detection (`_is_bot_sender`; if it returns None, legacy behavior applies), exact way the middle bot sends bets (assumed video_note), delay time accuracy in production.

## Unresolved / ask user
- Which leads were pushed that should not be (need example: topic icon).
- Time inaccuracies of delay alerts: suspected stale pending (unrecognized employee replies) — re-check after deploy.
- Bot was kicked from the Проёб group (Forbidden) -> re-add bot as admin, run `/proyob` in the group.

## Next steps
1. User uploads zip files to GitHub (must be signed in), Railway redeploys.
2. Run the 6 checks: Stop, no re-push, any-staff reply closes alert, link -> first message, no delay in bet phase, `/proyob`.
3. Fix whatever fails using screenshot/log. No git repo in dev env; recovery point = `tgcontrol-deploy.zip` + this dir.

## Audit 2026-10-17 (final pass)
- broadcast.py: yuborish + mark_pushed endi asyncio.shield ichida — "Stop" yuborish o'rtasida bosilsa ham xabar belgilanadi (qayta push bo'lmaydi).
- accounts.py (after_identified hook), db.py/userbot.py (people names), services.py (_group_fail_until), monitor.py (_watch_fail_until) — tekshirildi.
- Natija: 94 unit test OK; smoke_panel OK; smoke_userbot OK (stub aiogram/Telethon bilan).
- TEKSHIRILMAGAN (haqiqiy Telegram/Railway kerak): Push akkaunt login, media push, /proyob, stikerlar, _is_bot_sender, stavka = video_note taxmini, kechikish vaqti aniqligi.
- Keyingi qadam: GitHub'ga yuklash -> Railway deploy -> /proyob -> real chatda sinash.

## Chuqur audit 2026-10-17 (mustaqil, 2-bosqich)
Tekshirildi (statik, AST): aniqlanmagan nomlar yo'q; modullararo chaqiruvlar (db./monitor./pool./notifier.) mavjud metodlarga ishora qiladi; tokenlar/sessiya loglarga yozilmaydi; xabar matni faqat midbot "реквизиты" tekshiruvida o'qiladi.
Tekshirildi (haqiqiy): eski sxemali bazadan migratsiya (ma'lumot saqlanadi, qayta ochish ham ishlaydi) — yangi test `test_old_schema_migrates`.
TOPILGAN VA TUZATILGAN: broadcast.run() — `running=True` audience()/start_broadcast() xatosidan OLDIN qo'yilardi; xato bo'lsa Push abadiy "busy" bo'lib qolardi. Endi flag ishga tushirishdan oldin qo'yiladi. Test: `test_broadcast_not_stuck_busy_after_audience_error`.
Ilgari tuzatilgan: yuborish+mark_pushed asyncio.shield ichida (Stop paytida qayta push yo'q).
Natija: 96 unit test OK; smoke_panel OK; smoke_userbot OK (stub bilan). aiogram/Telethon bu muhitda o'rnatilmaydi (tarmoq yopiq) -> ularning haqiqiy API chaqiruvlari tekshirilmagan.
XAVF/ESLATMA: "🤖 Контроль автоставок" (botwatch_enabled) default O'CHIQ. Stavka davrida kechikish bo'lmasligi va "ставка" eslatmasi faqat shu yoqilganda ishlaydi -> Меню → Функции'da yoqilganini tekshiring.
Guruhda bot admin bo'lishi (yoki privacy o'chiq) kerak, aks holda "атайду"/stiker matnlarini ko'rmaydi.

## 2026-10-17 19:50 — «Новый чат» считался сообщением клиента (скриншот Hikmatillo)
Причина (подтверждена кодом): сообщения «Автоставки запущены…» и «Новый чат…» приходят от бота → handle_message считал их сообщением клиента (set_pending) → после ставок/срока приходил алерт «задержка» со ссылкой именно на них, даже если менеджер уже писал.
Исправление: monitor.handle_message/reconcile_top получили `opening`; userbot._is_opening (узкая проверка начала текста) ; statuses.looks_like_opening; features/config `opening_enabled`; db.lead_msg_total.
Тесты: 102 unit OK (+5 TestOpening: не запускает задержку, менеджер писал первым, ссылка на настоящее сообщение, после 3 ставок нет задержки); smoke_userbot расширен сквозной проверкой _on_message. Telethon реально не запускался.
НЕ проверено на живом Telegram: точные тексты (если у бота другое начало — добавить в OPENING_PHRASES). Старые «зависшие» ожидания в базе от прежних версий могут дать один лишний алерт — закройте «Выполнено».
