"""Админ-панель в личном чате: 100% инлайн-кнопки; язык — русский или узбекский (i18n.tr при показе).

Бот отвечает ТОЛЬКО администраторам из ADMIN_IDS. Остальные личные сообщения
молча учитываются (для рассылки по тем, кто нажал «Старт») — без ответа.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
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
from .i18n import tr
from .links import is_valid_username, normalize_username, parse_message_link
from .monitor import stavka_limits
from .services import daily_text, day_bounds, fines_text, journal_csv, lead_stats_text, ranking_text, weekly_text
from .statuses import parse_emojis
from .userbot import LoginFlow
from .worktime import fmt_hhmm, parse_date, parse_duration_minutes, parse_hhmm

log = logging.getLogger("panel")
h = S.h                                    # экранирует и помечает данные (перевод их не трогает)
NOPREVIEW = LinkPreviewOptions(is_disabled=True)


class Form(StatesGroup):
    wait = State()
    confirm = State()


PANEL: dict[int, int] = {}            # chat_id -> id сообщения-панели (его правим, а не шлём новые)
_bg: set = set()
QUICK = {"📊 Статус": "st", "📣 Push": "p:menu", "🎛 Функции": "fn", "📅 Лиды": "l:0",
         "📈 Отчёты": "rep:menu", "🏠 Меню": "m"}
QUICK.update({tr(label, "uz"): key for label, key in list(QUICK.items())})   # нижние кнопки на узбекском


async def _safe_delete(msg) -> None:
    try:
        await msg.delete()
    except Exception:
        pass


async def flash(bot, chat_id: int, text: str, seconds: float = 6.0) -> None:
    """Короткое уведомление, которое само исчезает (чат остаётся чистым)."""
    try:
        sent = await bot.send_message(chat_id, S.T(text), parse_mode="HTML", link_preview_options=NOPREVIEW)
    except Exception:
        return

    async def later() -> None:
        await asyncio.sleep(seconds)
        await _safe_delete(sent)

    task = asyncio.create_task(later())
    _bg.add(task)
    task.add_done_callback(_bg.discard)


async def show(target, text: str, markup=None, fresh: bool = False) -> None:
    """Показать экран: правит единственное сообщение-панель; ввод админа удаляется.
    fresh=True — панель переносится вниз чата (для /menu и кнопок снизу)."""
    text = S.T(text)
    kw = dict(reply_markup=markup, parse_mode="HTML", link_preview_options=NOPREVIEW)
    if isinstance(target, CallbackQuery):
        msg = target.message
        try:
            await msg.edit_text(text, **kw)
            PANEL[msg.chat.id] = msg.message_id
        except TelegramBadRequest as exc:
            if "not modified" not in str(exc).lower():
                sent = await msg.answer(text, **kw)
                PANEL[msg.chat.id] = sent.message_id
        return
    chat_id = target.chat.id
    bot = target.bot
    await _safe_delete(target)                               # убираем сообщение админа
    old = PANEL.get(chat_id)
    if old and not fresh:
        try:
            await bot.edit_message_text(text, chat_id=chat_id, message_id=old, **kw)
            return
        except TelegramBadRequest as exc:
            if "not modified" in str(exc).lower():
                return
        except Exception:
            pass
    if old:
        try:
            await bot.delete_message(chat_id, old)
        except Exception:
            pass
    sent = await bot.send_message(chat_id, text, **kw)
    PANEL[chat_id] = sent.message_id


MEDIA_EXT = {"voice": ".ogg", "video": ".mp4", "video_note": ".mp4", "photo": ".jpg",
             "audio": ".mp3", "animation": ".mp4", "document": ".bin"}
MAX_MEDIA = 20 * 1024 * 1024


def extract_media(message: Message):
    """(kind, file_id, size, ext) вложения или None."""
    if message.voice:
        return "voice", message.voice.file_id, message.voice.file_size, ".ogg"
    if message.video_note:
        return "video_note", message.video_note.file_id, message.video_note.file_size, ".mp4"
    if message.video:
        return "video", message.video.file_id, message.video.file_size, ".mp4"
    if message.animation:
        return "animation", message.animation.file_id, message.animation.file_size, ".mp4"
    if message.photo:
        ph = message.photo[-1]
        return "photo", ph.file_id, ph.file_size, ".jpg"
    if message.audio:
        return "audio", message.audio.file_id, message.audio.file_size, ".mp3"
    if message.document:
        d = message.document
        ext = os.path.splitext(d.file_name or "")[1][:8] or ".bin"
        return "document", d.file_id, d.file_size, ext
    return None


def need_rank(data: str) -> int:
    """Минимальный уровень для кнопки: 1 смотреть, 2 Push, 3 полное управление, 4 владелец."""
    p = (data or "").split(":")
    key, arg = p[0], (p[1] if len(p) > 1 else "")
    if key == "adm":
        return 4
    if key in ("m", "st", "help", "lang"):
        return 1
    if key == "pr":
        return 3 if arg == "viaset" else 1
    if key == "l":
        return 3 if arg == "recount" else 1
    if key == "rep":
        return 3 if arg in ("now", "time", "backup") else 1
    if key == "f" and arg in ("list", "o"):
        return 1
    if key == "c" and arg == "l":
        return 1
    if key == "fn" and arg == "":
        return 1
    if key == "p" and arg == "menu":
        return 1
    if key == "b" and arg == "log":
        return 1
    if key in ("p", "b", "tpl", "x"):
        return 2
    return 3


PUSH_ACTS = {"push_text", "push_limit", "push_period", "push_idle", "push_repeat", "push_emoji", "skip_emoji",
             "push_newlead", "push_media", "tpl_name", "find", "push_quiet"}


def act_rank(act: str) -> int:
    if act == "admin_add":
        return 4
    if act == "lead_check":
        return 1
    if act in PUSH_ACTS:
        return 2
    return 3


def build_admin_router(ctx: Ctx) -> Router:
    def PERIOD_PROMPT() -> str:
        return (f"📅 Сейчас: <b>{S.period_label(ctx)}</b>\n\nКакие лиды получат Push (по дате появления лида). "
                "Пришлите «с» и «по»: <code>26.08.2026 14.10.2026</code>, либо одну дату (с неё до сегодня). "
                "<code>*</code> — сбросить период.")

    r = Router(name="admin")
    owners = set(ctx.cfg.admin_ids)
    db = ctx.db
    S.set_lang_source(lambda: db.get("lang"))
    PANEL_FLASH = ctx.flash_seconds

    def rank_of(uid: int) -> int:
        if uid in owners:
            return 4
        return {"view": 1, "push": 2, "full": 3}.get(db.admin_role(uid) or "", 0)

    def msg_ok(message: Message) -> bool:
        return message.chat.type == "private" and message.from_user is not None and rank_of(message.from_user.id) > 0

    def cb_ok(cb: CallbackQuery) -> bool:
        return (cb.message is not None and cb.message.chat.type == "private"
                and rank_of(cb.from_user.id) > 0)

    r.message.filter(msg_ok)
    r.callback_query.filter(cb_ok)

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

    def chat_of(target) -> int:
        return target.chat.id if isinstance(target, Message) else target.message.chat.id

    async def notice(target, text: str, seconds: float | None = None) -> None:
        await flash(target.bot if isinstance(target, Message) else target.message.bot, chat_of(target), text,
                    PANEL_FLASH if seconds is None else seconds)

    async def home(target, state: FSMContext | None = None, note: str | None = None, fresh: bool = False) -> None:
        if state is not None:
            await state.clear()
        if note:
            await notice(target, note)
        await show(target, *S.main(ctx, rank_of(target.from_user.id)), fresh=fresh)

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
        await message.answer(S.T(text), parse_mode="HTML")

    # ------------------------------------------------------------ команды
    @r.message(Command("start", "menu", "panel"))
    async def cmd_menu(message: Message, state: FSMContext) -> None:
        uid = message.from_user.id
        if db.get(f"panel_kb_{uid}") != "1":                     # нижние кнопки — один раз
            db.set(f"panel_kb_{uid}", "1")
            try:
                await message.answer(S.T("⌨️ Кнопки быстрого доступа — внизу, у строки ввода."), reply_markup=S.reply_kb())
            except Exception:
                pass
        await home(message, state, fresh=True)

    @r.message(Command("status", "push", "report", "functions"))
    async def cmd_quick(message: Message, state: FSMContext) -> None:
        cmd = (message.text or "").split()[0].lstrip("/").split("@")[0]
        await go_quick(message, state, {"status": "📊 Статус", "push": "📣 Push", "report": "📈 Отчёты",
                                        "functions": "🎛 Функции"}[cmd])

    async def go_quick(message: Message, state: FSMContext, label: str) -> None:
        await state.clear()
        rk = rank_of(message.from_user.id)
        key = QUICK[label]
        if rk < need_rank(key):
            await notice(message, "⛔ Недостаточно прав.")
            await _safe_delete(message)
            return
        if key == "st":
            args = S.status(ctx)
        elif key == "p:menu":
            args = S.push_menu(ctx)
        elif key == "fn":
            args = S.functions(ctx, rk)
        elif key == "l:0":
            args = S.leads(ctx, 0)
        elif key == "rep:menu":
            args = S.reports_menu(ctx, rk)
        else:
            args = S.main(ctx, rk)
        await show(message, *args, fresh=True)

    @r.message(F.text.in_(set(QUICK)))
    async def quick_buttons(message: Message, state: FSMContext) -> None:
        await go_quick(message, state, message.text)

    # ------------------------------------------------------------ ввод текста (FSM)
    @r.message(Form.wait, F.text)
    async def on_input(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        act = data.get("act", "")
        text = (message.text or "").strip()
        if rank_of(message.from_user.id) < act_rank(act):
            await state.clear()
            await notice(message, "⛔ Недостаточно прав.")
            await _safe_delete(message)
            return
        err = await handle_input(message, state, act, text, data)
        if err:
            await notice(message, f"⚠️ {err}", PANEL_FLASH + 3)
            if act not in ("login_code", "login_pass"):
                await _safe_delete(message)

    @r.message(Form.wait, F.voice | F.video | F.video_note | F.photo | F.document | F.audio | F.animation)
    async def on_media(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        if data.get("act") != "push_media" or rank_of(message.from_user.id) < 2:
            await notice(message, "Сейчас вложение не ждут. Нажмите «Отмена».")
            return
        found = extract_media(message)
        if found is None:
            await notice(message, "⚠️ Этот тип вложения не поддерживается.")
            return
        kind, file_id, size, ext = found
        if size and size > MAX_MEDIA:
            await notice(message, "⚠️ Файл больше 20 МБ — бот не сможет его скачать. Пришлите поменьше.", PANEL_FLASH + 3)
            await _safe_delete(message)
            return
        folder = os.path.join(str(ctx.cfg.data_dir), "push_media")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, f"{uuid.uuid4().hex}{ext}")
        try:
            await message.bot.download(file_id, destination=path)
        except Exception as exc:
            await notice(message, f"⚠️ Не удалось скачать файл ({type(exc).__name__}).", PANEL_FLASH + 3)
            return
        old = ctx.broadcaster.media()
        db.set("push_media", json.dumps({"kind": kind, "file_id": file_id, "path": path}))
        drop_media_file(old)
        await notice(message, "✅ Вложение сохранено.")
        await after_push_edit(message, state, data)

    def drop_media_file(media: dict | None) -> None:
        if media and media.get("path") and media["path"] not in db.media_paths_in_use():
            try:
                os.remove(media["path"])
            except OSError:
                pass

    @r.message(Form.wait)
    async def on_input_other(message: Message) -> None:
        await notice(message, "Пришлите ответ текстом или нажмите «Отмена».")
        await _safe_delete(message)

    @r.message()
    async def fallback(message: Message, state: FSMContext) -> None:
        await home(message, state, fresh=True)

    async def after_push_edit(message: Message, state: FSMContext, data: dict) -> None:
        """После правки параметров Push — вернуться в меню Push или на экран подтверждения."""
        if data.get("ret") == "confirm" and ctx.broadcaster.has_content():
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
            await notice(message, f"✅ Добавлено: {', '.join('@' + a for a in added) or 'ничего нового'}")
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
            await notice(message, f"✅ Отслеживание с {d.strftime('%d.%m.%Y')}")
            await show(message, *S.more_settings(ctx))
            return None

        if act == "set_bw":
            nums = re.findall(r"\d+", text)
            if (len(nums) not in (2, 3) or not (1 <= int(nums[0]) <= 20) or not (10 <= int(nums[1]) <= 600)
                    or (len(nums) == 3 and not (30 <= int(nums[2]) <= 1440))):
                return ("Формат: «3 46 180» — сколько кружков завершают ставку (1–20); через сколько минут после "
                        "нового лида напомнить «ставка», если кружков меньше (10–600); предел ставки в минутах "
                        "(30–1440, третье число можно не писать).")
            db.set("botwatch_count", nums[0])
            db.set("botwatch_remind", nums[1])
            if len(nums) == 3:
                db.set("botwatch_cap", nums[2])
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

        if act == "set_nl":
            if not text.isdigit() or not (10 <= int(text) <= 1440):
                return "Минуты: число от 10 до 1440 (например 120)."
            db.set("newlead_minutes", text)
            await state.clear()
            await show(message, *S.more_settings(ctx))
            return None

        if act == "acc_ren":
            if not text or len(text) > 30:
                return "Имя: до 30 символов."
            db.rename_account(data["acc"], text)
            await state.clear()
            await show(message, *S.account_card(ctx, data["acc"]))
            return None

        if act == "set_plim":
            if not text.isdigit() or int(text) > 100000:
                return "Пришлите число (0 — не задавать, брать из общего)."
            db.set_folder_field(data["fid"], "push_limit", int(text))
            await state.clear()
            await show(message, *S.folder_card(ctx, data["fid"]))
            return None

        if act in ("set_quiet", "push_quiet"):
            raw = text.strip()
            if raw in ("0", "-", "*", ""):
                frm = to = ""
            else:
                parts = re.findall(r"\d{1,2}[:.]\d{2}", raw)
                if len(parts) != 2:
                    return "Пришлите два времени, например <code>21:00 09:00</code>, или <code>0</code> — убрать."
                from .broadcast import _hm
                parts = [x.replace(".", ":") for x in parts]
                if any(_hm(x) is None for x in parts):
                    return "Время вида ЧЧ:ММ, например <code>21:00 09:00</code>."
                frm, to = [f"{int(x.split(':')[0]):02d}:{x.split(':')[1]}" for x in parts]
            if act == "set_quiet":
                db.set_folder_field(data["fid"], "quiet_from", frm)
                db.set_folder_field(data["fid"], "quiet_to", to)
                await state.clear()
                await show(message, *S.folder_card(ctx, data["fid"]))
            else:
                db.set("push_quiet_from", frm)
                db.set("push_quiet_to", to)
                await state.clear()
                await show(message, *S.push_settings(ctx))
            return None

        if act == "set_fine":
            digits = re.sub(r"[\s,._]", "", text)
            if not digits.isdigit() or int(digits) > 10**9:
                return "Пришлите сумму цифрами, например 10000 (0 — без штрафа)."
            db.set_folder_field(data["fid"], "fine", int(digits))
            await state.clear()
            await show(message, *S.folder_card(ctx, data["fid"]))
            return None

        if act == "pay_min":
            if not text.isdigit() or not (1 <= int(text) <= 1440):
                return "Минуты: число от 1 до 1440."
            db.set("pay_minutes", text)
            await state.clear()
            await show(message, *S.pay_screen(ctx))
            return None

        if act == "pay_text":
            if not text or len(text) > 1000:
                return "Текст: от 1 до 1000 символов (по-русски)."
            db.set("pay_text", text)
            await state.clear()
            await show(message, *S.pay_screen(ctx))
            return None

        if act == "pay_bot":
            uname = text.strip().lstrip("@")
            if not is_valid_username(uname):
                return "Пришлите @username бота-посредника (например @client_bot)."
            try:
                uid, title = await ctx.userbot.resolve_user_id(uname)
            except Exception as exc:
                return f"Не нашёл такого пользователя ({type(exc).__name__}). Юзербот должен быть подключён."
            db.set("midbot_id", str(uid))
            db.set("midbot_name", "@" + uname)
            changed()
            await state.clear()
            await show(message, *(S.more_settings(ctx) if data.get("back") == "s:more" else S.pay_screen(ctx)))
            return None

        if act == "tpl_name":
            if not text or len(text) > 32:
                return "Название: от 1 до 32 символов."
            if not ctx.broadcaster.has_content():
                await state.clear()
                return "Сначала задайте текст или вложение Push."
            db.add_template(text, db.get("push_text"), db.get("push_media"))
            await state.clear()
            await show(message, *S.templates(ctx))
            return None

        if act == "push_newlead":
            try:
                minutes = parse_duration_minutes(text)
            except ValueError:
                return "Формат: <code>60</code> (минут), <code>2ч</code>; <code>0</code> — не ждать."
            if minutes > 60 * 24 * 30:
                return "Слишком большое значение."
            db.set("push_newlead_minutes", str(minutes))
            await after_push_edit(message, state, data)
            return None

        if act == "report_time":
            try:
                at = parse_hhmm(text)
            except ValueError:
                return "Формат: 23:30"
            db.set("report_time", fmt_hhmm(at))
            await state.clear()
            await show(message, *S.reports_menu(ctx, rank_of(message.from_user.id)))
            return None

        if act == "admin_add":
            if not re.fullmatch(r"\d{5,15}", text):
                return "Пришлите числовой ID пользователя (узнать: перешлите мне его сообщение)."
            uid = int(text)
            if uid in owners or db.admin_role(uid):
                return "Этот пользователь уже в списке."
            db.set_admin(uid, "view", int(time.time()))
            await state.clear()
            await show(message, *S.admins_screen(ctx))
            return None

        if act == "push_text":
            if text in ("-", "—", "0"):                       # убрать текст (например, оставить только голосовое/кружок)
                db.set("push_text", "")
            elif not text or len(text) > 3500:
                return "Текст: от 1 до 3500 символов. Пришлите <code>-</code>, чтобы убрать текст."
            else:
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
            raw = text.strip().lower()
            try:
                minutes = parse_duration_minutes(raw + "ч" if raw.isdigit() and raw != "0" else raw)
            except ValueError:
                return "Пришлите число часов, например <code>6</code>; можно <code>30м</code>, <code>1д</code>; <code>0</code> — не ограничивать."
            if minutes > 60 * 24 * 365:
                return "Слишком большое значение."
            db.set("push_repeat_minutes", str(minutes))
            await after_push_edit(message, state, data)
            return None

        if act in ("push_emoji", "skip_emoji"):
            emojis = [] if text.strip().lower() in ("*", "все", "всё", "hammasi") else parse_emojis(text)
            db.set("push_emojis" if act == "push_emoji" else "skip_emojis", " ".join(emojis))
            await after_push_edit(message, state, data)
            return None

        if act == "lead_check":
            parsed = parse_message_link(text)
            if parsed is None:
                return "Нужна ссылка вида https://t.me/c/1234567890/10/55 (нажмите на сообщение → «Копировать ссылку»)."
            await state.clear()
            await show(message, *S.lead_check(ctx, *parsed))
            return None

        if act == "find":
            try:
                found = await ctx.userbot.search_dialogs("" if text == "*" else text)
            except Exception as exc:
                return f"Не удалось выполнить поиск: {exc}. Проверьте, что юзербот подключён."
            await state.clear()
            lines = [f"• {S.h_or(n, '(без названия)')} — <code>{i}</code> ({k})" for i, n, k in found] or ["Ничего не найдено."]
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

        sdata = await state.get_data()
        target = "acc" if sdata.get("acc_login") else "main"
        acc_name = sdata.get("acc_name", "")

        async def finish(flow: LoginFlow) -> None:
            session = flow.session_string()
            await flow.cancel()
            ctx.logins.pop(uid, None)
            if target == "acc":
                db.add_account(acc_name or "Аккаунт", session, None, "", int(time.time()))
                ctx.pool.sync()
                await state.clear()
                await notice(message, "✅ Аккаунт добавлен. Назначьте его папке: Папки → папка → «📲 Аккаунт Push».", 10)
                await show(message, *S.accounts_screen(ctx))
                return
            await ctx.userbot.set_session(session)
            await state.clear()
            await notice(message, "✅ Аккаунт подключён. Подтягиваю папки и чаты… Через минуту проверьте «📊 Статус».", 10)
            await show(message, *S.main(ctx, rank_of(uid)))

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
            await state.set_data({"act": "login_code", "acc_login": target == "acc", "acc_name": acc_name})
            await show(message,
                       "📨 Код отправлен в Telegram на этот аккаунт (сообщение от «Telegram»).\n\n"
                       "Пришлите код, <b>разделив цифры пробелами или дефисами</b>, например <code>1 2 3 4 5</code>. "
                       "Если отправить код слитно, Telegram аннулирует его.", S.cancel_kb())
            return None

        flow = ctx.logins.get(uid)
        if flow is None:
            await state.clear()
            return "Сессия входа не найдена — начните заново: Меню → Аккаунты → Основной аккаунт."

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
                await state.set_data({"act": "login_pass", "acc_login": target == "acc", "acc_name": acc_name})
                await show(message, "🔐 Включена двухэтапная проверка. Пришлите пароль (сообщение будет удалено).",
                           S.cancel_kb())
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
        # сразу снимаем «часики» с кнопки — кнопка не «висит», пока считается экран
        try:
            await cb.answer()
        except Exception:
            pass
        alert = None
        try:
            if rank_of(cb.from_user.id) < need_rank(cb.data or ""):
                alert = "⛔ Недостаточно прав для этого действия."
            else:
                alert = await route(cb, state, (cb.data or "").split(":"))
        except Exception:
            log.exception("Ошибка в обработчике кнопки %s", cb.data)
            alert = "Что-то пошло не так, попробуйте ещё раз."
        if alert:
            await notice(cb, alert, PANEL_FLASH + 2)

    async def route(cb: CallbackQuery, state: FSMContext, p: list[str]) -> str | None:
        key = p[0]
        arg = p[1] if len(p) > 1 else ""
        rk = rank_of(cb.from_user.id)

        if key == "m":
            await state.clear()
            await show(cb, *S.main(ctx, rk))
        elif key == "lang":
            db.set("lang", "uz" if db.get("lang") != "uz" else "ru")
            await show(cb, *S.main(ctx, rk))
            try:                                         # нижние кнопки и меню «/» — тоже на новом языке
                await cb.message.answer(S.T("⌨️ Кнопки быстрого доступа — внизу, у строки ввода."),
                                        reply_markup=S.reply_kb())
                await ctx.bot.set_my_commands(S.bot_commands())
            except Exception:
                log.warning("Не удалось обновить нижние кнопки/команды после смены языка", exc_info=True)
        elif key == "pr":
            await state.clear()
            if arg == "via":
                await show(cb, *S.via_pick(ctx))
            elif arg == "viaset":
                v = p[2] if len(p) > 2 else "bot"
                if v.startswith("acc") and v[3:].isdigit() and db.account(int(v[3:])):
                    db.set("alert_via", "acc:" + v[3:])
                elif v == "main":
                    db.set("alert_via", "main")
                else:
                    db.set("alert_via", "bot")
                await show(cb, *S.via_pick(ctx))
            else:
                await show(cb, *S.proyob_screen(ctx))
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
            elif arg == "acc":
                await show(cb, *S.folder_acc_pick(ctx, int(p[2])))
            elif arg == "accset":
                fid, aid = int(p[2]), int(p[3])
                if db.folder(fid) is None:
                    return "Папка не найдена"
                if aid and db.account(aid) is None:
                    return "Аккаунт не найден"
                db.set_folder_field(fid, "account_id", aid or None)
                await show(cb, *S.folder_card(ctx, fid))
            elif arg == "plim":
                f = db.folder(int(p[2]))
                if f is None:
                    return "Папка не найдена"
                await prompt(cb, state, "set_plim",
                             f"🔢 Сколько Push для папки <b>{h(f['name'])}</b> за один раз (сейчас: {f['push_limit'] or 'общее число'}). "
                             "Например <code>50</code>. <code>0</code> — не задавать (брать из общего количества).",
                             f"f:o:{p[2]}", fid=int(p[2]))
            elif arg == "quiet":
                f = db.folder(int(p[2]))
                if f is None:
                    return "Папка не найдена"
                await prompt(cb, state, "set_quiet",
                             f"🌙 Когда НЕ слать Push папке <b>{h(f['name'])}</b>. Пришлите «с» и «до», например "
                             "<code>21:00 09:00</code>. <code>0</code> — использовать общее время.",
                             f"f:o:{p[2]}", fid=int(p[2]))
            elif arg in ("dly", "psh"):
                f = db.folder(int(p[2]))
                if f is None:
                    return "Папка не найдена"
                col = "delay_on" if arg == "dly" else "push_on"
                db.set_folder_field(f["id"], col, 0 if f[col] else 1)
                changed()
                await show(cb, *S.folder_card(ctx, f["id"]))
            elif arg == "fine":
                f = db.folder(int(p[2]))
                if f is None:
                    return "Папка не найдена"
                await prompt(cb, state, "set_fine",
                             f"💰 Штраф за каждую задержку для папки <b>{h(f['name'])}</b> (сейчас: {f['fine'] or 'не задан'}). "
                             "Пришлите сумму цифрами, например <code>10000</code>. <code>0</code> — без штрафа.",
                             f"f:o:{p[2]}", fid=int(p[2]))
            elif arg == "sync":
                if ctx.userbot.state != "online":
                    return "Юзербот не подключён: Меню → Аккаунты → Основной аккаунт."
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
                             "<b>Проще всего:</b> добавьте бота в группу «Проёб» (сделайте админом) и напишите в самой группе "
                             "команду <code>/proyob</code> — группа назначится сама.\n\n"
                             "Либо пришлите ID группы здесь (<code>-100…</code>), найти его можно через «🔎 Найти чат / ID».")
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
                need, remind, cap = stavka_limits(db)
                await prompt(cb, state, "set_bw",
                             "🎬 <b>Ставка нового лида</b>\n"
                             f"Сейчас: ставка заканчивается после <b>{need}</b> кружков; если через <b>{remind // 60} мин</b> "
                             f"после появления лида их меньше — «ставка» в «Проёб»; предел ставки <b>{cap // 60} мин</b>.\n\n"
                             "Пришлите три числа, например <code>3 46 180</code>:\n"
                             "1) сколько кружков завершают ставку;\n"
                             "2) через сколько минут после нового лида напомнить «ставка», если кружков меньше "
                             "(кружки идут примерно раз в 13 мин);\n"
                             "3) предел: через сколько минут ставка заканчивается, даже если кружков меньше.", "s:more")
            elif arg == "midbot":
                await prompt(cb, state, "pay_bot", "🤖 Пришлите @username бота-посредника («CLIENT …»). Его сообщения "
                             "считаются сообщениями клиента, а «Реквизиты отправлены…» запускает напоминание об оплате.",
                             "s:more")
            elif arg == "nl":
                await prompt(cb, state, "set_nl",
                             f"🆕 Сегодняшний лид без активности дольше N минут — напомню в «Проёб» (один раз). "
                             f"Сейчас: <b>{db.get('newlead_minutes')} мин</b>. Пришлите число минут (например <code>120</code>). "
                             "Включить/выключить — «Функции».", "s:more")
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
                await show(cb, *S.functions(ctx, rk))
            elif arg == "reset":
                await show(cb, "♻️ Сбросить настройки (лимит, часы, период, эмодзи, количество Push и т.д.) к заводским? "
                               "Папки, сотрудники, чаты, лиды, группа «Проёб» и аккаунт сохранятся.",
                           S.kb([("✅ Да, сбросить", "fn:resetok"), ("❌ Нет", "fn")]))
            elif arg == "resetok":
                reset_settings(db)
                changed()
                await show(cb, *S.functions(ctx, rk))
                return "Настройки сброшены"
            else:
                await state.clear()
                await show(cb, *S.functions(ctx, rk))

        # ---- Push (только вручную)
        elif key == "p":
            if arg == "menu":
                await state.clear()
                await show(cb, *S.push_menu(ctx))
            elif arg == "text":
                await prompt(cb, state, "push_text", "✏️ Пришлите текст Push (до 3500 символов). Пришлите <code>-</code>, чтобы убрать текст (если есть медиа — уйдёт только оно).", "p:menu")
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
                             "🔁 Тем, кому Push уже отправляли, повторно не слать, пока не пройдёт N часов "
                             f"(если Push 3 раза в день — поставьте <code>4</code>). Сейчас: "
                             f"<b>{S.dur_label(db.get_int('push_repeat_minutes'))}</b>.\n\n"
                             "Пришлите число часов (<code>4</code>), либо <code>30м</code>, <code>1д</code>. "
                             "<code>0</code> — не ограничивать.", "p:menu")
            elif arg == "emoji":
                await prompt(cb, state, "push_emoji",
                             f"🏷 Сейчас Push получают: <b>{S.h_or(db.get('push_emojis'), 'все, кроме исключённых')}</b>. "
                             "Пришлите эмодзи (можно несколько) или <code>*</code> — все, кроме исключённых.", "p:menu")
            elif arg == "skip":
                await prompt(cb, state, "skip_emoji",
                             f"🚫 Исключены (не учитываются в задержках и Push): <b>{h(db.get('skip_emojis'))}</b>. "
                             "Пришлите новый список, например <code>⚡ 💊</code>.", "p:menu")
            elif arg == "mode":
                db.set("push_mode", "bot" if db.get("push_mode") == "userbot" else "userbot")
                await show(cb, *S.push_menu(ctx))
            elif arg == "plan":
                await show(cb, *S.push_plan(ctx))
            elif arg == "set":
                await show(cb, *S.push_settings(ctx))
            elif arg == "quiet":
                await prompt(cb, state, "push_quiet",
                             "🌙 Общее время, когда НЕ слать Push (у папки может быть своё). Пришлите «с» и «до», например "
                             "<code>21:00 09:00</code>. <code>0</code> — убрать.", "p:set")
            elif arg == "folders":
                await state.clear()
                await show(cb, *S.push_folders(ctx))
            elif arg == "fold":
                # единый выключатель Push у папки (тот же, что в карточке папки)
                if p[2] == "all":
                    for f in db.folders():
                        db.set_folder_field(f["id"], "push_on", 1)
                else:
                    f = db.folder(int(p[2]))
                    if f is not None:
                        db.set_folder_field(f["id"], "push_on", 0 if f["push_on"] else 1)
                await show(cb, *S.push_folders(ctx))
            elif arg == "slot":
                order = ["all", "day", "evening"]
                cur = db.get("push_slot")
                db.set("push_slot", order[(order.index(cur) + 1) % 3] if cur in order else "all")
                await show(cb, *S.push_menu(ctx))
            elif arg == "media":
                await prompt(cb, state, "push_media",
                             "📎 Пришлите вложение для Push: 🎤 голосовое, ⭕ кружок, 🎬 видео, 🖼 фото или файл (до 20 МБ). "
                             "Текст из «✏️ Текст» пойдёт подписью (к кружку — отдельным сообщением).", "p:menu")
            elif arg == "nomedia":
                old = ctx.broadcaster.media()
                db.set("push_media", "")
                drop_media_file(old)
                await show(cb, *S.push_menu(ctx))
            elif arg == "test":
                if not ctx.broadcaster.has_content():
                    return "Сначала задайте текст или вложение."
                try:
                    await ctx.broadcaster.send_test(cb.message.chat.id)
                except Exception as exc:
                    return f"Не удалось отправить пробное ({type(exc).__name__})."
                return "Пробное отправлено вам ⬆️ (только вам, клиентам не уходит)."
            elif arg == "newlead":
                await prompt(cb, state, "push_newlead",
                             f"🆕 Новому лиду Push не шлём, пока не прошло N минут с его появления (идут автоставки). "
                             f"Сейчас: <b>{S.dur_label(db.get_int('push_newlead_minutes'))}</b>.\n\n"
                             "Пришлите: <code>60</code> (минут), <code>2ч</code>. <code>0</code> — не ждать.", "p:menu")

        elif key == "tpl":
            if arg == "menu":
                await state.clear()
                await show(cb, *S.templates(ctx))
            elif arg == "add":
                if not ctx.broadcaster.has_content():
                    return "Сначала задайте текст или вложение Push."
                await prompt(cb, state, "tpl_name", "💾 Пришлите название шаблона.", "tpl:menu")
            elif arg == "use":
                t = db.template(int(p[2]))
                if t is None:
                    return "Шаблон не найден"
                old = ctx.broadcaster.media()
                db.set("push_text", t["text"])
                db.set("push_media", t["media"])
                drop_media_file(old)
                await show(cb, *S.push_menu(ctx))
                return "Шаблон применён"
            elif arg == "del":
                t = db.template(int(p[2]))
                if t is not None:
                    db.delete_template(t["id"])
                    try:
                        drop_media_file(json.loads(t["media"]) if t["media"] else None)
                    except ValueError:
                        pass
                await show(cb, *S.templates(ctx))

        elif key == "rep":
            now = int(time.time())
            day_s, day_e = day_bounds(now, ctx.tz)
            back = S.kb([("⬅️ Назад", "rep:menu")])
            if arg == "menu":
                await state.clear()
                await show(cb, *S.reports_menu(ctx, rk))
            elif arg == "day":
                await show(cb, daily_text(db, ctx.tz, day_s, day_e, "сегодня"), back)
            elif arg == "leads":
                await show(cb, lead_stats_text(db, ctx.tz, now), back)
            elif arg in ("r1", "r7", "r30"):
                days = int(arg[1:])
                title = "сегодня" if days == 1 else f"{days} дней"
                await show(cb, ranking_text(db, day_e - days * 86400, day_e, title), back)
            elif arg in ("f7", "f30"):
                days = int(arg[1:])
                await show(cb, fines_text(db, day_e - days * 86400, day_e, f"{days} дней"), back)
            elif arg == "week":
                await show(cb, weekly_text(db, ctx.tz, now, markup=True), back)
            elif arg == "now":
                await ctx.bot.send_message(cb.message.chat.id, S.T(daily_text(db, ctx.tz, day_s, day_e, "сегодня")),
                                           parse_mode="HTML")
                return "Отчёт отправлен"
            elif arg == "time":
                await prompt(cb, state, "report_time",
                             f"🕙 Сейчас отчёт приходит в <b>{h(db.get('report_time'))}</b> (Ташкент). "
                             "Пришлите время, например <code>23:30</code>.", "rep:menu")
            elif arg == "journal":
                from aiogram.types import BufferedInputFile
                data = journal_csv(db, ctx.tz, now - 86400)
                stamp = datetime.fromtimestamp(now, ctx.tz).strftime("%Y-%m-%d_%H%M")
                await ctx.bot.send_document(cb.message.chat.id, BufferedInputFile(data, f"journal_{stamp}.csv"),
                                            caption=S.T("🧾 Журнал решений бота за сутки: время, ссылка, кто писал и "
                                                        "что бот решил (без текста сообщений). Можно переслать разработчику."))
                return "Файл отправлен"
            elif arg == "backup":
                from aiogram.types import FSInputFile
                path = None
                try:
                    path = ctx.services.make_backup(now)
                    await ctx.bot.send_document(cb.message.chat.id, FSInputFile(path),
                                                caption=S.T("💾 Копия настроек (без текстов сообщений и без сессии)."))
                finally:
                    if path:
                        try:
                            os.remove(path)
                        except Exception:
                            pass
                return "Файл отправлен"

        elif key == "acc":
            if arg == "menu":
                await state.clear()
                await show(cb, *S.accounts_screen(ctx))
            elif arg == "add":
                if not (ctx.cfg.api_id and ctx.cfg.api_hash):
                    return "На сервере не заданы API_ID и API_HASH."
                n = len(db.accounts()) + 1
                await prompt(cb, state, "login_phone",
                             "📲 Пришлите номер телефона нового аккаунта Push (не основного!), например <code>+998901234567</code>.\n\n"
                             "Аккаунт должен состоять в рабочих чатах нужной папки.", "acc:menu",
                             acc_login=True, acc_name=f"Push {n}")
            elif arg == "o":
                await show(cb, *S.account_card(ctx, int(p[2])))
            elif arg == "pause":
                a = db.account(int(p[2]))
                if a is None:
                    return "Аккаунт не найден"
                db.set_account_paused(a["id"], not a["paused"])
                ctx.pool.sync()
                await show(cb, *S.account_card(ctx, a["id"]))
            elif arg == "ren":
                await prompt(cb, state, "acc_ren", "✏️ Пришлите новое имя аккаунта (до 30 символов).",
                             f"acc:o:{p[2]}", acc=int(p[2]))
            elif arg == "del":
                a = db.account(int(p[2]))
                if a is not None and p[3:4] != ["ok"]:
                    await show(cb, f"🗑 Удалить аккаунт <b>{h(a['name'])}</b>? Папки, которым он назначен, останутся без аккаунта Push.",
                               S.kb([("✅ Да, удалить", f"acc:del:{a['id']}:ok"), ("❌ Нет", f"acc:o:{a['id']}")]))
                    return None
                db.delete_account(int(p[2]))
                ctx.pool.sync()
                changed()
                await show(cb, *S.accounts_screen(ctx))

        elif key == "pay":
            if arg == "menu":
                await state.clear()
                await show(cb, *S.pay_screen(ctx))
            elif arg == "bot":
                await prompt(cb, state, "pay_bot", "🤖 Пришлите @username бота-посредника («CLIENT …»), "
                             "который пишет «Реквизиты отправлены клиенту».", "pay:menu")
            elif arg == "min":
                await prompt(cb, state, "pay_min", f"⏱ Сейчас {db.get('pay_minutes')} мин. Через сколько минут после «Реквизиты отправлены клиенту» "
                             "напомнить сотруднику в «Проёб» (если нет ни ответа, ни чека)? Пришлите число.", "pay:menu")
            elif arg == "text":
                await prompt(cb, state, "pay_text", "✏️ Пришлите текст напоминания сотруднику (по-русски). Он придёт в «Проёб» после ссылки и @username.", "pay:menu")

        elif key == "adm":
            if arg == "menu":
                await state.clear()
                await show(cb, *S.admins_screen(ctx))
            elif arg == "add":
                await prompt(cb, state, "admin_add",
                             "👮 Пришлите числовой ID нового админа (по умолчанию он будет только смотреть). "
                             "ID можно узнать, переслав мне его сообщение.", "adm:menu")
            elif arg == "role":
                uid = int(p[2])
                cur = db.admin_role(uid)
                if cur:
                    order = ["view", "push", "full"]
                    db.set_admin(uid, order[(order.index(cur) + 1) % 3] if cur in order else "view", int(time.time()))
                await show(cb, *S.admins_screen(ctx))
            elif arg == "del":
                db.delete_admin(int(p[2]))
                await show(cb, *S.admins_screen(ctx))

        elif key == "b":
            data = await state.get_data()
            if arg == "new":
                text = db.get("push_text").strip()
                if not ctx.broadcaster.has_content():
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
            elif arg == "newlead":
                await prompt(cb, state, "push_newlead",
                             f"🆕 Новому лиду Push не шлём N минут (идут автоставки). Сейчас: "
                             f"<b>{S.dur_label(db.get_int('push_newlead_minutes'))}</b>. "
                             "Пришлите минуты (<code>60</code>, <code>2ч</code>), <code>0</code> — не ждать.",
                             "p:menu", ret="confirm")
            elif arg == "go":
                text = data.get("text") or db.get("push_text")
                if not ctx.broadcaster.has_content():
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
                    return "Останавливаю…"
                return "Сейчас рассылки нет."
            elif arg == "log":
                await show(cb, *S.broadcast_log(ctx))

        # ---- лиды
        elif key == "l":
            if arg == "chk":
                await prompt(cb, state, "lead_check",
                             "🔍 Пришлите ссылку на сообщение лида (в топике: нажмите на сообщение → «Копировать ссылку»). "
                             "Покажу, идёт ли ставка, сколько пришло кружков и ждёт ли клиент ответа.", "pr:menu")
                return None
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
                             "📲 Пришлите номер телефона нового основного аккаунта, например <code>+998901234567</code>.\n\n"
                             + (f"Сейчас подключён: <b>{h(ctx.userbot.me_label)}</b> — он будет заменён.\n\n" if ctx.userbot.me_label else "")
                             + "Новый аккаунт должен состоять во всех рабочих чатах и иметь те же папки Telegram "
                               "(названия как у папок бота). Лиды, отчёты и настройки сохраняются.", "a:menu")
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
                        S.T(f"📣 Рассылка идёт… {sent}/{total}, ошибок: {failed}"),
                        reply_markup=S.kb([("⏹ Остановить", "b:stop")]),
                    )
                except Exception:
                    pass

            result = await ctx.broadcaster.run(text, mode, "manual", progress)
            report = S.T(ctx.broadcaster.report(result))
            try:
                await msg.edit_text(report, reply_markup=S.kb([("📣 Push", "p:menu"), ("🏠 Меню", "m")]))
            except Exception:
                sent = await ctx.bot.send_message(chat_id, report)
                PANEL[chat_id] = sent.message_id

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
