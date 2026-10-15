"""Экраны админ-панели (тексты и инлайн-кнопки, 100% на русском).

Каждый экран — функция, возвращающая (текст HTML, клавиатура). Telegram-объекты
импортируются лениво, чтобы модуль можно было проверять без установленного aiogram.
"""
from __future__ import annotations

import html
import time

from .context import Ctx
from .features import FEATURES, emoji_lists, feature_on, master_on
from .links import message_link
from .statuses import classify
from .worktime import fmt_local

h = html.escape

STATE_LABELS = {
    "online": "🟢 подключён",
    "connecting": "🟡 подключается…",
    "starting": "🟡 запускается…",
    "offline": "🔴 нет связи, переподключаюсь",
    "unauthorized": "🔴 нужен повторный вход",
    "no_session": "⚪ аккаунт не подключён",
    "disabled": "⚪ не заданы API_ID / API_HASH",
}


def kb(*rows):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows]
    )


def cancel_kb(back: str = "m"):
    return kb([("❌ Отмена", back)])


def ago(ts: float) -> str:
    if not ts:
        return "ещё не было"
    sec = int(time.time() - ts)
    if sec < 90:
        return "только что"
    if sec < 5400:
        return f"{sec // 60} мин назад"
    return f"{sec // 3600} ч назад"


# ------------------------------------------------------------------ главное меню
def onoff(v: bool) -> str:
    return "✅ вкл" if v else "⛔ выкл"


def main(ctx: Ctx):
    db = ctx.db
    group = db.get("proyob_group_id") or "не задана ⚠️"
    text = (
        "🛠 <b>Панель управления</b>\n\n"
        f"Бот: {'🟢 ВКЛЮЧЁН' if master_on(db) else '🔴 ВЫКЛЮЧЕН'}\n"
        f"Юзербот: {STATE_LABELS.get(ctx.userbot.state, ctx.userbot.state)}\n"
        f"Группа «Проёб»: <code>{h(group)}</code>\n"
        f"Задержки: {onoff(feature_on(db, 'delay_enabled'))} · лимит <b>{db.get('limit_minutes')} мин</b> · "
        f"рабочий день <b>{db.get('work_start')}–{db.get('work_end')}</b>\n"
        f"Папок: {len(db.folders())}"
    )
    return text, kb(
        [("📁 Папки и сотрудники", "f:list"), ("⏱ Лимит ответа", "s:limit")],
        [("🕒 Рабочий день", "s:hours"), ("👥 Группа «Проёб»", "s:group")],
        [("📣 Push (горячие лиды)", "p:menu"), ("📅 Лиды", "l:0")],
        [("🎛 Функции и выключатели", "fn"), ("🔑 Аккаунт юзербота", "a:menu")],
        [("🔎 Найти чат / ID", "x:find"), ("⚙️ Ещё настройки", "s:more")],
        [("📊 Статус", "st"), ("📖 Помощь", "help")],
    )


def functions(ctx: Ctx):
    db = ctx.db
    lines = [f"Главный выключатель: {'🟢 ВКЛ' if master_on(db) else '🔴 ВЫКЛ'}"]
    rows = [[(("🔴 Выключить бота целиком" if master_on(db) else "🟢 Включить бота"), "fn:t:bot_enabled")]]
    for key, (title, note) in FEATURES.items():
        on = db.get(key) == "1"
        lines.append(f"{title}: {onoff(on)} — <i>{note}</i>")
        rows.append([((f"⛔ Выключить: {title}" if on else f"✅ Включить: {title}"), f"fn:t:{key}")])
    since = db.get_int("alerts_from")
    note = (f"\n\nЗадержки считаются по сообщениям с <b>{fmt_local(since, ctx.tz)}</b> — "
            "при включении отсчёт идёт с нуля, старые неотвеченные не порождают уведомлений.") if since else ""
    rows += [[("♻️ Сбросить настройки к заводским", "fn:reset")], [("⬅️ Назад", "m")]]
    text = "🎛 <b>Функции и выключатели</b>\n\n" + "\n".join(lines) + note
    return text, kb(*rows)


