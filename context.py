"""Общий контейнер зависимостей, передаваемый обработчикам."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any
from zoneinfo import ZoneInfo

from .breaks import BreakService
from .broadcast import Broadcaster
from .config import Config
from .db import Database
from .monitor import Monitor
from .notifier import BotNotifier
from .userbot import LoginFlow, Userbot

log = logging.getLogger("ctx")


@dataclass
class Ctx:
    cfg: Config
    db: Database
    tz: ZoneInfo
    bot: Any
    notifier: BotNotifier
    monitor: Monitor
    breaks: BreakService
    userbot: Userbot
    broadcaster: Broadcaster
    started_at: float = field(default_factory=time.time)
    logins: dict[int, LoginFlow] = field(default_factory=dict)
    _tasks: set = field(default_factory=set)

    def bg(self, coro) -> None:
        """Запустить фоновую задачу и не потерять ссылку на неё."""
        task = asyncio.create_task(coro)
        self._tasks.add(task)

        def _done(t: asyncio.Task) -> None:
            self._tasks.discard(t)
            if not t.cancelled() and t.exception() is not None:
                log.error("Фоновая задача упала: %r", t.exception())

        task.add_done_callback(_done)
