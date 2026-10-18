"""Экраны админ-панели (тексты и инлайн-кнопки). Пишутся по-русски; узбекский — через i18n.tr() при показе.

Каждый экран — функция, возвращающая (текст HTML, клавиатура). Telegram-объекты
импортируются лениво, чтобы модуль можно было проверять без установленного aiogram.
Пользовательские данные (названия, имена, тексты) — только через h(): так они экранируются
и помечаются, чтобы перевод их не трогал.
"""
from __future__ import annotations

import html
import time

from .context import Ctx
from .features import FEATURES, SPEED_NAMES, emoji_lists, feature_on, master_on
from .links import message_link
from .monitor import stavka_limits
from .statuses import classify
from .worktime import fmt_local



def h(value) -> str:
    """Экранировать данные для HTML и пометить их как данные (не переводить)."""
    from .i18n import mark
    return mark(html.escape(value))


def h_or(value, default: str) -> str:
    """h(value), а если значения нет — текст панели default (он переводится)."""
    return h(value) if value else default

STATE_LABELS = {
    "online": "🟢 подключён",
    "connecting": "🟡 подключается…",
    "starting": "🟡 запускается…",
    "offline": "🔴 нет связи, переподключаюсь",
    "unauthorized": "🔴 нужен повторный вход",
    "no_session": "⚪ аккаунт не подключён",
    "disabled": "⚪ не заданы API_ID / API_HASH",
    "paused": "⏸ на паузе",
}
_LANG = {"get": lambda: "ru"}


def set_lang_source(fn) -> None:
    _LANG["get"] = fn


def T(text: str) -> str:
    from .i18n import tr
    return tr(text, _LANG["get"]())


def kb(*rows):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=T(t), callback_data=d) for t, d in row] for row in rows]
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


def main(ctx: Ctx, rank: int = 4):
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
    lang_btn = "🌐 Язык: Русский" if db.get("lang") != "uz" else "🌐 Язык: Узбекский"
    if rank >= 3:
        rows = [
            [("📣 Push (горячие лиды)", "p:menu"), ("🚨 Проёб (задержки)", "pr:menu")],
            [("📁 Папки и сотрудники", "f:list"), ("📲 Аккаунты", "acc:menu")],
            [("📅 Лиды", "l:0"), ("📈 Отчёты и рейтинг", "rep:menu")],
            [("🎛 Функции и выключатели", "fn"), ("📊 Статус", "st")],
            [(lang_btn, "lang"), ("📖 Помощь", "help")],
        ]
        if rank >= 4:
            rows.append([("👮 Админы и права", "adm:menu")])
    else:
        rows = [[("📊 Статус", "st"), ("📅 Лиды", "l:0")],
                [("📈 Отчёты и рейтинг", "rep:menu"), ("🎛 Функции", "fn")],
                [("🚨 Проёб (задержки)", "pr:menu"), (lang_btn, "lang")],
                [("📖 Помощь", "help")]]
        if rank >= 2:
            rows.insert(1, [("📣 Push (горячие лиды)", "p:menu")])
    return text, kb(*rows)


def functions(ctx: Ctx, rank: int = 4):
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
    if rank < 3:
        rows = []
    else:
        rows += [[("♻️ Сбросить настройки к заводским", "fn:reset")]]
    rows += [[("⬅️ Назад", "m")]]
    text = "🎛 <b>Функции и выключатели</b>\n\n" + "\n".join(lines) + note
    return text, kb(*rows)