# ------------------------------------------------------------------ папки
def folders(ctx: Ctx):
    rows = []
    for f in ctx.db.folders():
        n_e = len(ctx.db.employees(f["id"]))
        n_c = len(ctx.db.chats(f["id"]))
        rows.append([(f"📁 {f['name']} · 👤{n_e} · 💬{n_c}", f"f:o:{f['id']}")])
    rows += [
        [("➕ Добавить папку", "f:add")],
        [("🔄 Подтянуть чаты из Telegram", "f:sync")],
        [("⬅️ Назад", "m")],
    ]
    text = (
        "📁 <b>Папки и сотрудники</b>\n\n"
        "Название папки здесь должно совпадать с названием папки в Telegram "
        "(у аккаунта юзербота) — чаты подтянутся автоматически.\n"
        + ("" if ctx.db.folders() else "\n<i>Папок пока нет — нажмите «Добавить папку».</i>")
    )
    return text, kb(*rows)


def folder_card(ctx: Ctx, fid: int):
    f = ctx.db.folder(fid)
    if f is None:
        return folders(ctx)
    emps = ctx.db.employees(fid)
    chats = ctx.db.chats(fid)
    emp_line = ", ".join(f"@{h(e['username'])} {'✅' if e['user_id'] else '⏳'}" for e in emps) or "не назначены ⚠️"
    manual = sum(1 for c in chats if c["source"] == "manual")
    text = (
        f"📁 <b>{h(f['name'])}</b>\n\n"
        f"👤 Сотрудники: {emp_line}\n"
        f"💬 Чатов под контролем: {len(chats)} (вручную: {manual})\n\n"
        "<i>✅ — ID сотрудника определён, ⏳ — определится сам при первой проверке.</i>"
    )
    rows = [
        [("👤➕ Добавить сотрудника", f"e:add:{fid}")],
        *[[(f"❌ @{e['username']}", f"e:del:{e['id']}")] for e in emps[:10]],
        [("💬 Чаты папки", f"c:l:{fid}")],
        [("✏️ Переименовать", f"f:ren:{fid}"), ("🗑 Удалить", f"f:del:{fid}")],
        [("⬅️ К папкам", "f:list")],
    ]
    return text, kb(*rows)


def chats(ctx: Ctx, fid: int):
    f = ctx.db.folder(fid)
    if f is None:
        return folders(ctx)
    rows_db = ctx.db.chats(fid)
    lines = [f"• {h(c['title'] or 'без названия')} — <code>{c['chat_id']}</code>" for c in rows_db[:25]]
    more = f"\n… и ещё {len(rows_db) - 25}" if len(rows_db) > 25 else ""
    text = (
        f"💬 <b>Чаты папки {h(f['name'])}</b> ({len(rows_db)})\n\n"
        + ("\n".join(lines) if lines else "<i>Чатов нет. Проверьте, что в Telegram есть папка с таким же названием.</i>")
        + more
    )
    rows = [[(f"❌ {(c['title'] or str(c['chat_id']))[:24]}", f"c:del:{fid}:{c['chat_id']}")] for c in rows_db[:8]]
    rows += [[("➕ Добавить чат по ID", f"c:add:{fid}")], [("⬅️ Назад к папке", f"f:o:{fid}")]]
    return text, kb(*rows)


# ------------------------------------------------------------------ настройки
def more_settings(ctx: Ctx):
    db = ctx.db
    timer = "от ПЕРВОГО неотвеченного" if db.get("timer_from") != "last" else "от ПОСЛЕДНЕГО сообщения"
    text = (
        "⚙️ <b>Дополнительные настройки</b>\n\n"
        f"📆 Отслеживание с даты: <b>{h(db.get('track_from'))}</b>\n"
        f"⏳ Отсчёт 20 минут: <b>{timer}</b>\n"
        f"☕ Перерыв: <b>{db.get('break_minutes')} мин</b>, до <b>{db.get('breaks_per_day')}</b> раз в день\n"
        f"🤖 Автоставок после нового чата: <b>{db.get('botwatch_count')}</b>, ждать: <b>{db.get('botwatch_wait')} мин</b>"
    )
    return text, kb(
        [("📆 Дата начала отслеживания", "s:track")],
        [("⏳ Переключить способ отсчёта", "s:timer")],
        [("☕ Перерывы (минуты и раз в день)", "s:brk")],
        [("🤖 Автоставки: сколько и сколько ждать", "s:bw")],
        [("⬅️ Назад", "m")],
    )


