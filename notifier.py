"""Отправка сообщений в группу «Проёб» и администратору.

Соблюдение правила безопасности №2: бот пишет ТОЛЬКО в группу «Проёб» (ID из настроек)
и администратору в личный чат. Другие чаты в этом модуле недоступны по построению.
"""
from __future__ import annotations

import asyncio
import logging
import time

log = logging.getLogger("notifier")


class NotConfigured(RuntimeError):
    """ID группы «Проёб» не задан."""


class Notifier:
    """Интерфейс (в тестах подменяется заглушкой)."""

    async def send_alert(self, text: str, alert_id: int) -> tuple[int, int]:
        raise NotImplementedError

    async def edit_alert(self, chat_id: int, message_id: int, text: str) -> None:
        raise NotImplementedError

    async def send_group_text(self, text: str) -> None:
        raise NotImplementedError

    async def react(self, chat_id: int, message_id: int, emoji: str) -> None:
        raise NotImplementedError

    async def notify_admin(self, text: str, key: str = "", min_interval: int = 0) -> None:
        raise NotImplementedError


class BotNotifier(Notifier):
    def __init__(self, bot, db, admin_ids: tuple[int, ...]):
        self.bot = bot
        self.db = db
        self.admin_ids = admin_ids
        self._last_admin_note: dict[str, float] = {}

    # ------------------------------------------------------------ служебное
    def group_id(self) -> int:
        raw = self.db.get("proyob_group_id").strip()
        if not raw:
            raise NotConfigured("ID группы «Проёб» не задан (Меню → Группа «Проёб»)")
        try:
            return int(raw)
        except ValueError as exc:
            raise NotConfigured(f"Некорректный ID группы: {raw}") from exc

    async def _retry(self, factory):
        from aiogram.exceptions import TelegramMigrateToChat, TelegramRetryAfter

        last: Exception | None = None
        for _ in range(4):
            try:
                return await factory()
            except TelegramRetryAfter as exc:
                await asyncio.sleep(float(exc.retry_after) + 1)
                last = exc
            except TelegramMigrateToChat as exc:
                new_id = int(exc.migrate_to_chat_id)
                log.warning("Группа «Проёб» стала супергруппой, новый ID: %s", new_id)
                self.db.set("proyob_group_id", str(new_id))
                last = exc
        raise RuntimeError(f"Не удалось отправить после повторов: {last}")

    # ------------------------------------------------------------ группа «Проёб»
    async def send_alert(self, text: str, alert_id: int) -> tuple[int, int]:
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions

        kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="✅ Выполнено", callback_data=f"done:{alert_id}")]]
        )

        async def go():
            gid = self.group_id()
            msg = await self.bot.send_message(
                gid, text, reply_markup=kb, link_preview_options=LinkPreviewOptions(is_disabled=True)
            )
            return gid, msg.message_id

        gid, mid = await self._retry(go)
        return int(gid), int(mid)

    async def edit_alert(self, chat_id: int, message_id: int, text: str) -> None:
        from aiogram.types import LinkPreviewOptions

        async def go():
            await self.bot.edit_message_text(
                text, chat_id=chat_id, message_id=message_id,
                link_preview_options=LinkPreviewOptions(is_disabled=True), reply_markup=None,
            )

        await self._retry(go)

    async def send_group_text(self, text: str) -> None:
        from aiogram.types import LinkPreviewOptions

        async def go():
            await self.bot.send_message(
                self.group_id(), text, link_preview_options=LinkPreviewOptions(is_disabled=True)
            )

        await self._retry(go)

    async def react(self, chat_id: int, message_id: int, emoji: str) -> None:
        from aiogram.types import ReactionTypeEmoji

        try:
            await self.bot.set_message_reaction(
                chat_id=chat_id, message_id=message_id, reaction=[ReactionTypeEmoji(emoji=emoji)]
            )
        except Exception as exc:  # реакции могут быть отключены в группе — это не ошибка
            log.debug("Реакция не поставлена: %s", exc)

    # ------------------------------------------------------------ администратор (личный чат)
    async def notify_admin(self, text: str, key: str = "", min_interval: int = 0) -> None:
        now = time.time()
        if key and min_interval and now - self._last_admin_note.get(key, 0) < min_interval:
            return
        if key:
            self._last_admin_note[key] = now
        for admin_id in self.admin_ids:
            try:
                await self.bot.send_message(admin_id, text)
            except Exception as exc:
                log.warning("Не удалось написать администратору %s: %s", admin_id, exc)
