"""Обработчики группы «Проёб».

Правило безопасности №2: реагируем ТОЛЬКО в группе, ID которой задан в настройках.
Правило №1: текст сообщений группы проверяется на ключевые слова перерыва
(«атайду», «перерыв», «тут») и нигде не сохраняется.
"""
from __future__ import annotations

import logging
import random
import time

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from .breaks import norm
from .context import Ctx
from .features import feature_on, set_feature
from .services import weekly_text

log = logging.getLogger("group")


def is_week_command(text: str | None) -> bool:
    """/statistika, statistika, /статистика — короткая команда (длинные сообщения не разбираем)."""
    if not text or len(text) > 20:
        return False
    t = norm(text.split("@")[0].strip().lstrip("/"))
    return t == "statistika"


def build_group_router(ctx: Ctx) -> Router:
    r = Router(name="group")

    def proyob_id() -> int | None:
        raw = ctx.db.get("proyob_group_id").strip()
        try:
            return int(raw) if raw else None
        except ValueError:
            return None

    @r.message(F.chat.type.in_({"group", "supergroup"}), Command("proyob", "setgroup"))
    async def cmd_set_group(message: Message) -> None:
        """Команда /proyob, написанная админом ПРЯМО в группе, делает эту группу «Проёб» (ID вводить не нужно)."""
        uid = message.from_user.id if message.from_user else 0
        owner = uid in set(ctx.cfg.admin_ids)
        role = ctx.db.admin_role(uid)
        if not (owner or role == "full"):
            return                                   # чужим молчим
        first = not ctx.db.get("proyob_group_id")
        ctx.db.set("proyob_group_id", str(message.chat.id))
        if first:
            set_feature(ctx.db, "delay_enabled", True)
            try:
                ctx.userbot.start_backfill()
            except Exception:
                log.warning("Не удалось запустить подсчёт лидов")
        try:
            await message.answer("✅ Эта группа теперь «Проёб». Уведомления о задержках будут приходить сюда.")
        except Exception:
            log.warning("Не удалось ответить в группе")

    @r.callback_query(F.data.startswith("done:"))
    async def on_done(cb: CallbackQuery) -> None:
        gid = proyob_id()
        if gid is None or cb.message is None or cb.message.chat.id != gid:
            await cb.answer()
            return
        try:
            alert_id = int((cb.data or "").split(":", 1)[1])
        except ValueError:
            await cb.answer()
            return
        user = cb.from_user
        who = f"@{user.username}" if user.username else user.full_name
        ok = await ctx.monitor.press_done(alert_id, who)
        await cb.answer("Отмечено как выполненное ✅" if ok else "Уже закрыто")

    @r.message(F.chat.type.in_({"group", "supergroup"}), F.sticker)
    async def on_group_sticker(message: Message) -> None:
        """Кто-то прислал стикер в «Проёб» — отвечаем случайным стикером из того же набора (не чаще раза в минуту)."""
        gid = proyob_id()
        if gid is None or message.chat.id != gid or message.from_user is None or message.from_user.is_bot:
            return
        now = time.time()
        if now - ctx.last_sticker_ts < 60:
            return
        ctx.last_sticker_ts = now
        try:
            name = message.sticker.set_name
            pool = (await message.bot.get_sticker_set(name)).stickers if name else []
            pick = random.choice(pool) if pool else message.sticker
            await message.answer_sticker(pick.file_id)
        except Exception:
            log.info("Не удалось ответить стикером")

    @r.message(F.chat.type.in_({"group", "supergroup"}), F.text)
    async def on_group_text(message: Message) -> None:
        gid = proyob_id()
        if gid is None or message.chat.id != gid:
            return                                   # чужие группы полностью игнорируем
        user = message.from_user
        if user is None or user.is_bot:
            return
        ts = int(message.date.timestamp())
        if is_week_command(message.text) and feature_on(ctx.db, "weekly_enabled"):
            if ts - ctx.last_week_cmd >= 30:                 # не чаще раза в 30 с
                ctx.last_week_cmd = ts
                try:
                    await ctx.notifier.send_group_text(weekly_text(ctx.db, ctx.tz, ts))
                except Exception:
                    log.warning("Не удалось отправить итог недели")
            return
        try:
            result = await ctx.breaks.handle_text(user.id, user.username, message.text, ts)
        except Exception:
            log.exception("Ошибка обработки перерыва")
            return
        if result == "started":
            await ctx.notifier.react(message.chat.id, message.message_id, "👌")
        elif result == "ended":
            await ctx.notifier.react(message.chat.id, message.message_id, "👍")
        elif result == "ended_late":
            await ctx.notifier.react(message.chat.id, message.message_id, "🤨")
        elif result == "limit":
            who = f"@{user.username}" if user.username else "Сотрудник"
            try:
                await ctx.notifier.send_group_text(
                    f"{who}, лимит перерывов на сегодня ({ctx.breaks.per_day}) исчерпан."
                )
            except Exception:
                log.warning("Не удалось отправить уведомление о лимите перерывов")

    return r
