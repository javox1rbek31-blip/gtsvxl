"""Админ-панель в личном чате: 100% инлайн-кнопки, русский язык.

Бот отвечает ТОЛЬКО администраторам из ADMIN_IDS. Остальные личные сообщения
молча учитываются (для рассылки по тем, кто нажал «Старт») — без ответа.
"""
from __future__ import annotations

import html
import logging
import re
import time
from datetime import datetime

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, ChatMemberUpdated, LinkPreviewOptions, Message

from . import screens as S
from .context import Ctx
from .features import FEATURES, SPEED_ORDER, reset_settings, set_feature
from .links import is_valid_username, normalize_username
from .statuses import parse_emojis
from .userbot import LoginFlow
from .worktime import fmt_hhmm, parse_date, parse_duration_minutes, parse_hhmm

log = logging.getLogger("panel")
h = html.escape
NOPREVIEW = LinkPreviewOptions(is_disabled=True)


class Form(StatesGroup):
    wait = State()
    confirm = State()


async def show(target, text: str, markup=None) -> None:
    if isinstance(target, CallbackQuery):
        msg = target.message
        try:
            await msg.edit_text(text, reply_markup=markup, parse_mode="HTML", link_preview_options=NOPREVIEW)
        except TelegramBadRequest as exc:
            if "not modified" not in str(exc).lower():
                await msg.answer(text, reply_markup=markup, parse_mode="HTML", link_preview_options=NOPREVIEW)
    else:
        await target.answer(text, reply_markup=markup, parse_mode="HTML", link_preview_options=NOPREVIEW)