# ------------------------------------------------------------------ папки
def folders(ctx: Ctx):
    rows = []
    per = ctx.db.leads_per_folder(ctx.monitor.track_from_ts())
    for f in ctx.db.folders():
        n_e = len(ctx.db.employees(f["id"]))
        n_c = len(ctx.db.chats(f["id"]))
        flags = ("" if f["delay_on"] else " ⏱⛔") + ("" if f["push_on"] else " 📣⛔")
        rows.append([(f"📁 {f['name']} · 👤{n_e} · 💬{n_c} · 🔥{per.get(f['id'], 0)}{flags}", f"f:o:{f['id']}")])
    rows += [
        [("➕ Добавить папку", "f:add")],
        [("🔄 Подтянуть чаты из Telegram", "f:sync")],
        [("⬅️ Назад", "m")],
    ]
    text = (
        "📁 <b>Папки и сотрудники</b>\n\n"
        "Название папки здесь должно совпадать с названием папки в Telegram "
        "(у основного аккаунта) — чаты подтянутся автоматически.\n"
        "👤 сотрудники · 💬 чаты · 🔥 лиды с даты начала.\n"
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
    acc = ctx.db.account(f["account_id"]) if f["account_id"] else None
    acc_s = (f"{h(acc['name'])} — {STATE_LABELS.get(ctx.pool.state_of(acc['id']), '') if ctx.pool else ''}"
             if acc else "не назначен ⚠️")
    quiet = f"{f['quiet_from']}–{f['quiet_to']}" if f["quiet_from"] and f["quiet_to"] else "общее"
    per = ctx.db.leads_per_folder(ctx.monitor.track_from_ts()).get(fid, 0)
    text = (
        f"📁 <b>{h(f['name'])}</b> · 💬 чатов {len(chats)} (вручную {manual}) · 🔥 лидов {per}\n\n"
        "<b>🚨 Проёб (задержки)</b>\n"
        f"👤 Кого отмечать: {emp_line}\n"
        f"⏱ Контроль: {onoff(bool(f['delay_on']))} · 💰 штраф: <b>{f['fine'] if f['fine'] else 'не задан'}</b>\n\n"
        "<b>📣 Push</b>\n"
        f"📲 Аккаунт Push: <b>{acc_s}</b>\n"
        f"📣 Push: {onoff(bool(f['push_on']))} · 🔢 сколько: <b>{f['push_limit'] or 'общее число'}</b>\n"
        f"🌙 Не слать Push в: <b>{quiet}</b>\n\n"
        "<i>✅ — ID сотрудника определён, ⏳ — определится сам. Аккаунт Push должен состоять в чатах этой папки.</i>"
    )
    rows = [
        [("👤➕ Добавить сотрудника", f"e:add:{fid}")],
        *[[(f"❌ @{e['username']}", f"e:del:{e['id']}")] for e in emps[:10]],
        [(("⏱⛔ Выкл. контроль" if f["delay_on"] else "⏱ Вкл. контроль"), f"f:dly:{fid}"),
         ("💰 Штраф", f"f:fine:{fid}")],
        [("📲 Выбрать аккаунт Push", f"f:acc:{fid}")],
        *([[("🚫 Убрать аккаунт", f"f:accset:{fid}:0")]] if acc else []),
        [(("📣⛔ Выкл. Push" if f["push_on"] else "📣 Вкл. Push"), f"f:psh:{fid}"),
         ("🔢 Сколько — только этой папке", f"f:plim:{fid}")],
        [("🌙 Тихое время", f"f:quiet:{fid}"), ("💬 Чаты папки", f"c:l:{fid}")],
        [("✏️ Переименовать", f"f:ren:{fid}"), ("🗑 Удалить", f"f:del:{fid}")],
        [("⬅️ К папкам", "f:list")],
    ]
    return text, kb(*rows)


def folder_acc_pick(ctx: Ctx, fid: int):
    f = ctx.db.folder(fid)
    if f is None:
        return folders(ctx)
    rows = []
    lines = []
    for a in ctx.db.accounts():
        st = STATE_LABELS.get(ctx.pool.state_of(a["id"]), "—") if ctx.pool else "—"
        used = [x["name"] for x in ctx.db.folders() if x["account_id"] == a["id"] and x["id"] != fid]
        mark = "✅ " if f["account_id"] == a["id"] else ""
        lines.append(f"{mark}<b>{h(a['name'])}</b> {h(a['label'])} — {st}"
                     + (f"\n   уже у папок: {h(', '.join(used))}" if used else ""))
        rows.append([(f"{mark}{a['name']}", f"f:accset:{fid}:{a['id']}")])
    if f["account_id"]:
        rows.append([("🚫 Убрать аккаунт", f"f:accset:{fid}:0")])
    rows += [[("➕ Добавить новый аккаунт", "acc:add")], [("⬅️ Назад", f"f:o:{fid}")]]
    text = (f"📲 <b>Аккаунт Push для папки {h(f['name'])}</b>\n\nВыберите из подключённых. Один аккаунт можно "
            "дать нескольким папкам — тогда он шлёт им по очереди.\n\n"
            + ("\n".join(lines) if lines else "<i>Подключённых аккаунтов пока нет — нажмите «Добавить новый аккаунт».</i>"))
    return text, kb(*rows)


def chats(ctx: Ctx, fid: int):
    f = ctx.db.folder(fid)
    if f is None:
        return folders(ctx)
    rows_db = ctx.db.chats(fid)
    lines = [f"• {h_or(c['title'], 'без названия')} — <code>{c['chat_id']}</code>" for c in rows_db[:25]]
    more = f"\n… и ещё {len(rows_db) - 25}" if len(rows_db) > 25 else ""
    text = (
        f"💬 <b>Чаты папки {h(f['name'])}</b> ({len(rows_db)})\n\n"
        + ("\n".join(lines) if lines else "<i>Чатов нет. Проверьте, что в Telegram есть папка с таким же названием.</i>")
        + more
    )
    rows = [[(f"❌ {(c['title'] or str(c['chat_id']))[:24]}", f"c:del:{fid}:{c['chat_id']}")] for c in rows_db[:8]]
    rows += [[("➕ Добавить чат по ID", f"c:add:{fid}"), ("🔎 Найти чат / ID", "x:find")], [("⬅️ Назад к папке", f"f:o:{fid}")]]
    return text, kb(*rows)


# ------------------------------------------------------------------ настройки
def more_settings(ctx: Ctx):
    db = ctx.db
    need, remind, cap = stavka_limits(db)
    timer = "от ПЕРВОГО неотвеченного" if db.get("timer_from") != "last" else "от ПОСЛЕДНЕГО сообщения"
    text = (
        "⚙️ <b>Настройки контроля</b> (Проёб)\n\n"
        f"📆 Отслеживание с даты: <b>{h(db.get('track_from'))}</b>\n"
        f"⏳ Отсчёт {db.get('limit_minutes')} минут: <b>{timer}</b>\n"
        f"☕ Перерыв: <b>{db.get('break_minutes')} мин</b>, до <b>{db.get('breaks_per_day')}</b> раз в день\n"
        f"🎬 Ставка нового лида: {onoff(feature_on(db, 'botwatch_enabled'))} — конец после <b>{need}</b> кружков, "
        f"«ставка» через <b>{remind // 60} мин</b>, предел <b>{cap // 60} мин</b>\n"
        f"🤖 Бот-посредник: <b>{h_or(db.get('midbot_name'), 'не задан')}</b>\n"
        f"🆕 Новый лид без контакта: {onoff(feature_on(db, 'newlead_enabled'))}, напомнить через <b>{db.get('newlead_minutes')} мин</b>"
    )
    return text, kb(
        [("📆 Дата начала отслеживания", "s:track")],
        [("⏳ Переключить способ отсчёта", "s:timer")],
        [("☕ Перерывы (минуты и раз в день)", "s:brk")],
        [("🎬 Ставка: кружки и сроки", "s:bw")],
        [("🤖 Бот-посредник («CLIENT …»)", "s:midbot")],
        [("💳 Напоминание об оплате", "pay:menu")],
        [("🆕 Новый лид без контакта", "s:nl")],
        [("⬅️ Назад", "pr:menu")],
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
        f"{stavka_line(db, now)}"
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


def stavka_line(db, now: int) -> str:
    if not feature_on(db, "botwatch_enabled"):
        return ""
    need, _, cap = stavka_limits(db)
    return f"🎬 В ставке сейчас (ждём {need} кружка): {db.stavka_running_count(need, now - cap)}\n"


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
    return f"{h(frm)} — {h_or(ctx.db.get('push_to'), 'сегодня')}"


# ------------------------------------------------------------------ Push
def plan_lines(ctx: Ctx, aud: dict) -> str:
    """Что произойдёт при старте: папка → аккаунт → сколько человек (и почему 0)."""
    sel = ctx.broadcaster.selected_folders()
    mode = ctx.db.get("push_mode")
    lines = []
    for f in ctx.db.folders():
        if not f["push_on"] or (sel and f["id"] not in sel):
            continue
        n = aud["by_folder"].get(f["id"], 0)
        acc = ctx.db.account(f["account_id"]) if f["account_id"] else None
        if mode == "userbot":
            who = h(acc["name"]) if acc else "—"
            st = ctx.pool.state_of(acc["id"]) if (acc and ctx.pool) else "no_session"
            note = ""
            if not acc:
                note = " ⚠️ нет аккаунта"
            elif st == "paused":
                note = " ⏸ аккаунт на паузе"
            elif st != "online":
                note = " 🔴 аккаунт не в сети"
            if f["id"] in aud.get("quiet", []):
                note = " 🌙 тихое время"
            lines.append(f"📁 <b>{h(f['name'])}</b> → 📲 {who} → <b>{n}</b>"
                         + (f" (лимит {f['push_limit']})" if f["push_limit"] else "") + note)
        else:
            lines.append(f"📁 <b>{h(f['name'])}</b> → бот → <b>{n}</b>")
    return "\n".join(lines) if lines else "<i>Нет папок для Push — включите их в «Папки и аккаунты Push».</i>"


def push_menu(ctx: Ctx):
    db = ctx.db
    mode = db.get("push_mode")
    aud = ctx.broadcaster.audience(mode)
    preview = db.get("push_text").strip()
    preview = (h(preview[:160]) + ("…" if len(preview) > 160 else "")) if preview else "<i>не задан</i>"
    limit = aud["limit"]
    text = (
        "📣 <b>Push — горячие лиды</b>\n\n"
        "Нажмёте «🚀 Разослать» — Push уйдёт во ВСЕ папки сразу, каждая со своего аккаунта:\n\n"
        f"{plan_lines(ctx, aud)}\n\n"
        f"Всего: <b>{len(aud['threads'])}</b> · число на КАЖДУЮ папку (без своего): "
        f"<b>{limit if limit else 'все'}</b> на каждую папку\n"
        f"📎 Вложение: <b>{media_label(ctx)}</b>\n"
        f"<b>Текст:</b> {preview}"
    )
    if limit and len(aud["threads"]) < limit * max(1, len(aud["by_folder"])) and aud["matched"] > len(aud["threads"]):
        sk = "; ".join(f"{k} {v}" for k, v in (("новые лиды", aud["fresh"]), ("недавно писали", aud["cooled"]),
                                                ("уже получали", aud["repeated"]),
                                                ("не вечерний контакт", aud["slot_skipped"])) if v)
        if sk:
            text += f"\n\nℹ️ Отсеяно фильтрами: {sk}."
    if aud.get("no_account"):
        text += "\n\n⚠️ Папкам без аккаунта Push слать не буду. Назначьте: «📁 Папки и аккаунты Push»."
    if aud["unreachable"]:
        text += f"\n\nℹ️ Через бота недоступно: {aud['unreachable']} (не нажимали «Старт»)."
    rows = [
        [("🚀 Разослать сейчас", "b:new")],
        [("📁 Папки и аккаунты Push", "p:plan")],
        [("✏️ Текст", "p:text"), ("📎 Медиа", "p:media")],
        [("🧪 Тест: только мне", "p:test"), ("📋 Шаблоны", "tpl:menu")],
        [("🔢 Сколько на папку (общее)", "p:limit"), ("⚙️ Настройки Push", "p:set")],
        [("🧾 Журнал рассылок", "b:log")],
    ]
    if ctx.broadcaster.media():
        rows.insert(3, [("🗑 Убрать медиа", "p:nomedia")])
    if ctx.broadcaster.running:
        rows.insert(0, [("⏹ Остановить текущую рассылку", "b:stop")])
        text = "⏳ <b>Сейчас идёт рассылка</b>\n\n" + text
    rows.append([("⬅️ Назад", "m")])
    return text, kb(*rows)


def push_plan(ctx: Ctx):
    """Все папки: аккаунт и счётчик Push — по кнопке открывается карточка папки."""
    aud = ctx.broadcaster.audience(ctx.db.get("push_mode"))
    rows = []
    for f in ctx.db.folders():
        acc = ctx.db.account(f["account_id"]) if f["account_id"] else None
        on = "✅" if f["push_on"] else "⛔"
        rows.append([(f"{on} {f['name']} → {acc['name'] if acc else 'нет аккаунта'}", f"f:o:{f['id']}")])
    rows += [[("🗂 Push по папкам (вкл/выкл)", "p:folders")], [("⬅️ Назад", "p:menu")]]
    text = ("📁 <b>Папки и аккаунты Push</b>\n\n"
            "Нажмите на папку, чтобы выбрать ей аккаунт, число Push и тихое время.\n\n" + plan_lines(ctx, aud))
    return text, kb(*rows)


def push_settings(ctx: Ctx):
    db = ctx.db
    mode = db.get("push_mode")
    aud = ctx.broadcaster.audience(mode)
    push_e, skip_e = emoji_lists(db)
    quiet = (f"{db.get('push_quiet_from')}–{db.get('push_quiet_to')}"
             if db.get("push_quiet_from") and db.get("push_quiet_to") else "нет")
    text = (
        "⚙️ <b>Настройки Push</b> (общие)\n\n"
        f"Получают статусы: {' '.join(push_e) or 'все'} · исключены: {' '.join(skip_e) or '—'}\n"
        f"Период лидов: <b>{period_label(ctx)}</b>\n"
        f"Режим: <b>{SLOT_NAMES.get(db.get('push_slot'), SLOT_NAMES['all'])}</b>\n"
        f"⏸ Не писать недавно активным: <b>{dur_label(db.get_int('push_idle_minutes'))}</b>\n"
        f"🔁 Не повторять тем же: <b>{dur_label(db.get_int('push_repeat_minutes'))}</b>\n"
        f"🆕 Новый лид — пауза: <b>{dur_label(db.get_int('push_newlead_minutes'))}</b>\n"
        f"🌙 Общее тихое время: <b>{quiet}</b> (у папки может быть своё)\n"
        f"Способ: <b>{'с аккаунтов Push' if mode == 'userbot' else 'через бота'}</b> · "
        f"скорость: <b>{SPEED_NAMES.get(db.get('push_speed'), SPEED_NAMES['normal'])}</b>\n"
        f"Подходит по статусу/периоду/папкам: {aud['matched']}"
    )
    return text, kb(
        [("📅 Период лидов", "p:period"), ("🕒 Режим: все / день / вечер", "p:slot")],
        [("⏸ Пауза после сообщения", "p:idle"), ("🔁 Повтор Push", "p:repeat")],
        [("🆕 Новые лиды: пауза", "p:newlead"), ("🌙 Общее тихое время", "p:quiet")],
        [("📨 Способ", "p:mode"), ("⚡ Скорость", "p:speed")],
        [("🏷 Эмодзи для Push", "p:emoji"), ("🚫 Исключённые эмодзи", "p:skip")],
        [("⬅️ Назад", "p:menu")],
    )


SLOT_NAMES = {"all": "все лиды", "day": "день (сначала контактные сегодня)", "evening": "вечер (только контакт сегодня)"}


def folders_label(ctx: Ctx) -> str:
    sel = ctx.broadcaster.selected_folders()
    names = [f["name"] for f in ctx.db.folders() if f["push_on"] and (not sel or f["id"] in sel)]
    return h(", ".join(names)) if names else "нет"


def by_folder_line(ctx: Ctx, aud: dict) -> str:
    if not aud["by_folder"]:
        return ""
    parts = []
    for fid, n in aud["by_folder"].items():
        f = ctx.db.folder(fid)
        parts.append(f"{h(f['name']) if f else fid}: {n}")
    return "По папкам: " + ", ".join(parts) + "\n"


def offline_accounts(ctx: Ctx, aud: dict) -> list[str]:
    if ctx.db.get("push_mode") != "userbot" or not ctx.pool:
        return []
    out = []
    for fid in aud["by_folder"]:
        f = ctx.db.folder(fid)
        if f and f["account_id"] and ctx.pool.state_of(f["account_id"]) != "online":
            a = ctx.db.account(f["account_id"])
            out.append(a["name"] if a else str(f["account_id"]))
    return sorted(set(out))


def media_label(ctx: Ctx) -> str:
    m = ctx.broadcaster.media()
    if not m:
        return "нет"
    names = {"voice": "🎤 голосовое", "video": "🎬 видео", "video_note": "⭕ кружок", "photo": "🖼 фото",
             "document": "📄 файл", "audio": "🎵 аудио", "animation": "🎞 GIF"}
    return names.get(m["kind"], m["kind"])


def broadcast_confirm(ctx: Ctx, text: str):
    mode = ctx.db.get("push_mode")
    aud = ctx.broadcaster.audience(mode)
    n = len(aud["threads"])
    mode_s = "с вашего аккаунта" if mode == "userbot" else "через бота"
    warn = ""
    if mode == "userbot":
        warn = ("\n\n⚠️ <b>Отправка с личного аккаунта</b> может привести к ограничению аккаунта Telegram. "
                "Лучше небольшими порциями. Пауза 2–3 сек; при FloodWait/PeerFlood остановлюсь сам.")
    if aud.get("no_account"):
        warn += "\n\n⚠️ Без аккаунта Push (не отправлю): " + ", ".join(h(ctx.db.folder(f)["name"]) for f in aud["no_account"])
    off = offline_accounts(ctx, aud)
    if off:
        warn += "\n\n🔴 Аккаунты не в сети: " + ", ".join(h(x) for x in off) + " — их лидам Push не уйдёт."
    if aud["limit"] and n < aud["limit"]:
        warn += f"\n\n⚠️ Просили {aud['limit']}, подходит только {n} (остальные отсеяны фильтрами)."
    if aud["unreachable"]:
        warn += f"\n\nℹ️ {aud['unreachable']} лидов недоступны через бота — они не нажимали «Старт»."
    body = (
        "🚀 <b>Подтвердите Push</b>\n\n"
        f"Получателей: <b>{n}</b> из подходящих {aud['matched']} · способ: <b>{mode_s}</b>\n"
        f"Период лидов: {period_label(ctx)}\n"
        f"Пропущено: совсем новые — {aud['fresh']}, недавно писали — {aud['cooled']}, уже получали — {aud['repeated']}\n"
        f"\n{plan_lines(ctx, aud)}\n\n"
        f"📎 Вложение: {media_label(ctx)}\n"
        f"Время: ≈ <b>{ctx.broadcaster.estimate_minutes(n)} мин</b>\n\n"
        f"<b>Текст:</b>\n{h(text[:1500]) if text else '—'}{warn}"
    )
    rows = [
        [("✅ Начать", "b:go"), ("❌ Отмена", "m")],
        [("🔢 Сколько на папку (общее)", "b:limit"), ("✏️ Текст", "b:text")],
        [("📨 Способ", "b:mode"), ("📅 Период", "b:period")],
        [("⏸ Пауза после сообщения", "b:idle")],
    ]
    if n == 0:
        body += "\n\n⛔ Получателей нет: проверьте эмодзи, период и способ."
        rows = [[("🔢 Сколько на папку (общее)", "b:limit"), ("📅 Период", "b:period")],
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
        f"{state}\n"
    )
    per = ctx.db.leads_per_folder(ctx.monitor.track_from_ts())
    if per:
        names = {f["id"]: f["name"] for f in ctx.db.folders()}
        head += "\n<b>По папкам:</b> " + " · ".join(
            f"{h(names.get(fid, str(fid)))} {n}" for fid, n in sorted(per.items(), key=lambda kv: -kv[1])) + "\n"
    head += f"\n<b>За последние 7 дней — {total}:</b>\n"
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
    rows = ([nav] if nav else []) + [[("📁 Лиды по папкам (сегодня/вчера/7 дн./всего)", "rep:leads")],
                                     [("🔄 Пересчитать лидов с даты начала", "l:recount")], [("⬅️ Назад", "m")]]
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
        [("⬅️ Назад", "acc:menu")],
    )


HELP = (
    "📖 <b>Как устроено меню</b>\n\n"
    "📣 <b>Push</b> — рассылка горячим лидам (текст, медиа, количество, период, тихое время).\n"
    "🚨 <b>Проёб</b> — контроль задержек: группа, лимит, рабочий день, перерывы, ставка, оплата; "
    "«🔍 Проверить лид» — что бот думает о конкретном лиде.\n"
    "🎬 <b>Ставка нового лида</b> — новый лид (новый топик) → ставка; пока не пришли 3 кружка, ничьи сообщения "
    "не считаются задержкой. Кружков нет к сроку — «ставка» в «Проёб». После 3 кружков — обычный контроль задержек.\n"
    "📁 <b>Папки и сотрудники</b> — папки, кого отмечать, штраф, чаты, аккаунт Push и число Push для папки.\n"
    "📲 <b>Аккаунты</b> — основной (следит за чатами; сменить) и аккаунты Push (пауза, имя, удаление), "
    "а также кто пишет в «Проёб».\n"
    "📅 <b>Лиды</b> и 📈 <b>Отчёты</b> — сколько лидов по папкам, кто как ответил, штрафы, итоги.\n"
    "🎛 <b>Функции</b> — выключатели каждой функции. 📊 <b>Статус</b> — что сейчас работает.\n\n"
    "<b>Первая настройка:</b> 1) Аккаунты → Основной аккаунт → подключить; 2) Папки → добавить папку (название как в Telegram) "
    "и сотрудников; 3) Проёб → Группа «Проёб» (или напишите /proyob в самой группе; бот — админ группы).\n\n"
    "<b>Перерывы:</b> сотрудник пишет в «Проёб» <code>атайду</code> или <code>перерыв</code>, "
    "вернувшись — <code>тут</code>.\n"
    "<b>ID helper:</b> «🔎 Найти чат / ID» — по названию покажет ID чата.\n\n"
    "Команда /menu всегда открывает это меню."
)


# ------------------------------------------------------------------ шаблоны Push
def templates(ctx: Ctx):
    rows_db = ctx.db.templates()
    text = ("📋 <b>Шаблоны Push</b>\n\nСохраните текущий текст и вложение, чтобы потом применять одной кнопкой.\n\n"
            + ("\n".join(f"• {h(t['name'])}{' 📎' if t['media'] else ''}" for t in rows_db) or "<i>Шаблонов пока нет.</i>"))
    rows = [[(f"▶️ {t['name'][:22]}", f"tpl:use:{t['id']}"), ("🗑", f"tpl:del:{t['id']}")] for t in rows_db[:10]]
    rows += [[("💾 Сохранить текущий как шаблон", "tpl:add")], [("⬅️ Назад", "p:menu")]]
    return text, kb(*rows)


# ------------------------------------------------------------------ отчёты
def reports_menu(ctx: Ctx, rank: int = 4):
    db = ctx.db
    text = ("📈 <b>Отчёты и рейтинг</b>\n\n"
            f"Ежедневный отчёт: {onoff(feature_on(db, 'report_enabled'))} · в <b>{h(db.get('report_time'))}</b>\n"
            "Сотрудники: сколько ответили клиентам, среднее/максимальное ожидание, сколько ответов позже лимита (рабочие часы, без перерывов).")
    rows = [[("📊 Сегодня", "rep:day"), ("📁 Лиды по папкам", "rep:leads")],
            [("👥 Сотрудники: сегодня", "rep:r1")],
            [("👥 7 дней", "rep:r7"), ("👥 30 дней", "rep:r30")],
            [("💰 Штрафы: 7 дней", "rep:f7"), ("💰 Штрафы: 30 дней", "rep:f30")],
            [("📅 Итог недели", "rep:week")]]
    if rank >= 3:
        rows += [[("📨 Прислать отчёт сейчас", "rep:now"), ("🕙 Время отчёта", "rep:time")],
                 [("💾 Резервная копия", "rep:backup")]]
    rows.append([("⬅️ Назад", "m")])
    return text, kb(*rows)


# ------------------------------------------------------------------ админы
ROLE_NAMES = {"view": "👁 только смотрит", "push": "📣 смотрит + Push", "full": "🛠 полное управление"}


def admins_screen(ctx: Ctx):
    rows_db = ctx.db.admins()
    owners = ", ".join(f"<code>{i}</code>" for i in ctx.cfg.admin_ids)
    text = (f"👮 <b>Админы и права</b>\n\nВладельцы (из Railway, полный доступ): {owners}\n\n"
            + ("\n".join(f"• <code>{a['user_id']}</code> — {ROLE_NAMES.get(a['role'], a['role'])}" for a in rows_db)
               or "<i>Других админов нет.</i>")
            + "\n\nУровни: 👁 смотрит → 📣 +Push → 🛠 всё. Админ должен один раз нажать «Старт» у бота.")
    rows = [[(f"{a['user_id']}: {ROLE_NAMES.get(a['role'], a['role'])}", f"adm:role:{a['user_id']}"),
             ("🗑", f"adm:del:{a['user_id']}")] for a in rows_db[:15]]
    rows += [[("➕ Добавить админа по ID", "adm:add")], [("⬅️ Назад", "m")]]
    return text, kb(*rows)


REPLY_ROWS = (("📊 Статус", "📣 Push"), ("🎛 Функции", "📅 Лиды"), ("📈 Отчёты", "🏠 Меню"))
BOT_COMMANDS = (("menu", "🏠 Главное меню"), ("status", "📊 Статус"), ("push", "📣 Push"),
                ("report", "📈 Отчёты"), ("functions", "🎛 Функции"))


def reply_kb():
    """Нижние кнопки (на языке панели; панель понимает их на обоих языках)."""
    from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=T(t)) for t in row] for row in REPLY_ROWS],
        resize_keyboard=True, is_persistent=True)


