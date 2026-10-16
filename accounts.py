"""Дополнительные аккаунты для Push (по одному на папку/группу папок).

Эти аккаунты ТОЛЬКО отправляют Push: они не слушают сообщения и ничего не читают.
Мониторинг задержек идёт через основной аккаунт юзербота.
"""
from __future__ import annotations

import asyncio
import logging
import time

from .userbot import Userbot

log = logging.getLogger("accounts")


class PushAccount(Userbot):
    def __init__(self, cfg, db, notifier, tz, acc_id: int):
        super().__init__(cfg, db, None, notifier, tz)
        self.acc_id = acc_id

    def session(self) -> str:
        row = self.db.account(self.acc_id)
        return row["session"] if row else ""

    @property
    def name(self) -> str:
        row = self.db.account(self.acc_id)
        return row["name"] if row else f"#{self.acc_id}"

    async def _serve(self, session: str) -> None:
        from telethon import TelegramClient
        from telethon.sessions import StringSession

        self.state = "connecting"
        client = TelegramClient(StringSession(session), self.cfg.api_id, self.cfg.api_hash,
                                connection_retries=-1, retry_delay=2, auto_reconnect=True,
                                request_retries=5, flood_sleep_threshold=60)
        self.client = client
        await client.connect()
        if not await client.is_user_authorized():
            self.state = "unauthorized"
            await self.notifier.notify_admin(
                f"🔑 Аккаунт Push «{self.name}» разлогинен. Меню → Аккаунты Push → подключите заново.",
                key=f"acc-unauth-{self.acc_id}", min_interval=3600)
            await self._disconnect()
            await self._sleep_or_wake(300)
            return
        me = await client.get_me()
        label = " ".join(x for x in (getattr(me, "first_name", "") or "",
                                     f"@{me.username}" if getattr(me, "username", None) else "") if x) or str(me.id)
        self.me_label = label
        self.db.set_account_info(self.acc_id, int(me.id), label)
        self.state = "online"
        try:
            await client.get_dialogs(limit=None)          # прогрев кэша чатов, чтобы Push не падал на ValueError
            self._dialogs_refreshed = True
        except Exception:
            pass
        failures = 0
        last_ping = time.monotonic()
        while True:
            await self._sleep_or_wake(15)
            if self.client is not client or self.session() != session:
                return
            if time.monotonic() - last_ping >= 60:
                last_ping = time.monotonic()
                try:
                    await asyncio.wait_for(client.get_me(), timeout=30)
                    failures = 0
                    self.state = "online"
                except Exception as exc:
                    failures += 1
                    if failures >= 3:
                        raise ConnectionError("нет ответа от Telegram") from exc

    async def set_session(self, session: str) -> None:
        self.state = "connecting"
        self.wake()

    async def logout(self) -> None:
        self.state = "no_session"
        self.wake()


class AccountPool:
    """Запускает/останавливает аккаунты Push по таблице accounts."""

    def __init__(self, cfg, db, notifier, tz, main_userbot):
        self.cfg, self.db, self.notifier, self.tz, self.main = cfg, db, notifier, tz, main_userbot
        self.accounts: dict[int, PushAccount] = {}
        self._tasks: dict[int, asyncio.Task] = {}

    async def _runner(self, acc: PushAccount) -> None:
        while True:
            try:
                await acc.run()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Аккаунт Push %s упал, перезапуск", acc.acc_id)
            await asyncio.sleep(10)

    def sync(self) -> None:
        """Привести работающие аккаунты в соответствие с базой (вызывается регулярно и после правок)."""
        wanted = {r["id"] for r in self.db.accounts()}
        for acc_id in wanted - set(self.accounts):
            acc = PushAccount(self.cfg, self.db, self.notifier, self.tz, acc_id)
            self.accounts[acc_id] = acc
            self._tasks[acc_id] = asyncio.create_task(self._runner(acc))
        for acc_id in set(self.accounts) - wanted:
            self._tasks.pop(acc_id).cancel()
            acc = self.accounts.pop(acc_id)
            asyncio.create_task(acc._disconnect())

    def wake(self, acc_id: int) -> None:
        acc = self.accounts.get(acc_id)
        if acc:
            acc.wake()

    def sender(self, acc_id: int | None):
        """Аккаунт Push папки (None, если не назначен/не запущен). Основной юзербот для Push НЕ используется."""
        return self.accounts.get(acc_id) if acc_id else None

    def state_of(self, acc_id: int | None) -> str:
        acc = self.sender(acc_id)
        return acc.state if acc is not None else "no_session"

    async def stop(self) -> None:
        for t in self._tasks.values():
            t.cancel()
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        for acc in self.accounts.values():
            await acc._disconnect()
