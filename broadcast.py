"""Ручной Push по горячим лидам (🔥) с защитой от бана (Anti-Flood).

Push уходит ТОЛЬКО по команде администратора (автоматического расписания нет).
Получатели: лиды, чьё имя начинается с «push-эмодзи» (по умолчанию 🔥), не из «исключённых»
(⚡ 💊), появившиеся в выбранный период; количество ограничено настройкой «Количество».
Между отправками — случайная пауза 2–3 секунды.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

from datetime import datetime, time as dtime, timedelta

from .db import Database
from .features import emoji_lists, feature_on
from .notifier import Notifier
from .statuses import classify
from .userbot import SendAbort, SendSkip, Userbot
from .worktime import parse_date

log = logging.getLogger("broadcast")

MIN_DELAY = 2.0
MAX_DELAY = 3.0
PROGRESS_EVERY = 20

Progress = Callable[[int, int, int], Awaitable[None]]


class Broadcaster:
    def __init__(self, db: Database, bot, userbot: Userbot, notifier: Notifier, tz: ZoneInfo, sleep=asyncio.sleep):
        self.db = db
        self.bot = bot
        self.userbot = userbot
        self.notifier = notifier
        self.tz = tz
        self._sleep = sleep
        self.running = False
        self._stop = False
        self.current_id: int | None = None

    # ------------------------------------------------------------ аудитория
    def _period(self) -> tuple[int, int]:
        """(с, по) в unix-времени по настройкам; «по» включает весь последний день."""
        try:
            d_from = parse_date(self.db.get("push_from") or self.db.get("track_from"))
            lo = int(d_from.replace(tzinfo=self.tz).timestamp())
        except ValueError:
            lo = 0
        hi = 2**31
        raw_to = self.db.get("push_to")
        if raw_to:
            try:
                d_to = parse_date(raw_to)
                hi = int(datetime.combine(d_to.date() + timedelta(days=1), dtime(0, 0), tzinfo=self.tz).timestamp()) - 1
            except ValueError:
                pass
        return lo, hi

    def audience(self, mode: str, now: int | None = None) -> dict:
        """Кому уйдёт Push: {'threads': [(chat, topic)], 'matched', 'unreachable', 'limit', 'cooldown', 'repeat'}.

        Лид = топик (или личный чат). Фильтры: эмодзи-статус (🔥 да, ⚡ 💊 нет), период появления,
        «недавно писали» (пауза N минут) и «уже получал Push» (повтор не чаще N дней), затем лимит.
        """
        now = int(time.time()) if now is None else now
        push_e, skip_e = emoji_lists(self.db)
        lo, hi = self._period()
        rows = [r for r in self.db.leads_with_labels()
                if classify(r["label"], push_e, skip_e) == "push" and lo <= r["first_seen"] <= hi]
        matched = len(rows)

        idle = max(0, self.db.get_int("push_idle_minutes"))
        cooled = 0
        if idle:
            border = now - idle * 60
            kept = [r for r in rows if max(r["last_seen"], r["last_activity_ts"] or 0) <= border]
            cooled = len(rows) - len(kept)
            rows = kept

        repeat = max(0, self.db.get_int("push_repeat_days"))
        repeated = 0
        if repeat:
            border = now - repeat * 86400
            kept = [r for r in rows if not r["last_push_ts"] or r["last_push_ts"] <= border]
            repeated = len(rows) - len(kept)
            rows = kept

        # сначала те, кому ещё не слали, затем давно не получавшие; внутри — свежие первыми
        rows.sort(key=lambda r: (r["last_push_ts"] is not None, r["last_push_ts"] or 0, -r["last_seen"]))
        unreachable = 0
        if mode == "bot":
            reachable = self.db.reachable_bot_users()
            kept = [r for r in rows if r["chat_id"] < 0 or r["chat_id"] in reachable]
            unreachable = len(rows) - len(kept)
            rows = kept
        limit = max(0, self.db.get_int("push_limit"))
        chosen = rows[:limit] if limit else rows
        return {"threads": [(r["chat_id"], r["topic_id"]) for r in chosen], "matched": matched,
                "unreachable": unreachable, "limit": limit, "cooled": cooled, "repeated": repeated,
                "ready": len(rows)}

    def estimate_minutes(self, n: int) -> int:
        return max(1, round(n * (MIN_DELAY + MAX_DELAY) / 2 / 60))

    def stop(self) -> None:
        self._stop = True

    # ------------------------------------------------------------ запуск
    async def run(self, text: str, mode: str, trigger: str = "manual",
                  progress: Progress | None = None) -> dict:
        if not feature_on(self.db, "push_enabled"):
            return {"status": "disabled", "sent": 0, "failed": 0, "total": 0}
        if self.running:
            return {"status": "busy", "sent": 0, "failed": 0, "total": 0}
        text = text.strip()
        if not text:
            return {"status": "empty", "sent": 0, "failed": 0, "total": 0}

        self.running = True
        self._stop = False
        aud = self.audience(mode)
        recipients, unreachable = aud["threads"], aud["unreachable"]
        bid = self.db.start_broadcast(mode, trigger, len(recipients), int(time.time()))
        self.current_id = bid
        sent = failed = 0
        status = "done"
        reason = ""
        try:
            for i, (chat_id, topic_id) in enumerate(recipients):
                if self._stop:
                    status = "stopped"
                    break
                try:
                    await self._send_one(chat_id, topic_id, text, mode)
                    self.db.mark_pushed(chat_id, topic_id, int(time.time()))
                    sent += 1
                except SendSkip:
                    failed += 1
                except SendAbort as exc:
                    status, reason = "aborted", str(exc)
                    break
                except Exception as exc:
                    failed += 1
                    log.warning("Рассылка: ошибка отправки %s/%s: %s", chat_id, topic_id, type(exc).__name__)

                if (i + 1) % PROGRESS_EVERY == 0:
                    self.db.update_broadcast(bid, sent, failed)
                    if progress:
                        try:
                            await progress(sent, failed, len(recipients))
                        except Exception:
                            pass
                if i + 1 < len(recipients):
                    await self._sleep(random.uniform(MIN_DELAY, MAX_DELAY))
        except asyncio.CancelledError:
            status = "interrupted"
            raise
        finally:
            self.db.update_broadcast(bid, sent, failed)
            self.db.finish_broadcast(bid, status, int(time.time()))
            self.running = False
            self.current_id = None

        result = {"status": status, "sent": sent, "failed": failed, "total": len(recipients),
                  "unreachable": unreachable, "reason": reason, "mode": mode}
        return result

    async def _send_one(self, chat_id: int, topic_id: int, text: str, mode: str) -> None:
        if mode == "userbot":
            msg_id = await self.userbot.send_text(chat_id, topic_id, text)
            self.db.ignore_msg(chat_id, msg_id, int(time.time()))
            return
        await self._send_via_bot(chat_id, topic_id, text)

    async def _send_via_bot(self, chat_id: int, topic_id: int, text: str) -> None:
        from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter

        for attempt in (1, 2):
            try:
                msg = await self.bot.send_message(chat_id, text, message_thread_id=(topic_id if topic_id > 1 else None))
                self.db.ignore_msg(chat_id, int(msg.message_id), int(time.time()))
                return
            except TelegramRetryAfter as exc:
                if attempt == 2:
                    raise SendAbort(f"Telegram просит подождать {exc.retry_after} с") from exc
                await self._sleep(float(exc.retry_after) + 1)
            except TelegramForbiddenError as exc:
                if chat_id > 0:
                    self.db.mark_blocked(chat_id)
                raise SendSkip("нет доступа") from exc
            except TelegramBadRequest as exc:
                raise SendSkip(str(exc)[:60]) from exc

    # ------------------------------------------------------------ отчёты
    @staticmethod
    def report(result: dict) -> str:
        names = {"done": "✅ Рассылка завершена", "stopped": "⏹ Рассылка остановлена",
                 "aborted": "⛔ Рассылка прервана", "busy": "⏳ Другая рассылка уже идёт",
                 "empty": "⚠️ Пустой текст рассылки", "disabled": "⛔ Push выключен в разделе «Функции»", "interrupted": "⚠️ Рассылка прервана перезапуском"}
        lines = [names.get(result["status"], result["status"])]
        if result["status"] in ("busy", "empty", "disabled"):
            return lines[0]
        lines.append(f"Отправлено: {result['sent']} из {result['total']}, ошибок: {result['failed']}")
        if result.get("unreachable"):
            lines.append(f"Недоступно через бота (не нажали «Старт»): {result['unreachable']}")
        if result.get("reason"):
            lines.append(f"Причина: {result['reason']}")
        return "\n".join(lines)