def bot_commands():
    """Команды в меню «/» (на языке панели)."""
    from aiogram.types import BotCommand

    return [BotCommand(command=c, description=T(d)) for c, d in BOT_COMMANDS]


def push_folders(ctx: Ctx):
    """Включить/выключить Push по папкам — тот же выключатель, что в карточке папки."""
    rows = []
    for f in ctx.db.folders():
        rows.append([(("✅ " if f["push_on"] else "⬜ ") + f["name"] + ("" if f["push_on"] else " (Push выкл)"), f"p:fold:{f['id']}")])
    rows += [[("✅ Включить Push всем папкам", "p:fold:all")], [("⬅️ Назад", "p:plan")]]
    text = ("🗂 <b>Push по папкам</b>\n\nОтметьте папки, которым слать Push. Аккаунт Push и число для каждой папки — "
            "в карточке папки («📁 Папки и сотрудники»). Число Push задаётся на каждую папку (150 = по 150 в каждой).")
    return text, kb(*rows)


def proyob_screen(ctx: Ctx):
    """Только контроль задержек. Папки/сотрудники — в «📁 Папки», аккаунты и «кто пишет» — в «📲 Аккаунты»."""
    db = ctx.db
    group = db.get("proyob_group_id") or "не задана ⚠️"
    lines = []
    for f in db.folders():
        emps = " ".join("@" + h(e["username"]) for e in db.employees(f["id"])) or "никто ⚠️"
        lines.append(f"📁 <b>{h(f['name'])}</b>: {emps} · {'⏱' if f['delay_on'] else '⛔'}"
                     + (f" · 💰{f['fine']}" if f["fine"] else ""))
    text = (
        "🚨 <b>Проёб — контроль задержек</b>\n\n"
        f"Группа «Проёб»: <code>{h(group)}</code> · пишет в неё: <b>{via_label(ctx)}</b> "
        "<i>(меняется в «📲 Аккаунты»)</i>\n"
        f"Контроль: {onoff(feature_on(db, 'delay_enabled'))} · лимит <b>{db.get('limit_minutes')} мин</b> · "
        f"рабочий день <b>{db.get('work_start')}–{db.get('work_end')}</b>\n"
        f"🎬 Ставка нового лида: {onoff(feature_on(db, 'botwatch_enabled'))} — пока не пришли "
        f"{stavka_limits(db)[0]} кружка, сообщения лида не считаются задержкой\n\n"
        "<b>Кого отмечать в каждой папке:</b>\n" + ("\n".join(lines) if lines else "<i>папок нет</i>")
        + "\n\n<i>Папки, сотрудники, штрафы и выключатель контроля по папке — в «📁 Папки и сотрудники».</i>"
    )
    rows = [
        [(("⛔ Выключить контроль" if feature_on(db, "delay_enabled") else "✅ Включить контроль"), "fn:t:delay_enabled")],
        [("👥 Группа «Проёб»", "s:group"), ("🔎 Найти чат / ID", "x:find")],
        [("⏱ Лимит ответа", "s:limit"), ("🕒 Рабочий день", "s:hours")],
        [("🔍 Проверить лид по ссылке", "l:chk")],
        [("🧾 Журнал решений за сутки (файл)", "rep:journal")],
        [("⚙️ Настройки контроля (ставка, перерывы, оплата…)", "s:more")],
        [("⬅️ Назад", "m")],
    ]
    return text, kb(*rows)


