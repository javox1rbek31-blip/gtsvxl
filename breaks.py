"""Контроль перерывов (30 минут, 3 раза в день) и паузы таймера папок.

Текст читается ТОЛЬКО в группе «Проёб» и только для поиска ключевых слов
(«атайду»/«перерыв» — начало, «тут» — возвращение). Текст нигде не сохраняется.
"""
from __future__ import annotations

import logging
import re
import time
from zoneinfo import ZoneInfo

from .db import Database
from .features import feature_on
from .notifier import Notifier
from .worktime import local_day_start

log = logging.getLogger("breaks")

_START = re.compile(r"(?<!\w)(атайду|перерыв)(?!\w)", re.IGNORECASE)
_BACK = re.compile(r"^\W*тут\W*$", re.IGNORECASE)


def detect(text: str | None) -> str | None:
    """'start' | 'end' | None."""
    if not text:
        return None
    if _BACK.match(text):
        return "end"
    if _START.search(text):
        return "start"
    return None


class BreakService:
    def __init__(self, db: Database, tz: ZoneInfo, notifier: Notifier, clock=time.time):
        self.db = db
        self.tz = tz
        self.notifier = notifier
        self.clock = clock

    # ------------------------------------------------------------ вспомогательное
    @property
    def duration(self) -> int:
        return max(1, self.db.get_int("break_minutes")) * 60

    @property
    def per_day(self) -> int:
        return max(1, self.db.get_int("breaks_per_day"))

    def _eff_end(self, br) -> int:
        """Момент, когда перерыв перестаёт ставить таймер на паузу."""
        limit = br["start_ts"] + self.duration
        return min(br["end_ts"], limit) if br["end_ts"] is not None else limit

    @staticmethod
    def _same_person(br, emp) -> bool:
        if br["user_id"] is not None and emp["user_id"] is not None and br["user_id"] == emp["user_id"]:
            return True
        return bool(br["username"]) and br["username"] == emp["username"]

    def learn(self, user_id: int | None, username: str | None) -> bool:
        """Запомнить числовой ID сотрудника по его @username. True, если что-то изменилось."""
        if not user_id or not username:
            return False
        uname = username.lower()
        row = self.db.one("SELECT 1 FROM employees WHERE username=? AND (user_id IS NULL OR user_id<>?)", (uname, user_id))
        if row is None:
            return False
        self.db.set_employee_user_id(uname, user_id)
        return True

    # ------------------------------------------------------------ события из группы
    async def handle_text(self, user_id: int | None, username: str | None, text: str | None, ts: int) -> str | None:
        """Возвращает: started | already | limit | ended | ended_late | noop | unknown | None."""
        changed = self.learn(user_id, username)
        kind = detect(text)
        if kind is None or not feature_on(self.db, "breaks_enabled"):
            return None

        folders = self.db.employee_folder_ids(user_id, username)
        if not folders:
            return "unknown"   # не сотрудник из папок — игнорируем
        uname = (username or "").lower() or self._employee_username(user_id)

        if kind == "start":
            cur = self.db.open_break(user_id, uname)
            if cur is not None:
                if ts < cur["start_ts"] + self.duration:
                    return "already"
                self.db.close_break(cur["id"], cur["start_ts"] + self.duration)  # забыл написать «тут»
            since = local_day_start(ts, self.tz)
            if self.db.breaks_since(user_id, uname, since) >= self.per_day:
                return "limit"
            self.db.add_break(user_id, uname, ts)
            self.recompute_pauses(ts)
            return "started"

        cur = self.db.open_break(user_id, uname)
        if cur is None:
            return "noop"
        late = ts > cur["start_ts"] + self.duration
        self.db.close_break(cur["id"], ts)
        self.recompute_pauses(ts)
        return "ended_late" if late else "ended"

    def _employee_username(self, user_id: int | None) -> str | None:
        if user_id is None:
            return None
        row = self.db.one("SELECT username FROM employees WHERE user_id=? LIMIT 1", (user_id,))
        return row["username"] if row else None

    # ------------------------------------------------------------ паузы папок
    def recompute_pauses(self, now: int) -> None:
        """Папка стоит на паузе, пока ВСЕ её сотрудники на перерыве (иначе ответить может другой)."""
        breaks = self.db.recent_breaks(now - 2 * 86400)
        for folder in self.db.folders():
            emps = self.db.employees(folder["id"])
            open_pause = self.db.open_pause(folder["id"])
            if not emps:
                if open_pause is not None:
                    self.db.end_pause(folder["id"], now)
                continue

            current: list = []       # текущий перерыв каждого сотрудника (если есть)
            latest: list = []        # последний перерыв каждого сотрудника, начавшийся до now
            for emp in emps:
                mine = [b for b in breaks if self._same_person(b, emp) and b["start_ts"] <= now]
                mine.sort(key=lambda b: b["start_ts"])
                last = mine[-1] if mine else None
                latest.append(last)
                current.append(last if last is not None and now < self._eff_end(last) else None)

            should = all(c is not None for c in current)
            if should and open_pause is None:
                self.db.start_pause(folder["id"], min(now, max(c["start_ts"] for c in current)))
            elif not should and open_pause is not None:
                ends = [self._eff_end(b) for b in latest if b is not None]
                end_ts = min([now] + ends) if ends else now
                self.db.end_pause(folder["id"], max(end_ts, open_pause["start_ts"]))

    # ------------------------------------------------------------ периодическая проверка
    async def tick(self, now: int | None = None) -> None:
        now = int(now if now is not None else self.clock())
        self.recompute_pauses(now)
        if not feature_on(self.db, "breaks_enabled"):
            return
        for br in self.db.all_open_breaks():
            if br["reminded"]:
                continue
            due = br["start_ts"] + self.duration
            if now < due:
                continue
            who = br["username"] or self._employee_username(br["user_id"])
            label = f"@{who}" if who else f"Сотрудник (ID {br['user_id']})"
            try:
                await self.notifier.send_group_text(f"{label}, время перерыва вышло!")
                self.db.mark_break_reminded(br["id"])
            except Exception as exc:
                log.warning("Напоминание о перерыве не отправлено: %s", exc)
                if now - due > 600:          # не зацикливаемся, если группа недоступна
                    self.db.mark_break_reminded(br["id"])
