"""Обработчики группы «Проёб».

Правило безопасности №2: реагируем ТОЛЬКО в группе, ID которой задан в настройках.
Правило №1: текст сообщений группы проверяется на ключевые слова перерыва
(«атайду», «перерыв», «тут») и нигде не сохраняется.
"""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message

from .breaks import norm
from .context import Ctx
from .features import feature_on
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