def lead_check(ctx: Ctx, chat_id: int, topic_id: int, msg_id: int):
    """Что бот знает о лиде по ссылке на его сообщение (только состояние и время, без текста)."""
    db = ctx.db
    back = kb([("🔍 Проверить другой лид", "l:chk")], [("⬅️ Назад", "pr:menu")])
    chat = db.chat(chat_id)
    if chat is None:
        return "🔍 Этот чат не под контролем: его нет в папках бота.", back
    if topic_id == 0 and chat_id in db.forum_ids():
        if any(t["topic_id"] == msg_id for t in db.topics(chat_id)):
            topic_id = msg_id                                   # ссылка на сам топик
        else:
            return "🔍 Это группа с топиками: пришлите ссылку на сообщение внутри топика лида.", back
    tz, now = ctx.tz, int(time.time())

    def t(ts) -> str:
        return fmt_local(ts, tz, "%d.%m %H:%M") if ts else "—"
    folder = db.folder(chat["folder_id"])
    topic = next((x for x in db.topics(chat_id) if x["topic_id"] == topic_id), None) if topic_id else None
    label = (f"{topic['icon']} {topic['title']}".strip() if topic else "") or chat["title"] or str(chat_id)
    lead, st = db.lead_row(chat_id, topic_id), db.state(chat_id, topic_id)
    need, remind, cap = stavka_limits(db)
    if topic_id <= 1:
        stavka = "только для групп с топиками"
    elif not feature_on(db, "botwatch_enabled"):
        stavka = "контроль ставки выключен (🎛 Функции)"
    elif lead is None or lead["stavka_from"] is None:
        stavka = "не отслеживалась — лид появился раньше или не распознан как новый"
    else:
        start, n = int(lead["stavka_from"]), int(lead["bet_count"])
        if n >= need or lead["stavka_end"] is not None:
            end = f" в {t(lead['stavka_end'])}" if lead["stavka_end"] is not None else ""
            stavka = f"✅ завершена{end} — кружков {n} из {need} (началась {t(start)})"
        elif now >= start + cap:
            stavka = f"закончилась по пределу {cap // 60} мин — кружков {n} из {need} (началась {t(start)})"
        else:
            stavka = (f"⏳ идёт — кружков {n} из {need}, началась {t(start)}; если кружков не будет — «ставка» "
                      f"в {t(start + remind)}; предел до {t(start + cap)}")
        if lead["watch_warned"] and n < need:
            stavka += " · напоминание «ставка» было"
    if st is None or st["status"] == "idle":
        delay = "ответа никто не ждёт" + (f" (последний ответ {t(st['last_reply_ts'])})" if st is not None and st["last_reply_ts"] else "")
    elif st["status"] == "pending":
        waited = ctx.monitor.elapsed(chat["folder_id"], int(st["pending_since"]), now) // 60
        delay = (f"⏳ клиент ждёт ответа с {t(st['pending_since'])} — {waited} мин рабочего времени "
                 f"из {db.get('limit_minutes')}")
    else:
        delay = f"🚨 уже в «Проёб» (клиент ждёт с {t(st['pending_since'])})"
    push_e, skip_e = emoji_lists(db)
    lines = [f"🔍 <b>{h(label)}</b> · папка <b>{h(folder['name'] if folder else '?')}</b>", "",
             f"🎬 Ставка: {stavka}", f"⏱ Задержка: {delay}"]
    if lead is not None:
        lines.append(f"📨 От клиента: сообщений {lead['msg_count']}, кружков {lead['bet_count']}; "
                     f"последнее {t(lead['last_seen'])}")
    if topic_id and classify(label, push_e, skip_e) == "skip":
        lines.append("⚡/💊 — такой лид в задержках не учитывается")
    if folder is not None and not folder["delay_on"]:
        lines.append("⛔ В этой папке контроль задержек выключен")
    if not feature_on(db, "delay_enabled"):
        lines.append("⛔ Контроль задержек выключен (🎛 Функции)")
    from .services import journal_lines
    events = journal_lines(db, tz, db.journal_thread(chat_id, topic_id, 15), name=h)
    if events:
        lines += ["", "<b>Что бот видел и решил</b> (последние события):"] + events
    return "\n".join(lines), back