# ------------------------------------------------------------------ статус
def status(ctx: Ctx):
    db = ctx.db
    now = int(time.time())
    by = db.count_by_status()
    chats_total = len(db.chat_ids())
    up = int(now - ctx.started_at)
    missing = ctx.userbot.missing_folders
    lc = lead_counts(ctx)
    text = (
        "📊 <b>Статус</b>\n\n"
        f"Юзербот: {STATE_LABELS.get(ctx.userbot.state, ctx.userbot.state)}"
        f"{' — ' + h(ctx.userbot.me_label) if ctx.userbot.me_label else ''}\n"
        f"Последняя сверка чатов: {ago(ctx.userbot.last_reconcile)}\n"
        f"Последнее событие: {ago(ctx.userbot.last_event)}\n"
        f"Группа «Проёб»: <code>{h(db.get('proyob_group_id') or '—')}</code>\n\n"
        f"Папок: {len(db.folders())} · чатов под контролем: {chats_total}\n"
        f"⏳ Ждут ответа: {by.get('pending', 0)} · 🚨 В «Проёб»: {by.get('alerted', 0)}\n"
        f"Открытых уведомлений: {len(db.open_alerts())}\n"
        f"Лидов с {h(db.get('track_from'))}: {lc['total']} · 🔥 {lc['push']} · "
        f"исключённых {lc['skip']}\n"
        f"Нажали «Старт» у бота: {len(db.reachable_bot_users())}\n"
        f"Функции: задержки {onoff(feature_on(db, 'delay_enabled'))}, перерывы {onoff(feature_on(db, 'breaks_enabled'))}, "
        f"учёт лидов {onoff(feature_on(db, 'leads_enabled'))}\n\n"
        f"Аптайм: {up // 3600} ч {up % 3600 // 60} мин"
    )
    if missing:
        text += "\n\n⚠️ В Telegram нет папок: " + ", ".join(h(m) for m in missing)
    return text, kb([("🔄 Обновить", "st"), ("⬅️ Назад", "m")])


# ------------------------------------------------------------------ лиды (подсчёт)
def lead_counts(ctx: Ctx) -> dict[str, int]:
    since = ctx.monitor.track_from_ts()
    push_e, skip_e = emoji_lists(ctx.db)
    out = {"total": 0, "push": 0, "skip": 0, "none": 0}
    for r in ctx.db.leads_with_labels():
        if r["first_seen"] < since:
            continue
        out["total"] += 1
        out[classify(r["label"], push_e, skip_e)] += 1
    return out


def dur_label(minutes: int) -> str:
    if minutes <= 0:
        return "выкл"
    if minutes % 1440 == 0:
        return f"{minutes // 1440} дн"
    if minutes % 60 == 0:
        return f"{minutes // 60} ч"
    return f"{minutes} мин"


def period_label(ctx: Ctx) -> str:
    frm = ctx.db.get("push_from") or ctx.db.get("track_from")
    return f"{h(frm)} — {h(ctx.db.get('push_to') or 'сегодня')}"