def build_admin_router(ctx: Ctx) -> Router:
    def PERIOD_PROMPT() -> str:
        return (f"📅 Сейчас: <b>{S.period_label(ctx)}</b>\n\nКакие лиды получат Push (по дате появления лида). "
                "Пришлите «с» и «по»: <code>26.08.2026 14.10.2026</code>, либо одну дату (с неё до сегодня). "
                "<code>*</code> — сбросить период.")

    r = Router(name="admin")
    admins = set(ctx.cfg.admin_ids)
    r.message.filter(F.chat.type == "private", F.from_user.id.in_(admins))
    r.callback_query.filter(F.from_user.id.in_(admins), F.message.chat.type == "private")
    db = ctx.db

    # ------------------------------------------------------------ вспомогательное
    async def prompt(target, state: FSMContext, act: str, text: str, back: str = "m", **data) -> None:
        await state.set_state(Form.wait)
        await state.set_data({"act": act, "back": back, **data})
        await show(target, text, S.cancel_kb(back))

    def changed() -> None:
        ctx.monitor.reload()

    def sync_later() -> None:
        if ctx.userbot.state == "online":
            ctx.bg(ctx.userbot.refresh_all())

    async def home(target, state: FSMContext | None = None, note: str | None = None) -> None:
        if state is not None:
            await state.clear()
        if note:
            await show(target, note)
        await show(target, *S.main(ctx))

    # ------------------------------------------------------------ ID helper (пересланные сообщения)
    @r.message(F.forward_origin)
    async def id_helper(message: Message) -> None:
        o = message.forward_origin
        kind = getattr(o, "type", "")
        if kind == "channel":
            c = o.chat
            text = f"📢 Канал <b>{h(c.title or '')}</b>\nID: <code>{c.id}</code>"
        elif kind == "chat":
            c = o.sender_chat
            text = f"👥 Группа <b>{h(c.title or '')}</b>\nID: <code>{c.id}</code>"
        elif kind == "user":
            u = o.sender_user
            name = h(" ".join(x for x in (u.first_name, u.last_name) if x))
            text = (f"👤 Пользователь <b>{name}</b>"
                    f"{' @' + h(u.username) if u.username else ''}\nID: <code>{u.id}</code>\n\n"
                    "ℹ️ Telegram не раскрывает ID <b>группы</b>, из которой переслано сообщение. "
                    "Чтобы узнать ID группы — «🔎 Найти чат / ID» в меню.")
        else:
            text = ("🙈 Отправитель скрыл аккаунт — Telegram не передаёт его ID.\n"
                    "Для ID группы используйте «🔎 Найти чат / ID» в меню.")
        await message.answer(text, parse_mode="HTML")

    # ------------------------------------------------------------ команды
    @r.message(Command("start", "menu", "panel"))
    async def cmd_menu(message: Message, state: FSMContext) -> None:
        await home(message, state)

    # ------------------------------------------------------------ ввод текста (FSM)
    @r.message(Form.wait, F.text)
    async def on_input(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        act = data.get("act", "")
        text = (message.text or "").strip()
        err = await handle_input(message, state, act, text, data)
        if err:
            await message.answer(f"⚠️ {err}", reply_markup=S.cancel_kb(data.get("back", "m")))

    @r.message(Form.wait)
    async def on_input_other(message: Message) -> None:
        await message.answer("Пришлите ответ текстом или нажмите «Отмена».", reply_markup=S.cancel_kb())

    @r.message()
    async def fallback(message: Message, state: FSMContext) -> None:
        await home(message, state)

    async def after_push_edit(message: Message, state: FSMContext, data: dict) -> None:
        """После правки параметров Push — вернуться в меню Push или на экран подтверждения."""
        if data.get("ret") == "confirm" and db.get("push_text").strip():
            await state.set_state(Form.confirm)
            await state.set_data({"text": db.get("push_text")})
            await show(message, *S.broadcast_confirm(ctx, db.get("push_text")))
        else:
            await state.clear()
            await show(message, *S.push_menu(ctx))

    async def handle_input(message: Message, state: FSMContext, act: str, text: str, data: dict) -> str | None:
        """Возвращает текст ошибки (состояние сохраняется) или None при успехе."""
        if act == "add_folder":
            if not text or len(text) > 32:
                return "Название: от 1 до 32 символов."
            fid = db.add_folder(text)
            if fid is None:
                return "Папка с таким названием уже есть."
            await state.clear()
            sync_later()
            await show(message, *S.folder_card(ctx, fid))
            return None

        if act == "ren_folder":
            if not text or len(text) > 32:
                return "Название: от 1 до 32 символов."
            if not db.rename_folder(data["fid"], text):
                return "Папка с таким названием уже есть."
            await state.clear()
            sync_later()
            await show(message, *S.folder_card(ctx, data["fid"]))
            return None

        if act == "add_emp":
            names = [normalize_username(x) for x in re.split(r"[\s,;]+", text) if x.strip()]
            bad = [n for n in names if not is_valid_username(n)]
            if not names or bad:
                return "Укажите @username (латиница, цифры, _; от 4 символов). Можно несколько через пробел."
            added = [n for n in names if db.add_employee(data["fid"], n)]
            changed()
            await state.clear()
            sync_later()
            await message.answer(f"✅ Добавлено: {', '.join('@' + a for a in added) or 'ничего нового'}")
            await show(message, *S.folder_card(ctx, data["fid"]))
            return None

        if act == "add_chat":
            try:
                ids = [int(x) for x in re.split(r"[\s,;]+", text) if x.strip()]
            except ValueError:
                return "ID чата — число, например -1001234567890. Можно несколько через пробел."
            if not ids:
                return "Пришлите хотя бы один ID."
            for cid in ids:
                db.upsert_chat(cid, data["fid"], "", "manual")
            changed()
            await state.clear()
            sync_later()
            await show(message, *S.chats(ctx, data["fid"]))
            return None

        if act == "set_limit":
            if not text.isdigit() or not (1 <= int(text) <= 720):
                return "Введите число минут от 1 до 720."
            db.set("limit_minutes", text)
            await home(message, state, f"✅ Лимит ответа: {text} мин")
            return None

        if act == "set_hours":
            parts = re.split(r"\s*[-–—\s]\s*", text)
            try:
                a, b = parse_hhmm(parts[0]), parse_hhmm(parts[1])
            except (ValueError, IndexError):
                return "Формат: 10:00-23:00"
            if a >= b:
                return "Начало должно быть раньше конца (одни сутки)."
            db.set("work_start", fmt_hhmm(a))
            db.set("work_end", fmt_hhmm(b))
            await home(message, state, f"✅ Рабочий день: {fmt_hhmm(a)}–{fmt_hhmm(b)} (Ташкент)")
            return None

        if act == "set_group":
            try:
                gid = int(text)
            except ValueError:
                return "ID группы — число вида -1001234567890."
            if gid >= 0:
                return "ID группы отрицательный (-100…). Найдите его через «🔎 Найти чат / ID»."
            try:
                await ctx.bot.get_chat(gid)
                member = await ctx.bot.get_chat_member(gid, ctx.bot.id)
                if member.status in ("left", "kicked"):
                    return "Бот не состоит в этой группе — добавьте его и повторите."
            except Exception as exc:
                return f"Бот не видит этот чат ({type(exc).__name__}). Добавьте бота в группу и проверьте ID."
            first_time = not db.get("proyob_group_id")
            db.set("proyob_group_id", str(gid))
            note = f"✅ Группа «Проёб»: {gid}"
            if first_time:
                set_feature(db, "delay_enabled", True)     # отсчёт «с чистого листа»: старые лиды не потревожат
                note += ("\nКонтроль задержек включён — учитываются только новые сообщения с этого момента. "
                         f"Подсчёт лидов с {db.get('track_from')} запускаю.")
                ctx.userbot.start_backfill()
            await home(message, state, note)
            return None

        if act == "set_track":
            try:
                d = parse_date(text)
            except ValueError as exc:
                return str(exc)
            db.set("track_from", d.strftime("%Y-%m-%d"))
            await state.clear()
            await message.answer(f"✅ Отслеживание с {d.strftime('%d.%m.%Y')}")
            await show(message, *S.more_settings(ctx))
            return None

        if act == "set_bw":
            nums = re.findall(r"\d+", text)
            if len(nums) != 2 or not (1 <= int(nums[0]) <= 20) or not (5 <= int(nums[1]) <= 240):
                return "Формат: «3 20» — сколько автоставок и сколько минут ждать (5–240)."
            db.set("botwatch_count", nums[0])
            db.set("botwatch_wait", nums[1])
            await state.clear()
            await show(message, *S.more_settings(ctx))
            return None

        if act == "set_brk":
            nums = re.findall(r"\d+", text)
            if len(nums) != 2 or not (1 <= int(nums[0]) <= 240) or not (1 <= int(nums[1]) <= 20):
                return "Формат: «30 3» — минут перерыва и сколько раз в день."
            db.set("break_minutes", nums[0])
            db.set("breaks_per_day", nums[1])
            await state.clear()
            await show(message, *S.more_settings(ctx))
            return None

        if act == "push_text":
            if not text or len(text) > 3500:
                return "Текст: от 1 до 3500 символов."
            db.set("push_text", text)
            await after_push_edit(message, state, data)
            return None

        if act == "push_limit":
            if not text.isdigit() or int(text) > 5000:
                return "Введите число от 0 до 5000 (0 = все подходящие)."
            db.set("push_limit", str(int(text)))
            await after_push_edit(message, state, data)
            return None

        if act == "push_period":
            low = text.lower()
            if low in ("*", "все", "всё", "сброс", "hammasi"):
                db.set("push_from", "")
                db.set("push_to", "")
            else:
                found = re.findall(r"\d{4}-\d{2}-\d{2}|\d{1,2}\.\d{1,2}\.\d{2,4}", text)
                if not found:
                    return "Формат: «26.08.2026 14.10.2026» (с и по) или одна дата (с неё до сегодня). «*» — сбросить."
                try:
                    d_from = parse_date(found[0])
                    d_to = parse_date(found[1]) if len(found) > 1 else None
                except ValueError as exc:
                    return str(exc)
                if d_to is not None and d_to < d_from:
                    return "Вторая дата должна быть не раньше первой."
                db.set("push_from", d_from.strftime("%Y-%m-%d"))
                db.set("push_to", d_to.strftime("%Y-%m-%d") if d_to else "")
            await after_push_edit(message, state, data)
            return None

        if act == "push_idle":
            try:
                minutes = parse_duration_minutes(text)
            except ValueError:
                return "Формат: <code>30</code> (минут), <code>2ч</code>, <code>1д</code>; <code>0</code> — выключить."
            if minutes > 60 * 24 * 365:
                return "Слишком большое значение."
            db.set("push_idle_minutes", str(minutes))
            await after_push_edit(message, state, data)
            return None

        if act == "push_repeat":
            try:
                minutes = parse_duration_minutes(text if text.strip().lower()[-1:].isalpha() else text.strip() + "д")
            except ValueError:
                return "Пришлите число дней, например <code>7</code>; <code>0</code> — не ограничивать."
            db.set("push_repeat_days", str(minutes // 1440))
            await after_push_edit(message, state, data)
            return None

        if act in ("push_emoji", "skip_emoji"):
            emojis = [] if text.strip().lower() in ("*", "все", "всё", "hammasi") else parse_emojis(text)
            db.set("push_emojis" if act == "push_emoji" else "skip_emojis", " ".join(emojis))
            await after_push_edit(message, state, data)
            return None

        if act == "find":
            try:
                found = await ctx.userbot.search_dialogs("" if text == "*" else text)
            except Exception as exc:
                return f"Не удалось выполнить поиск: {exc}. Проверьте, что юзербот подключён."
            await state.clear()
            lines = [f"• {h(n)} — <code>{i}</code> ({k})" for i, n, k in found] or ["Ничего не найдено."]
            await show(message, "🔎 <b>Результаты поиска</b>\n\n" + "\n".join(lines) +
                       "\n\n<i>Нажмите на ID, чтобы скопировать.</i>",
                       S.kb([("🔎 Искать ещё", "x:find"), ("⬅️ Назад", "m")]))
            return None

        return await handle_login_input(message, state, act, text)

    # ------------------------------------------------------------ вход в аккаунт юзербота
    async def handle_login_input(message: Message, state: FSMContext, act: str, text: str) -> str | None:
        from telethon import errors

        uid = message.from_user.id

        async def wipe() -> None:
            try:
                await message.delete()
            except Exception:
                pass

        async def finish(flow: LoginFlow) -> None:
            session = flow.session_string()
            await flow.cancel()
            ctx.logins.pop(uid, None)
            await ctx.userbot.set_session(session)
            await state.clear()
            await message.answer("✅ Аккаунт подключён. Подтягиваю папки и чаты… Через минуту проверьте «📊 Статус».")
            await show(message, *S.main(ctx))

        if act == "login_phone":
            phone = re.sub(r"[^\d+]", "", text)
            phone = phone if phone.startswith("+") else "+" + phone
            if not re.fullmatch(r"\+\d{9,15}", phone):
                return "Номер в международном формате, например +998901234567."
            flow = LoginFlow(ctx.cfg.api_id, ctx.cfg.api_hash)
            try:
                await flow.start(phone)
            except errors.FloodWaitError as exc:
                await flow.cancel()
                return f"Слишком много попыток. Подождите {exc.seconds} сек."
            except errors.PhoneNumberInvalidError:
                await flow.cancel()
                return "Telegram не принял этот номер."
            except errors.ApiIdInvalidError:
                await flow.cancel()
                return "Неверные API_ID / API_HASH в настройках сервера."
            except Exception as exc:
                await flow.cancel()
                return f"Не удалось отправить код ({type(exc).__name__})."
            ctx.logins[uid] = flow
            await state.set_state(Form.wait)
            await state.set_data({"act": "login_code"})
            await message.answer(
                "📨 Код отправлен в Telegram на этот аккаунт (сообщение от «Telegram»).\n\n"
                "Пришлите код, <b>разделив цифры пробелами или дефисами</b>, например <code>1 2 3 4 5</code>. "
                "Если отправить код слитно, Telegram аннулирует его.",
                parse_mode="HTML", reply_markup=S.cancel_kb(),
            )
            return None

        flow = ctx.logins.get(uid)
        if flow is None:
            await state.clear()
            return "Сессия входа не найдена — начните заново: Меню → Аккаунт юзербота."

        if act == "login_code":
            await wipe()
            digits = re.sub(r"\D", "", text)
            if len(digits) < 4:
                return "Не вижу код. Пришлите цифры через пробел: 1 2 3 4 5."
            try:
                res = await flow.submit_code(digits)
            except errors.PhoneCodeInvalidError:
                return "Неверный код. Попробуйте ещё раз."
            except errors.PhoneCodeExpiredError:
                await flow.cancel()
                ctx.logins.pop(uid, None)
                await state.clear()
                return "Код устарел. Начните вход заново."
            except Exception as exc:
                return f"Ошибка входа ({type(exc).__name__})."
            if res == "password":
                await state.set_data({"act": "login_pass"})
                await message.answer("🔐 Включена двухэтапная проверка. Пришлите пароль (сообщение будет удалено).",
                                     reply_markup=S.cancel_kb())
                return None
            await finish(flow)
            return None

        if act == "login_pass":
            await wipe()
            try:
                await flow.submit_password(text)
            except errors.PasswordHashInvalidError:
                return "Неверный пароль. Попробуйте ещё раз."
            except Exception as exc:
                return f"Ошибка входа ({type(exc).__name__})."
            await finish(flow)
            return None

        return "Неизвестное действие — откройте /menu."

    # ------------------------------------------------------------ кнопки
    @r.callback_query()
    async def on_cb(cb: CallbackQuery, state: FSMContext) -> None:
        alert = None
        try:
            alert = await route(cb, state, (cb.data or "").split(":"))
        except Exception:
            log.exception("Ошибка в обработчике кнопки %s", cb.data)
            alert = "Что-то пошло не так, попробуйте ещё раз."
        try:
            await cb.answer(alert, show_alert=bool(alert))
        except Exception:
            pass

    async def route(cb: CallbackQuery, state: FSMContext, p: list[str]) -> str | None:
        key = p[0]
        arg = p[1] if len(p) > 1 else ""

        if key == "m":
            await state.clear()
            await show(cb, *S.main(ctx))
        elif key == "st":
            await show(cb, *S.status(ctx))
        elif key == "help":
            await show(cb, S.HELP, S.kb([("⬅️ Назад", "m")]))

        # ---- папки
        elif key == "f":
            if arg == "list":
                await state.clear()
                await show(cb, *S.folders(ctx))
            elif arg == "o":
                await state.clear()
                await show(cb, *S.folder_card(ctx, int(p[2])))
            elif arg == "add":
                await prompt(cb, state, "add_folder", "➕ Пришлите название папки (как в Telegram), например <b>РД7</b>.", "f:list")
            elif arg == "ren":
                await prompt(cb, state, "ren_folder", "✏️ Пришлите новое название папки.", f"f:o:{p[2]}", fid=int(p[2]))
            elif arg == "del":
                f = db.folder(int(p[2]))
                if f is None:
                    return "Папка не найдена"
                await show(cb, f"🗑 Удалить папку <b>{h(f['name'])}</b> вместе с сотрудниками и чатами?",
                           S.kb([("✅ Да, удалить", f"f:delok:{p[2]}"), ("❌ Нет", f"f:o:{p[2]}")]))
            elif arg == "delok":
                db.delete_folder(int(p[2]))
                changed()
                await show(cb, *S.folders(ctx))
            elif arg == "sync":
                if ctx.userbot.state != "online":
                    return "Юзербот не подключён: Меню → Аккаунт юзербота."
                try:
                    added, removed = await ctx.userbot.sync_folders()
                except Exception as exc:
                    return f"Ошибка синхронизации: {type(exc).__name__}"
                ctx.bg(ctx.userbot.reconcile())
                miss = ctx.userbot.missing_folders
                note = f"Добавлено чатов: {added}, убрано: {removed}."
                if miss:
                    note += f" Нет папки в Telegram: {', '.join(miss)}"
                await show(cb, *S.folders(ctx))
                return note[:190]

        elif key == "e":
            if arg == "add":
                await prompt(cb, state, "add_emp",
                             "👤 Пришлите @username сотрудника (можно несколько через пробел).",
                             f"f:o:{p[2]}", fid=int(p[2]))
            elif arg == "del":
                emp = db.employee(int(p[2]))
                if emp is not None:
                    db.delete_employee(emp["id"])
                    changed()
                    await show(cb, *S.folder_card(ctx, emp["folder_id"]))

        elif key == "c":
            if arg == "l":
                await state.clear()
                await show(cb, *S.chats(ctx, int(p[2])))
            elif arg == "add":
                await prompt(cb, state, "add_chat",
                             "➕ Пришлите ID чата (например <code>-1001234567890</code>). "
                             "Узнать ID: «🔎 Найти чат / ID» в главном меню.",
                             f"c:l:{p[2]}", fid=int(p[2]))
            elif arg == "del":
                db.delete_chat(int(p[3]))
                changed()
                await show(cb, *S.chats(ctx, int(p[2])))
                return "Чат убран. Если он есть в папке Telegram — вернётся при синхронизации."

        # ---- настройки
        elif key == "s":
            if arg == "limit":
                await prompt(cb, state, "set_limit", f"⏱ Сейчас: <b>{db.get('limit_minutes')} мин</b>. Пришлите новое число минут.")
            elif arg == "hours":
                await prompt(cb, state, "set_hours",
                             f"🕒 Сейчас: <b>{db.get('work_start')}–{db.get('work_end')}</b> (Ташкент). "
                             "Пришлите в формате <code>10:00-23:00</code>.")
            elif arg == "group":
                await prompt(cb, state, "set_group",
                             f"👥 Сейчас: <code>{h(db.get('proyob_group_id') or '—')}</code>\n\n"
                             "Пришлите ID группы «Проёб» (<code>-100…</code>). Добавьте бота в группу и найдите ID "
                             "через «🔎 Найти чат / ID».")
            elif arg == "more":
                await state.clear()
                await show(cb, *S.more_settings(ctx))
            elif arg == "track":
                await prompt(cb, state, "set_track",
                             f"📆 Сейчас: <b>{h(db.get('track_from'))}</b>. Лиды, написавшие раньше этой даты, "
                             "игнорируются. Пришлите дату, например <code>26.08.2026</code>.", "s:more")
            elif arg == "timer":
                db.set("timer_from", "last" if db.get("timer_from") != "last" else "first")
                await show(cb, *S.more_settings(ctx))
            elif arg == "bw":
                await prompt(cb, state, "set_bw",
                             f"🤖 Сейчас: {db.get('botwatch_count')} автоставок, ждать {db.get('botwatch_wait')} мин. "
                             "Пришлите два числа, например <code>3 20</code> (сколько автоставок после «Новый чат» "
                             "и через сколько минут тишины предупредить).", "s:more")
            elif arg == "brk":
                await prompt(cb, state, "set_brk",
                             f"☕ Сейчас: {db.get('break_minutes')} мин, {db.get('breaks_per_day')} раз в день. "
                             "Пришлите два числа, например <code>30 3</code>.", "s:more")

        # ---- функции и выключатели
        elif key == "fn":
            if arg == "t":
                feature = p[2]
                if feature == "bot_enabled" or feature in FEATURES:
                    set_feature(db, feature, db.get(feature) != "1")
                    changed()
                await show(cb, *S.functions(ctx))
            elif arg == "reset":
                await show(cb, "♻️ Сбросить настройки (лимит, часы, период, эмодзи, количество Push и т.д.) к заводским? "
                               "Папки, сотрудники, чаты, лиды, группа «Проёб» и аккаунт сохранятся.",
                           S.kb([("✅ Да, сбросить", "fn:resetok"), ("❌ Нет", "fn")]))
            elif arg == "resetok":
                reset_settings(db)
                changed()
                await show(cb, *S.functions(ctx))
                return "Настройки сброшены"
            else:
                await state.clear()
                await show(cb, *S.functions(ctx))

        # ---- Push (только вручную)
        elif key == "p":
            if arg == "menu":
                await state.clear()
                await show(cb, *S.push_menu(ctx))
            elif arg == "text":
                await prompt(cb, state, "push_text", "✏️ Пришлите текст Push (до 3500 символов).", "p:menu")
            elif arg == "limit":
                await prompt(cb, state, "push_limit",
                             f"🔢 Сейчас: <b>{db.get('push_limit')}</b>. Сколько человек за один Push? "
                             "Пришлите число (<code>0</code> = все подходящие). Меняйте в любой момент.", "p:menu")
            elif arg == "period":
                await prompt(cb, state, "push_period", PERIOD_PROMPT(), "p:menu")
            elif arg == "speed":
                cur = db.get("push_speed")
                nxt = SPEED_ORDER[(SPEED_ORDER.index(cur) + 1) % len(SPEED_ORDER)] if cur in SPEED_ORDER else "normal"
                db.set("push_speed", nxt)
                await show(cb, *S.push_menu(ctx))
                if nxt == "fast":
                    return "Быстро: выше риск ограничений Telegram"
            elif arg == "idle":
                await prompt(cb, state, "push_idle",
                             f"⏸ Не писать тем, кто <b>недавно</b> писал (клиент или менеджер). Сейчас: <b>{S.dur_label(db.get_int('push_idle_minutes'))}</b>.\n\n"
                             "Пришлите, сколько времени должно пройти с последнего сообщения: "
                             "<code>30</code> (минут), <code>2ч</code>, <code>1д</code>. <code>0</code> — не проверять.", "p:menu")
            elif arg == "repeat":
                await prompt(cb, state, "push_repeat",
                             f"🔁 Не слать Push повторно тем, кому уже отправляли за последние N дней. "
                             f"Сейчас: <b>{S.dur_label(db.get_int('push_repeat_days') * 1440)}</b>.\n\n"
                             "Пришлите число дней, например <code>7</code>. <code>0</code> — не ограничивать.", "p:menu")
            elif arg == "emoji":
                await prompt(cb, state, "push_emoji",
                             f"🏷 Сейчас Push получают: <b>{h(db.get('push_emojis') or 'все, кроме исключённых')}</b>. "
                             "Пришлите эмодзи (можно несколько) или <code>*</code> — все, кроме исключённых.", "p:menu")
            elif arg == "skip":
                await prompt(cb, state, "skip_emoji",
                             f"🚫 Исключены (не учитываются в задержках и Push): <b>{h(db.get('skip_emojis'))}</b>. "
                             "Пришлите новый список, например <code>⚡ 💊</code>.", "p:menu")
            elif arg == "mode":
                db.set("push_mode", "bot" if db.get("push_mode") == "userbot" else "userbot")
                await show(cb, *S.push_menu(ctx))

        elif key == "b":
            data = await state.get_data()
            if arg == "new":
                text = db.get("push_text").strip()
                if not text:
                    await prompt(cb, state, "push_text", "✏️ Пришлите текст Push.", "p:menu", ret="confirm")
                else:
                    await state.set_state(Form.confirm)
                    await state.set_data({"text": text})
                    await show(cb, *S.broadcast_confirm(ctx, text))
            elif arg == "text":
                await prompt(cb, state, "push_text", "✏️ Пришлите новый текст Push.", "p:menu", ret="confirm")
            elif arg == "limit":
                await prompt(cb, state, "push_limit",
                             f"🔢 Сейчас: <b>{db.get('push_limit')}</b>. Пришлите, скольким отправить (<code>0</code> = всем).",
                             "p:menu", ret="confirm")
            elif arg == "period":
                await prompt(cb, state, "push_period", PERIOD_PROMPT(), "p:menu", ret="confirm")
            elif arg == "idle":
                await prompt(cb, state, "push_idle",
                             f"⏸ Не писать тем, кто <b>недавно</b> писал (клиент или менеджер). Сейчас: <b>{S.dur_label(db.get_int('push_idle_minutes'))}</b>.\n\n"
                             "Пришлите, сколько времени должно пройти с последнего сообщения: "
                             "<code>30</code> (минут), <code>2ч</code>, <code>1д</code>. <code>0</code> — не проверять.", "p:menu", ret="confirm")
            elif arg == "mode":
                db.set("push_mode", "bot" if db.get("push_mode") == "userbot" else "userbot")
                await show(cb, *S.broadcast_confirm(ctx, data.get("text") or db.get("push_text")))
            elif arg == "go":
                text = data.get("text") or db.get("push_text")
                if not text:
                    await show(cb, *S.push_menu(ctx))
                    return "Нет текста Push."
                if ctx.broadcaster.running:
                    return "Другая рассылка уже идёт."
                await state.clear()
                start_broadcast(cb, text, db.get("push_mode"))
                await show(cb, "📣 Push запущен. Отчёт придёт сюда.", S.kb([("⏹ Остановить", "b:stop"), ("⬅️ Меню", "m")]))
            elif arg == "stop":
                if ctx.broadcaster.running:
                    ctx.broadcaster.stop()
                    return "Останавливаю после текущего сообщения…"
                return "Сейчас рассылки нет."
            elif arg == "log":
                await show(cb, *S.broadcast_log(ctx))

        # ---- лиды
        elif key == "l":
            if arg == "recount":
                if ctx.userbot.state != "online":
                    return "Юзербот не подключён."
                started = ctx.userbot.start_backfill()
                await show(cb, *S.leads(ctx, 0))
                return "Пересчёт запущен, это займёт несколько минут." if started else "Пересчёт уже идёт."
            await show(cb, *S.leads(ctx, int(arg or 0)))

        # ---- аккаунт юзербота
        elif key == "a":
            if arg == "menu":
                await state.clear()
                await show(cb, *S.account(ctx))
            elif arg == "login":
                if not (ctx.cfg.api_id and ctx.cfg.api_hash):
                    return "На сервере не заданы API_ID и API_HASH (my.telegram.org)."
                await prompt(cb, state, "login_phone",
                             "📲 Пришлите номер телефона аккаунта-юзербота, например <code>+998901234567</code>.\n\n"
                             "Это аккаунт, который состоит во всех рабочих чатах и папках.", "a:menu")
            elif arg == "sync":
                if ctx.userbot.state != "online":
                    return "Юзербот не подключён."
                ctx.bg(ctx.userbot.refresh_all())
                return "Обновляю… Результат смотрите в «📊 Статус»."
            elif arg == "logout":
                await show(cb, "🚪 Отключить аккаунт юзербота? Мониторинг остановится.",
                           S.kb([("✅ Да, отключить", "a:logoutok"), ("❌ Нет", "a:menu")]))
            elif arg == "logoutok":
                await ctx.userbot.logout()
                await show(cb, *S.account(ctx))

        elif key == "x":
            await prompt(cb, state, "find",
                         "🔎 Пришлите часть названия чата или группы (или <code>*</code> — показать первые 15). "
                         "Покажу ID, который нужен для настроек.")
        return None

    def start_broadcast(cb: CallbackQuery, text: str, mode: str) -> None:
        chat_id = cb.message.chat.id
        msg = cb.message

        async def job() -> None:
            async def progress(sent: int, failed: int, total: int) -> None:
                try:
                    await msg.edit_text(
                        f"📣 Рассылка идёт… {sent}/{total}, ошибок: {failed}",
                        reply_markup=S.kb([("⏹ Остановить", "b:stop")]),
                    )
                except Exception:
                    pass

            result = await ctx.broadcaster.run(text, mode, "manual", progress)
            await ctx.bot.send_message(chat_id, ctx.broadcaster.report(result))

        ctx.bg(job())

    return r


def build_public_router(ctx: Ctx) -> Router:
    """Не-админы: молча запоминаем тех, кто нажал «Старт» (для рассылки через бота). Ответов нет."""
    r = Router(name="public")

    @r.my_chat_member(F.chat.type == "private")
    async def on_member(ev: ChatMemberUpdated) -> None:
        status = str(getattr(ev.new_chat_member.status, "value", ev.new_chat_member.status))
        if status == "kicked":
            ctx.db.mark_blocked(ev.chat.id)
        elif status == "member":
            ctx.db.add_bot_user(ev.chat.id, int(time.time()))

    @r.message(F.chat.type == "private")
    async def on_private(message: Message) -> None:
        if message.from_user and not message.from_user.is_bot:
            ctx.db.add_bot_user(message.from_user.id, int(time.time()))

    return r