def via_label(ctx: Ctx) -> str:
    via = ctx.db.get("alert_via") or "bot"
    if via == "main":
        return "🔑 основной аккаунт"
    if via.startswith("acc:"):
        a = ctx.db.account(int(via[4:])) if via[4:].isdigit() else None
        return f"📲 {h(a['name'])}" if a else "🤖 бот"
    return "🤖 бот"


def via_pick(ctx: Ctx):
    via = ctx.db.get("alert_via") or "bot"
    def mark(v): return "✅ " if via == v else ""
    rows = [[(f"{mark('bot')}🤖 Бот", "pr:viaset:bot")],
            [(f"{mark('main')}🔑 Основной аккаунт", "pr:viaset:main")]]
    for a in ctx.db.accounts():
        rows.append([(f"{mark('acc:' + str(a['id']))}📲 {a['name']}", f"pr:viaset:acc{a['id']}")])
    rows.append([("⬅️ Назад", "acc:menu")])
    text = ("📤 <b>Кто пишет в «Проёб»</b>\n\nСейчас: <b>" + via_label(ctx) + "</b>\n\n"
            "Выберите, от чьего имени приходят уведомления о задержках и напоминания. Аккаунт должен состоять в группе «Проёб». "
            "Если аккаунт не в сети или не в группе — напишет бот.\n\n"
            "<i>У аккаунта нет кнопки «✅ Выполнено»: уведомление закрывается само, когда менеджер ответил.</i>")
    return text, kb(*rows)