# ------------------------------------------------------------------ Push
def push_menu(ctx: Ctx):
    db = ctx.db
    mode = db.get("push_mode")
    aud = ctx.broadcaster.audience(mode)
    mode_s = "с вашего аккаунта" if mode == "userbot" else "через бота (только нажавшим «Старт»)"
    preview = db.get("push_text").strip()
    preview = (h(preview[:200]) + ("…" if len(preview) > 200 else "")) if preview else "<i>не задан</i>"
    push_e, skip_e = emoji_lists(db)
    limit = aud["limit"]
    text = (
        "📣 <b>Push — горячие лиды</b>\n\n"
        "Отправляется <b>только по вашей кнопке</b>, автоматического расписания нет.\n\n"
        f"Получают: {' '.join(push_e) or 'все'} · исключены: {' '.join(skip_e) or '—'}\n"
        f"Период лидов: <b>{period_label(ctx)}</b>\n"
        f"Подходит по статусу и периоду: <b>{aud['matched']}</b>\n"
        f"⏸ Не писать недавно активным: <b>{dur_label(db.get_int('push_idle_minutes'))}</b>"
        f"{' (пропущено ' + str(aud['cooled']) + ')' if aud['cooled'] else ''}\n"
        f"🔁 Не повторять тем же лидам: <b>{dur_label(db.get_int('push_repeat_days') * 1440)}</b>"
        f"{' (пропущено ' + str(aud['repeated']) + ')' if aud['repeated'] else ''}\n"
        f"Будет отправлено: <b>{len(aud['threads'])}</b> (количество: {limit if limit else 'все'})\n"
        f"Способ: <b>{mode_s}</b>\n"
        f"Пауза между сообщениями: 2–3 сек\n\n"
        f"<b>Текст:</b>\n{preview}"
    )
    if aud["unreachable"]:
        text += f"\n\nℹ️ Через бота недоступно: {aud['unreachable']} (не нажимали «Старт»)."
    rows = [
        [("🚀 Разослать сейчас", "b:new")],
        [("✏️ Текст", "p:text"), ("🔢 Количество", "p:limit")],
        [("📅 Период лидов", "p:period"), ("📨 Способ", "p:mode")],
        [("⏸ Пауза после сообщения", "p:idle"), ("🔁 Повтор Push", "p:repeat")],
        [("🏷 Эмодзи для Push", "p:emoji"), ("🚫 Исключённые эмодзи", "p:skip")],
        [("🧾 Журнал рассылок", "b:log")],
    ]
    if ctx.broadcaster.running:
        rows.insert(0, [("⏹ Остановить текущую рассылку", "b:stop")])
        text = "⏳ <b>Сейчас идёт рассылка</b>\n\n" + text
    rows.append([("⬅️ Назад", "m")])
    return text, kb(*rows)


def broadcast_confirm(ctx: Ctx, text: str):
    mode = ctx.db.get("push_mode")
    aud = ctx.broadcaster.audience(mode)
    n = len(aud["threads"])
    mode_s = "с вашего аккаунта" if mode == "userbot" else "через бота"
    warn = ""
    if mode == "userbot":
        warn = ("\n\n⚠️ <b>Отправка с личного аккаунта</b> может привести к ограничению аккаунта Telegram. "
                "Лучше небольшими порциями. Пауза 2–3 сек; при FloodWait/PeerFlood остановлюсь сам.")
    if aud["unreachable"]:
        warn += f"\n\nℹ️ {aud['unreachable']} лидов недоступны через бота — они не нажимали «Старт»."
    body = (
        "🚀 <b>Подтвердите Push</b>\n\n"
        f"Получателей: <b>{n}</b> из подходящих {aud['matched']} · способ: <b>{mode_s}</b>\n"
        f"Период лидов: {period_label(ctx)}\n"
        f"Пропущено: недавно писали — {aud['cooled']}, уже получали — {aud['repeated']}\n"
        f"Время: ≈ <b>{ctx.broadcaster.estimate_minutes(n)} мин</b>\n\n"
        f"<b>Текст:</b>\n{h(text[:1500])}{warn}"
    )
    rows = [
        [("✅ Начать", "b:go"), ("❌ Отмена", "m")],
        [("🔢 Количество", "b:limit"), ("✏️ Текст", "b:text")],
        [("📨 Способ", "b:mode"), ("📅 Период", "b:period")],
        [("⏸ Пауза после сообщения", "b:idle")],
    ]
    if n == 0:
        body += "\n\n⛔ Получателей нет: проверьте эмодзи, период и способ."
        rows = [[("🔢 Количество", "b:limit"), ("📅 Период", "b:period")],
                [("⏸ Пауза после сообщения", "b:idle")], [("⬅️ Назад", "p:menu")]]
    return body, kb(*rows)


def broadcast_log(ctx: Ctx):
    rows = ctx.db.last_broadcasts(8)
    if not rows:
        text = "🧾 <b>Журнал рассылок</b>\n\nПока пусто."
    else:
        names = {"running": "идёт", "done": "готово", "stopped": "остановлена",
                 "aborted": "прервана", "interrupted": "прервана перезапуском"}
        lines = [
            f"• {fmt_local(r['started_ts'], ctx.tz)} · {'бот' if r['mode'] == 'bot' else 'аккаунт'} · "
            f"{r['sent']}/{r['total']} (ошибок {r['failed']}) · {names.get(r['status'], r['status'])}"
            for r in rows
        ]
        text = "🧾 <b>Журнал рассылок</b>\n\n" + "\n".join(lines)
    return text, kb([("⬅️ Назад", "p:menu")])


