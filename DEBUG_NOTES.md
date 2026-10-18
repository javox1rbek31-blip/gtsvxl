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

## 2026-10-18 — funksiyalar tahlili (aralashmalar)
Topildi: (1) Push yuborish kodi monitoring klassi (Userbot) ichida edi -> `sender.py` (PushSender) ga ajratildi, Userbot va PushAccount undan meros oladi; SendAbort/SendSkip eski importlar ishlashi uchun userbot'dan qayta eksport qilinadi. (2) «новый лид без контакта» eslatmasi zaderjka zonasidagi (pending/alerted) lidlarni takrorlardi -> endi o'tkazib yuboradi (+test). (3) Aylanma importlar yo'q; panel.py ichida 1200 qatorli yopiq funksiya bor — sinovsiz bo'lmadim (xavfli).
Natija: 103 unit + 4 smoke (panel, userbot, sender, ...) OK. Smoke skriptlar endi repoda: tests/smoke/. Xarita: ARCHITECTURE.md.

## 2026-10-18 03:50 — hisobotlar, lidlar, asosiy akkaunt
Tasdiqlangan nuqsonlar: (1) kunlik hisobot adminga `parse_mode` siz yuborilardi -> ekranda `<b>` teglari ko'rinardi (tushunarsiz). Endi `notify_admin(html=True)`. (2) reyting faqat o'rtacha tezlik bo'yicha edi (1 ta tez javob 50 ta javobdan yuqori turardi), kim nechta/qancha kechikkani yo'q edi. (3) Lidlar papka bo'yicha faqat qisqa ro'yxatda (6 tagacha) edi.
Tuzatish: services.people_lines (javoblar soni · o'rtacha · maks · limitdan kech), lead_stats_text (papka: bugun/kecha/7 kun/jami + ИТОГО), daily/weekly qayta yozildi, HTML escape; yangi tugma «📁 Лиды по папкам» (Hisobotlar va Lidlar ekrani), db.reply_stats(late_s), db.lead_periods. Asosiy akkaunt almashtirish allaqachon bor edi (Аккаунты → Основной аккаунт (сменить)); so'rov matni tushunarliroq qilindi.
Testlar: 109 unit + 4 smoke OK.

## 2026-10-18 04:10 — menyu tartibi
Tasdiqlandi: akkaunt tugmalari 5 joyda, papkalar Проёб ekranida aralash edi; «Ещё настройки» bosh menyuda va Проёб ichida takror.
Tuzatildi (faqat ekran/tugmalar, routelar o'zgarmadi): yuqoridagi menyu xaritasi. Qo'shimcha xato: `pr:viaset` (kim yozadi) ko'rish huquqi (rank 1) bilan o'zgartirilardi -> endi rank 3.
Testlar: 109 unit + 4 smoke OK (smoke kutilgan matnlari yangilandi).

## 2026-10-18 17:30 — menyu tartibi
Topildi: Push yoqish ikki joyda (papka kartasi `push_on` va alohida «папки для Push» ro'yxati) — ikkalasi ham filtr bo'lgani uchun chalkash. Tuzatildi: bitta tugma (push_on); eski ro'yxat avtomatik ko'chiriladi. «Количество» yorliqlari aniqlashtirildi (umumiy / faqat shu papka). Akkauntlar faqat «📲 Аккаунты»da; Проёб ekrani faqat zaderjka.
Testlar: 110 unit + 4 smoke OK.

## 2026-10-18 — ставка нового лида по правилу владельца (3 кружка)
Жалоба: «zaderjkalar ham xato, stavkani aniqlash ham xato». Старая логика: «фаза ставок» считалась от first_seen по ВРЕМЕНИ (срок 46 мин), сообщения клиента во время ставок ставились в ожидание и после срока давали «задержку»; кружок бота никогда не запускал таймер (даже настоящий кружок клиента позже); «Новый чат» узнавался по тексту.
Новое правило (monitor._absorb_stavka): новый лид → ставка; конец ТОЛЬКО по 3 кружкам (или предел 180 мин); во время ставки ничьи сообщения не учитываются. Распознавание нового лида без текста: id сообщения рядом с id топика (S1) + дата создания топика при сверке (S3, discover_topic). Кружки считаются один раз (таблица bets). Напоминание «ставка» — по папкам, в рабочее время, со сверкой кружков в Telegram. Ставка включается один раз при обновлении (ensure_stavka_on). Новое в панели: «🔍 Проверить лид по ссылке», «🤖 Бот-посредник» в настройках, «🎬 В ставке сейчас» в статусе.
Независимая проверка (отдельный агент, с пробными скриптами) нашла 7 нуждающихся в исправлении мест — все исправлены:
(1) сверка видела последний кружок бота и после обновления делала его «сообщением клиента» (старый завышенный bet_count) → ложная задержка. Теперь сверка никогда не превращает кружок бота в сообщение клиента; живой кружок — только после N учтённых в таблице bets; bets хранятся 365 дней.
(2) одна «битая» нить (топик удалён) блокировала все напоминания «ставка» → повтор по каждой нити отдельно, после 6 неудач — сообщение админу.
(3) пересчёт кружков считал кружки нашего Push → исключены (аккаунты Push, ignored_msgs).
(4)(5) текст «Новый чат» мог читаться в нити со ставкой / при сверке → stavka_owns повторяет условия начала ставки и учитывает live.
(6) ложный алерт, созданный до распознавания ставки, оставался открытым → закрывается с пометкой «это была ставка нового лида».
(7) обработчики событий могут закончить не по порядку → принадлежность к ставке решается по ВРЕМЕНИ сообщения: leads.stavka_end = время N-го кружка.
Повторная проверка того же агента подтвердила исправления и нашла ещё 4 мелочи — исправлены: (A) уведомление, оказавшееся ставкой, не идёт в штрафы/отчёты (status 'void', n_items−1; в «Проёб» пометка «ℹ️ Не задержка»); (C) если конец ставки узнали задним числом (досчитали кружок из Telegram), неотвеченное сообщение клиента после 3-го кружка начинает ждать ответа (_wait_after_stavka; если после него писал сотрудник — нет); (D) в нити с отслеженной ставкой кружок после её конца — сообщение клиента и при сверке; FloodWait/нет связи при пересчёте кружков = «Telegram недоступен», а не «битая нить».
Известное ограничение (B): если ставку распознали задним числом уже ПОСЛЕ её конца и клиент после неё писал несколько раз, ожидание считается от его ПОСЛЕДНЕГО сообщения (первое после ставки не хранится) — уведомление может прийти позже, ложным не будет.
Тесты: 145 unit + smoke_panel/userbot/sender OK; фаззинг (случайные последовательности, перестановка порядка, поздняя проверка топика) — 0 нарушений.
НЕ проверено на живом Telegram: что первое сообщение нового топика имеет id ≤ topic_id+25 (иначе срабатывает S3 через ~4 с), что ForumTopic.date/from_id приходят в Telethon 1.36, iter_messages(reply_to=topic).
Если после ставки бот-посредник сам (не клиент) пишет служебные сообщения — они пока считаются сообщением клиента: нужен скриншот такого сообщения.

## 2026-10-18 вечер — проверка на НАСТОЯЩИХ Telethon 1.36 / aiogram 3.13 (собраны из исходников GitHub)
Найдено настоящими объектами Telethon (заглушки этого не показывали): после КАЖДОГО перезапуска (деплой на Railway) сверка топиков (channels.getForumTopics) получала последнее сообщение без профиля отправителя → ответ менеджера, которого нет в списке сотрудников, считался сообщением клиента → через 20 минут ложная «задержка». Это совпадает с жалобой «менеджер уже написал, а бот пишет задержка». Исправлено: флаг «бот» берётся из списка users того же ответа; уже обработанное сообщение сверка больше не трогает (thread_state.seen_msg_id); человек, которого бот видел раньше, = сотрудник.
Также: форум распознаётся по самому событию (до первой сверки); пересчёт лидов не считает сообщения менеджеров; Push не идёт лиду, у которого идёт ставка.
Новое: журнал решений (таблица journal, 14 дней, без текста): «🔍 Проверить лид» показывает, что бот видел и решил по каждому сообщению; «🧾 Журнал решений за сутки (файл)» в «Проёб» — CSV для разработчика.
Тесты: unit 170 + tests/reallib (19 юзербот на настоящем Telethon, 6 панель на настоящем aiogram — обход ВСЕХ 115 кнопок на ru/uz и с правами «просмотр», проверка HTML/лимитов как в Telegram) + smoke — всё OK.
Не сделано (остановлено по просьбе): полный перевод панели на узбекский (сейчас в режиме uz часть текста остаётся по-русски).

## 2026-10-19 — узбекский режим панели переведён полностью
Было: перевод отдельных слов → в режиме uz смешанные фразы (полрусского, полузбекского).
Стало: app/i18n_uz.py — 700 неизменных кусков строк панели (всё, что в f-строках между {…}) и их перевод; tr() заменяет
их в готовом тексте, длинные первыми, слова только целиком. Данные (названия папок/чатов, имена, текст Push) экраны
оборачивают через h() невидимой меткой U+2063 — внутри метки перевод ничего не трогает; метки убираются перед
отправкой всегда (и в русском режиме). Нижние кнопки, меню «/» и личные уведомления админам — тоже на языке панели.
В группу «Проёб» и клиентам — всегда по-русски (не меняли). «♻️ Сброс настроек» больше не сбрасывает язык.
Ввод на узбекском: «2 soat», «30 daq», «1 kun», «yo'q»/«o'ch» = 0.
Исправлен русский текст подсказки «💳 Минуты»: напоминание получает сотрудник в «Проёб», а не клиент.
Проверки: тест test_every_panel_phrase_has_translation (ast: у каждой русской фразы панели есть перевод — новая фраза
без перевода роняет тест); test_uzbek_panel_has_no_russian_left (настоящий aiogram: обход всех кнопок в uz + неверный
ввод в каждый запрос + проверка лида + отчёты с данными → русских фраз нет); FakeSession падает, если метка ушла в Telegram.
Тесты: unit 155 (Python 3.12 и 3.13) + reallib 26 (3.13) + smoke — OK.