def accounts_screen(ctx: Ctx):
    rows_db = ctx.db.accounts()
    folders_by_acc: dict[int, list[str]] = {}
    for f in ctx.db.folders():
        if f["account_id"]:
            folders_by_acc.setdefault(f["account_id"], []).append(f["name"])
    ub = ctx.userbot
    lines = [f"🔑 <b>Основной</b> (следит за задержками, Push НЕ шлёт): "
             f"{STATE_LABELS.get(ub.state, ub.state)}" + (f" · {h(ub.me_label)}" if ub.me_label else "")]
    for a in rows_db:
        st = STATE_LABELS.get(ctx.pool.state_of(a["id"]), "—") if ctx.pool else "—"
        lines.append(f"📲 <b>{h(a['name'])}</b> {h(a['label'])} — {st}\n   Push для папок: "
                     f"{h_or(', '.join(folders_by_acc.get(a['id'], [])), 'не назначены')}")
    text = ("📲 <b>Аккаунты</b>\n\n" + "\n".join(lines)
            + f"\n\n📤 В группу «Проёб» пишет: <b>{via_label(ctx)}</b>"
            + "\n\n<i>Основной — следит за чатами. Аккаунты Push — только шлют Push (по одному на папку). "
              "Нажмите на аккаунт — пауза, имя, удаление. Назначить аккаунт папке: «📁 Папки» → папка.</i>")
    rows = [[("🔑 Основной аккаунт (сменить)", "a:menu")], [("📤 Кто пишет в «Проёб»", "pr:via")]]
    rows += [[(f"{'⏸' if a['paused'] else '📲'} {a['name']}", f"acc:o:{a['id']}")] for a in rows_db[:15]]
    rows += [[("➕ Добавить аккаунт Push", "acc:add")], [("⬅️ Назад", "m")]]
    return text, kb(*rows)