# ------------------------------------------------------------------ лиды
PAGE = 15


def leads(ctx: Ctx, offset: int):
    since = int(time.time()) - 7 * 86400
    total = ctx.db.count_leads(since)
    offset = max(0, min(offset, max(0, total - 1)))
    rows_db = ctx.db.leads_since(since, PAGE, offset)
    c = lead_counts(ctx)
    ub = ctx.userbot
    state = ("⏳ идёт пересчёт истории…" if ub.backfilling else
             f"последний пересчёт: {ago(float(ctx.db.get('leads_backfill_ts') or 0))}")
    head = (
        f"📅 <b>Лиды</b>\n\nС {h(ctx.db.get('track_from'))}: <b>{c['total']}</b> · "
        f"🔥 {c['push']} · исключённых (⚡💊) {c['skip']} · без статуса {c['none']}\n"
        f"{state}\n\n<b>За последние 7 дней — {total}:</b>\n"
    )
    if not rows_db:
        text = head + "пока нет."
    else:
        lines = [
            f"{offset + i}. {fmt_local(r['last_seen'], ctx.tz)} · {h((r['label'] or '')[:26] or str(r['chat_id']))} · "
            f"<a href=\"{h(message_link(r['chat_id'], r['last_msg_id'], r['topic_id']))}\">сообщение</a>"
            for i, r in enumerate(rows_db, start=1)
        ]
        text = head + "\n".join(lines)
    nav = []
    if offset > 0:
        nav.append(("⬅️", f"l:{max(0, offset - PAGE)}"))
    if offset + PAGE < total:
        nav.append(("➡️", f"l:{offset + PAGE}"))
    rows = ([nav] if nav else []) + [[("🔄 Пересчитать лидов с даты начала", "l:recount")], [("⬅️ Назад", "m")]]
    return text, kb(*rows)


# ------------------------------------------------------------------ аккаунт
def account(ctx: Ctx):
    ub = ctx.userbot
    text = (
        "🔑 <b>Аккаунт юзербота</b>\n\n"
        f"Состояние: {STATE_LABELS.get(ub.state, ub.state)}\n"
        + (f"Аккаунт: {h(ub.me_label)}\n" if ub.me_label else "")
        + "\nЮзербот — это обычный аккаунт Telegram, который состоит в рабочих чатах. "
          "Он видит только метаданные сообщений (ID, время, отправитель)."
    )
    return text, kb(
        [("📲 Подключить / переподключить", "a:login")],
        [("🔄 Обновить папки и сотрудников", "a:sync")],
        [("🚪 Отключить аккаунт", "a:logout")],
        [("⬅️ Назад", "m")],
    )


HELP = (
    "📖 <b>Как это работает</b>\n\n"
    "1. «Аккаунт юзербота» → подключите аккаунт (номер → код → пароль, если есть).\n"
    "2. «Папки» → добавьте папки (название как в Telegram) и сотрудников (@username).\n"
    "3. «Группа Проёб» → укажите ID группы (найдите через «🔎 Найти чат / ID»). "
    "Бот должен быть в группе, а в @BotFather выключен Group Privacy.\n"
    "4. Готово: если менеджер не ответил лиду за лимит времени — в «Проёб» придёт список ссылок "
    "и отметка сотрудника.\n\n"
    "<b>Перерывы:</b> сотрудник пишет в «Проёб» <code>атайду</code> или <code>перерыв</code>, "
    "вернувшись — <code>тут</code>.\n"
    "<b>ID helper:</b> перешлите мне сообщение — верну ID, который Telegram позволяет узнать. "
    "ID обычной группы по пересылке не раскрывается — используйте «🔎 Найти чат / ID».\n"
    "<b>Рассылки:</b> клиентам, которые нажали «Старт» у бота — через бота; остальным — "
    "через аккаунт (риск ограничений).\n\n"
    "Команда /menu всегда открывает это меню."
)
