"""Ядро мониторинга задержек.

Работает ТОЛЬКО с метаданными: ID чата, ID сообщения, время, ID отправителя,
признак «исходящее». Текст сообщений сюда не передаётся вообще.
"""
from __future__ import annotations

import json
import logging
import time
from collections import defaultdict
from zoneinfo import ZoneInfo

from .breaks import BreakService
from .db import Database
from .features import emoji_lists, feature_on, master_on
from .links import message_link, mention
from .notifier import Notifier
from .statuses import classify
from .worktime import is_work_time, parse_date, parse_hhmm, work_seconds_between

log = logging.getLogger("monitor")

MAX_LINKS_PER_ALERT = 30
CACHE_TTL = 30


class Monitor:
    def __init__(self, db: Database, tz: ZoneInfo, notifier: Notifier, breaks: BreakService, clock=time.time):
        self.db = db
        self.tz = tz
        self.notifier = notifier
        self.breaks = breaks
        self.clock = clock
        self._chat_ids: set[int] = set()
        self._emp_ids: set[int] = set()
        self._push_ids: set[int] = set()
        self._forums: set[int] = set()
        self._cache_at = 0.0
        self._fail_until = 0.0

    # ------------------------------------------------------------ кеш
    def reload(self) -> None:
        self._chat_ids = self.db.chat_ids()
        self._forums = self.db.forum_ids()
        self._emp_ids = self.db.employee_user_ids()
        self._push_ids = self.db.account_user_ids()
        self._cache_at = self.clock()

    def _fresh(self) -> None:
        if self.clock() - self._cache_at > CACHE_TTL:
            self.reload()

    def is_monitored(self, chat_id: int) -> bool:
        self._fresh()
        return chat_id in self._chat_ids

    def is_forum(self, chat_id: int) -> bool:
        self._fresh()
        return chat_id in self._forums

    # ------------------------------------------------------------ настройки
    def window(self):
        return parse_hhmm(self.db.get("work_start")), parse_hhmm(self.db.get("work_end"))

    def track_from_ts(self) -> int:
        try:
            d = parse_date(self.db.get("track_from"))
        except ValueError:
            return 0
        return int(d.replace(tzinfo=self.tz).timestamp())

    # ------------------------------------------------------------ события
    async def handle_message(self, chat_id: int, topic_id: int, msg_id: int, ts: int, sender_id: int | None,
                             out: bool, service: bool = False, bet: bool = False,
                             proof: bool = False, requisites: bool = False) -> None:
        """bet=True — сообщение-кружок (ставка посредника): считаем по ТИПУ вложения, текст не читаем.
        topic_id: id топика форума (лид = топик) либо 0 для обычного чата."""
        self._fresh()
        if not master_on(self.db) or chat_id not in self._chat_ids or service:
            return
        if self.db.is_ignored(chat_id, msg_id):      # сообщение нашей же рассылки
            return
        if sender_id is not None and sender_id in self._push_ids:
            return                                   # наш аккаунт Push — не клиент и не сотрудник
        state = self.db.state(chat_id, topic_id)
        self.db.touch_activity(chat_id, topic_id, ts)

        if requisites:                               # посредник отправил реквизиты: ждём чек/ответ сотрудника
            if feature_on(self.db, "pay_enabled"):
                self.db.start_payment(chat_id, topic_id, ts)
            return

        if out or (sender_id is not None and sender_id in self._emp_ids):
            self.db.done_payment(chat_id, topic_id)   # сотрудник написал — напоминать клиенту не нужно
            if state is not None and state["last_lead_ts"] and ts < state["last_lead_ts"]:
                return                                 # ответ старше последнего сообщения лида
            self._record_reply(chat_id, topic_id, state, sender_id, out, ts)
            self.db.set_idle(chat_id, topic_id, ts)
            await self._resolve_replied(chat_id, topic_id)
            return

        if proof:
            self.db.done_payment(chat_id, topic_id)   # клиент прислал фото/PDF
        if ts < self.track_from_ts():
            return
        if state is not None and state["last_reply_ts"] and ts < state["last_reply_ts"]:
            return                                     # на это сообщение уже ответили
        if feature_on(self.db, "leads_enabled"):
            self.db.touch_lead(chat_id, topic_id, sender_id, msg_id, ts)
            self.db.bump_count(chat_id, topic_id, bet)
        restart = state is not None and (
            state["status"] == "alerted"
            or (state["status"] == "pending" and (state["pending_since"] or 0) < self.db.get_int("alerts_from")))
        self.db.set_pending(chat_id, topic_id, msg_id, ts, restart_streak=restart)

    def _record_reply(self, chat_id: int, topic_id: int, state, sender_id: int | None, out: bool, ts: int) -> None:
        """Для отчётов: сколько сотрудник заставил лида ждать (рабочее время). Только ID и время."""
        if state is None or state["status"] not in ("pending", "alerted") or not state["pending_since"]:
            return
        chat = self.db.chat(chat_id)
        if chat is None:
            return
        fid = chat["folder_id"]
        try:
            waited = self.elapsed(fid, int(state["pending_since"]), int(ts))
            self.db.record_reply(chat_id, topic_id, fid, None if out else sender_id,
                                 int(state["pending_since"]), int(ts), waited)
        except Exception:
            log.exception("Не удалось записать ответ для отчёта")

    async def reconcile_top(self, chat_id: int, topic_id: int, msg_id: int, ts: int, sender_id: int | None,
                            out: bool, service: bool) -> None:
        """Сверка по последнему сообщению нити (страховка от пропущенных событий)."""
        self._fresh()
        if not master_on(self.db) or chat_id not in self._chat_ids or service:
            return
        if sender_id is not None and sender_id in self._push_ids:
            return
        state = self.db.state(chat_id, topic_id)
        is_employee = out or (sender_id is not None and sender_id in self._emp_ids)
        if is_employee:
            if state is not None and state["status"] in ("pending", "alerted"):
                await self.handle_message(chat_id, topic_id, msg_id, ts, sender_id, out)
            else:
                self.db.touch_activity(chat_id, topic_id, ts)
            return
        if state is not None and state["last_lead_msg_id"] == msg_id:
            return                                     # это сообщение уже учтено
        await self.handle_message(chat_id, topic_id, msg_id, ts, sender_id, out)

    # ------------------------------------------------------------ закрытие алертов
    async def _resolve_replied(self, chat_id: int, topic_id: int) -> None:
        key = [chat_id, topic_id]
        for alert in self.db.open_alerts():
            remaining = json.loads(alert["remaining"])
            if key not in remaining:
                continue
            remaining.remove(key)
            if remaining:
                self.db.set_alert_remaining(alert["id"], remaining)
            else:
                await self._close_alert(alert, "ответил менеджер")

    async def _close_alert(self, alert, by: str) -> None:
        self.db.close_alert(alert["id"], "done", by, int(self.clock()))
        if alert["group_chat_id"] and alert["message_id"]:
            try:
                await self.notifier.edit_alert(
                    alert["group_chat_id"], alert["message_id"], f"{alert['text']}\n\n✅ Выполнено ({by})"
                )
            except Exception as exc:
                log.warning("Не удалось обновить сообщение в «Проёб»: %s", exc)

    async def press_done(self, alert_id: int, who: str) -> bool:
        alert = self.db.alert(alert_id)
        if alert is None or alert["status"] != "open":
            return False
        await self._close_alert(alert, f"отметил {who}")
        return True

    # ------------------------------------------------------------ таймер
    def elapsed(self, folder_id: int, since: int, now: int, pauses=None) -> int:
        ws, we = self.window()
        total = work_seconds_between(since, now, self.tz, ws, we)
        if pauses is None:
            pauses = self.db.pauses(folder_id, since)
        paused = 0
        for p in pauses:
            lo = max(p["start_ts"], since)
            hi = min(p["end_ts"] if p["end_ts"] is not None else now, now)
            if hi > lo:
                paused += work_seconds_between(lo, hi, self.tz, ws, we)
        return max(0, total - paused)

    async def tick(self, now: int | None = None) -> int:
        """Проверить все ожидающие чаты. Возвращает число отправленных алертов."""
        now = int(now if now is not None else self.clock())
        if not master_on(self.db):
            return 0
        self.breaks.recompute_pauses(now)
        if not feature_on(self.db, "delay_enabled"):
            return 0
        ws, we = self.window()
        if not is_work_time(now, self.tz, ws, we):
            return 0
        if self.clock() < self._fail_until:
            return 0

        limit = max(1, self.db.get_int("limit_minutes")) * 60
        from_first = self.db.get("timer_from") != "last"
        alerts_from = self.db.get_int("alerts_from")
        push_e, skip_e = emoji_lists(self.db)
        pauses: dict[int, list] = {}
        due: dict[int, list] = defaultdict(list)
        off = {f["id"] for f in self.db.folders() if not f["delay_on"]}      # папки без контроля
        for row in self.db.pending_rows():
            ref = row["pending_since"] if from_first else row["last_lead_ts"]
            if ref is None or row["last_lead_msg_id"] is None:
                continue
            if ref < alerts_from:                       # до включения контроля — не считаем (без «потока» старых)
                continue
            if classify(row["label"] or "", push_e, skip_e) == "skip":   # ⚡ / 💊 не учитываем
                continue
            fid = row["folder_id"]
            if fid in off:
                continue
            if fid not in pauses:
                pauses[fid] = self.db.pauses(fid, 0)
            if self.elapsed(fid, ref, now, pauses[fid]) >= limit:
                due[fid].append(row)

        sent = 0
        for fid, rows in due.items():
            rows.sort(key=lambda r: r["pending_since"] or 0)
            for i in range(0, len(rows), MAX_LINKS_PER_ALERT):
                if await self._send_alert(fid, rows[i:i + MAX_LINKS_PER_ALERT], now):
                    sent += 1
                else:
                    return sent
        return sent

    async def _send_alert(self, folder_id: int, rows: list, now: int) -> bool:
        folder = self.db.folder(folder_id)
        if folder is None:
            return True
        lines = [message_link(r["chat_id"], r["last_lead_msg_id"], r["topic_id"]) for r in rows]
        who = " ".join(mention(e["username"]) for e in self.db.employees(folder_id))
        footer = f"{who} {folder['name']} задержка".strip()
        text = "\n".join(lines + [footer])
        threads = [(r["chat_id"], r["topic_id"]) for r in rows]

        alert_id = self.db.create_alert(folder_id, [list(t) for t in threads], text, now)
        try:
            gid, mid = await self.notifier.send_alert(text, alert_id)
        except Exception as exc:
            self.db.delete_alert(alert_id)
            self._fail_until = self.clock() + 120
            log.error("Алерт не отправлен в «Проёб»: %s", exc)
            await self.notifier.notify_admin(
                f"⚠️ Не удаётся отправить уведомление в группу «Проёб»: {exc}\n"
                "Проверьте, что бот добавлен в группу и ID группы указан верно.",
                key="alert-fail", min_interval=1800,
            )
            return False
        self.db.set_alert_message(alert_id, gid, mid)
        self.db.mark_alerted(threads, alert_id)
        return True


    async def watch_bots(self, now: int | None = None) -> int:
        """Контроль «посредника»: после нового чата должны прийти N автоставок. Если нить затихла раньше —
        предупреждение только администратору в личку (в группы ничего не пишем)."""
        now = int(now if now is not None else self.clock())
        if not feature_on(self.db, "botwatch_enabled"):
            return 0
        need = self.db.get_int("botwatch_count")             # сколько кружков-ставок должно прийти
        wait = max(1, self.db.get_int("botwatch_wait")) * 60
        rows = self.db.silent_bot_threads(self.db.get_int("botwatch_from"), need, now - wait)
        sent = 0
        for r in rows:
            if r["topic_id"] and r["topic_id"] > 1:
                link = message_link(r["chat_id"], r["last_msg_id"], r["topic_id"])
            else:
                link = message_link(r["chat_id"], r["last_msg_id"])
            chat = self.db.chat(r["chat_id"])
            folder = self.db.folder(chat["folder_id"]) if chat else None
            if folder is not None and not folder["delay_on"]:
                self.db.mark_watch_warned(r["chat_id"], r["topic_id"])
                continue
            who = " ".join(mention(e["username"]) for e in self.db.employees(folder["id"])) if folder else ""
            text = f"{link}\n{who} {folder['name'] if folder else ''} ставка".replace("  ", " ").replace("\n ", "\n")
            try:
                await self.notifier.send_group_text(text)
            except Exception as exc:
                log.warning("Автоставка: не удалось написать в «Проёб»: %s", exc)
                await self.notifier.notify_admin(
                    f"⚠️ Автоставки не пришли ({r['bet_count']} из {need}), а в «Проёб» написать не удалось.\n{link}",
                    key="botwatch-fail", min_interval=600)
            self.db.mark_watch_warned(r["chat_id"], r["topic_id"])
            sent += 1
        return sent