def account_card(ctx: Ctx, acc_id: int):
    a = ctx.db.account(acc_id)
    if a is None:
        return accounts_screen(ctx)
    st = STATE_LABELS.get(ctx.pool.state_of(acc_id), "—") if ctx.pool else "—"
    fl = [f["name"] for f in ctx.db.folders() if f["account_id"] == acc_id]
    text = (f"📲 <b>{h(a['name'])}</b> {h(a['label'])}\n\nСостояние: {st}\n"
            f"Push для папок: {h_or(', '.join(fl), 'не назначены')}\n\n"
            "<i>На паузе аккаунт отключается от Telegram и Push с него не идёт — как будто его нет. "
            "Снимите паузу, и он вернётся.</i>")
    return text, kb(
        [(("▶️ Возобновить" if a["paused"] else "⏸ Пауза"), f"acc:pause:{acc_id}")],
        [("✏️ Переименовать", f"acc:ren:{acc_id}"), ("🗑 Удалить", f"acc:del:{acc_id}")],
        [("⬅️ К аккаунтам", "acc:menu")],
    )


def pay_screen(ctx: Ctx):
    db = ctx.db
    mid = db.get("midbot_name")
    text = ("💳 <b>Напоминание об оплате</b> (сотруднику в «Проёб»)\n\n"
            f"Функция: {onoff(feature_on(db, 'pay_enabled'))}\n"
            f"Бот-посредник: <b>{h_or(mid, 'не задан ⚠️')}</b>\n"
            f"Ждать: <b>{db.get('pay_minutes')} мин</b> после «Реквизиты отправлены клиенту»\n\n"
            f"<b>Текст сотруднику (после ссылки и @username):</b>\n{h(db.get('pay_text'))}\n\n"
            "Клиенту бот ничего не пишет. Если за это время сотрудник написал или клиент прислал фото/PDF — напоминания не будет. "
            "<i>⚠️ Для этой функции читается текст ТОЛЬКО сообщений бота-посредника (ищется фраза), он нигде не сохраняется.</i>")
    return text, kb([("🔁 Включить/выключить", "fn:t:pay_enabled")],
                    [("🤖 Бот-посредник", "pay:bot"), ("⏱ Минуты", "pay:min")],
                    [("✏️ Текст для сотрудника", "pay:text")], [("⬅️ Назад", "s:more")])
