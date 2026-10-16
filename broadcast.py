"""Ручной Push по горячим лидам (🔥) с защитой от бана (Anti-Flood).

Push уходит ТОЛЬКО по команде администратора (автоматического расписания нет).
Получатели: лиды, чьё имя начинается с «push-эмодзи» (по умолчанию 🔥), не из «исключённых»
(⚡ 💊), появившиеся в выбранный период; количество ограничено настройкой «Количество».
Между отправками — случайная пауза 2–3 секунды.
"""
from __future__ import annotations

import asyncio
import json
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
from .worktime import local_day_start, parse_date

log = logging.getLogger("broadcast")

MIN_DELAY = 2.0
MAX_DELAY = 3.0
PROGRESS_EVERY = 5
SPEEDS = {"fast": (1.0, 2.0), "normal": (2.0, 3.0), "slow": (3.0, 5.0)}

Progress = Callable[[int, int, int], Awaitable[None]]


def split_quota(avail: dict[int, int], limit: int) -> dict[int, int]:
    """Лимит делится ПОРОВНУ между папками. Если у папки лидов меньше её доли — остаток НЕ передаётся другим.
    avail включает все выбранные папки (в том числе пустые). limit=0 — без ограничения."""
    if limit <= 0:
        return dict(avail)
    ids = sorted(avail)
    if not ids:
        return {}
    base, extra = divmod(limit, len(ids))
    return {f: min(avail[f], base + (1 if i < extra else 0)) for i, f in enumerate(ids)}


def _hm(raw: str) -> int | None:
    try:
        h_, m_ = raw.strip().split(":")
        v = int(h_) * 60 + int(m_)
        return v if 0 <= v < 1440 else None
    except (ValueError, AttributeError):
        return None


def in_quiet(frm: str, to: str, minute_of_day: int) -> bool:
    """Попадает ли время в «тихое окно» frm–to (окно может переходить через полночь). Пусто — окна нет."""
    a, b = _hm(frm or ""), _hm(to or "")
    if a is None or b is None or a == b:
        return False
    return a <= minute_of_day < b if a < b else (minute_of_day >= a or minute_of_day < b)


def split_with_own(avail: dict[int, int], own: dict[int, int], limit: int) -> dict[int, int]:
    """Папки со своим числом (own>0) берут его; остальные делят общий лимит поровну (0 = все)."""
    out = {f: min(avail[f], own[f]) for f in avail if own.get(f, 0) > 0}
    rest = {f: avail[f] for f in avail if f not in out}
    out.update(split_quota(rest, limit))
    return out


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
        self.pool = None                                  # AccountPool (аккаунты Push по папкам)

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

    # ------------------------------------------------------------ содержимое Push (текст + медиа)
    def media(self) -> dict | None:
        raw = self.db.get("push_media")
        if not raw:
            return None
        try:
            m = json.loads(raw)
        except ValueError:
            return None
        return m if isinstance(m, dict) and m.get("kind") else None

    def has_content(self) -> bool:
        return bool(self.db.get("push_text").strip() or self.media())

    # ------------------------------------------------------------ аудитория
    def selected_folders(self) -> set[int]:
        raw = self.db.get("push_folders")
        return {int(x) for x in raw.replace(";", ",").split(",") if x.strip().isdigit()}

    def audience(self, mode: str, now: int | None = None) -> dict:
        """Кому уйдёт Push. Порядок: новые лиды → вчерашние → за прошлую неделю → старые.
        Лимит делится ПОРОВНУ между выбранными папками. Фильтры: статус-эмодзи, период, папки,
        «новый лид» (ждём автоставки), «недавно писали», «уже получал», режим дня/вечера."""
        now = int(time.time()) if now is None else now
        push_e, skip_e = emoji_lists(self.db)
        lo, hi = self._period()
        cmap = self.db.chat_folder_map()
        folders = {f["id"]: f for f in self.db.folders()}
        sel = self.selected_folders()
        allowed = {fid for fid, f in folders.items() if f["push_on"] and (not sel or fid in sel)}
        today = local_day_start(now, self.tz)
        quiet: list[int] = []
        t_local = datetime.fromtimestamp(now, self.tz)
        mod = t_local.hour * 60 + t_local.minute
        for fid in sorted(allowed):                       # «тихое время»: у папки своё, иначе общее
            f = folders[fid]
            frm, to = (f["quiet_from"], f["quiet_to"]) if f["quiet_from"] and f["quiet_to"] else (
                self.db.get("push_quiet_from"), self.db.get("push_quiet_to"))
            if in_quiet(frm, to, mod):
                quiet.append(fid)
        allowed -= set(quiet)
        no_account: list[int] = []
        if mode == "userbot" and self.pool is not None:
            # Push идёт ТОЛЬКО через аккаунт, назначенный папке; основной аккаунт (задержки) не используется
            no_account = sorted(fid for fid in allowed if not folders[fid]["account_id"])
            allowed = {fid for fid in allowed if folders[fid]["account_id"]}
        rows = [r for r in self.db.leads_with_labels()
                if classify(r["label"], push_e, skip_e) == "push" and lo <= r["first_seen"] <= hi
                and cmap.get(r["chat_id"]) in allowed]
        matched = len(rows)

        fresh_n = max(0, self.db.get_int("push_newlead_minutes"))
        fresh = 0
        if fresh_n:                                       # совсем новый лид: автоставки ещё идут — пока не трогаем
            border = now - fresh_n * 60
            kept = [r for r in rows if r["first_seen"] <= border]
            fresh = len(rows) - len(kept)
            rows = kept

        idle = max(0, self.db.get_int("push_idle_minutes"))
        cooled = 0
        if idle:
            border = now - idle * 60
            kept = [r for r in rows if max(r["last_seen"], r["last_activity_ts"] or 0) <= border]
            cooled = len(rows) - len(kept)
            rows = kept

        repeat = max(0, self.db.get_int("push_repeat_minutes"))
        repeated = 0
        if repeat:
            border = now - repeat * 60
            kept = [r for r in rows if not r["last_push_ts"] or r["last_push_ts"] <= border]
            repeated = len(rows) - len(kept)
            rows = kept

        yesterday = today - 86400
        week_ago = yesterday - 7 * 86400
        slot = self.db.get("push_slot")

        def active_today(r) -> bool:
            return max(r["last_seen"], r["last_activity_ts"] or 0) >= today

        slot_skipped = 0
        if slot == "evening":                             # вечером — только те, с кем сегодня был контакт
            kept = [r for r in rows if active_today(r)]
            slot_skipped = len(rows) - len(kept)
            rows = kept

        def bucket(r) -> int:                              # сегодня → вчера → прошлая неделя → старые
            t = r["first_seen"]
            return 0 if t >= today else 1 if t >= yesterday else 2 if t >= week_ago else 3

        def key(r):
            base = (bucket(r), r["last_push_ts"] is not None, r["last_push_ts"] or 0, -r["last_seen"])
            return ((0 if active_today(r) else 1),) + base if slot == "day" else base

        rows.sort(key=key)
        unreachable = 0
        if mode == "bot":
            reachable = self.db.reachable_bot_users()
            kept = [r for r in rows if r["chat_id"] < 0 or r["chat_id"] in reachable]
            unreachable = len(rows) - len(kept)
            rows = kept
        limit = max(0, self.db.get_int("push_limit"))
        by_f: dict[int, list] = {fid: [] for fid in allowed}
        for r in rows:
            by_f.setdefault(cmap[r["chat_id"]], []).append(r)
        own = {fid: int(folders[fid]["push_limit"] or 0) for fid in by_f}
        quota = split_with_own({fid: len(v) for fid, v in by_f.items()}, own, limit)
        chosen = []
        for fid, v in by_f.items():
            chosen += [(r, fid) for r in v[:quota[fid]]]
        chosen.sort(key=lambda t: key(t[0]))
        items = [(r["chat_id"], r["topic_id"], fid) for r, fid in chosen]
        return {"threads": [(c, t) for c, t, _ in items], "items": items,
                "by_folder": {fid: quota[fid] for fid in quota if quota[fid]}, "no_account": no_account, "quiet": quiet,
                "matched": matched, "unreachable": unreachable, "limit": limit, "cooled": cooled,
                "repeated": repeated, "fresh": fresh, "slot_skipped": slot_skipped, "ready": len(rows)}

    def delays(self) -> tuple[float, float]:
        return SPEEDS.get(self.db.get("push_speed"), SPEEDS["normal"])

    def estimate_minutes(self, n: int) -> int:
        lo, hi = self.delays()
        return max(1, round(n * (lo + hi + 1.0) / 2 / 60))

    def stop(self) -> None:
        self._stop = True

    # ------------------------------------------------------------ запуск
    def sender_name(self, folder_id: int) -> str:
        folder = self.db.folder(folder_id)
        acc = self.db.account(folder["account_id"]) if folder and folder["account_id"] else None
        return acc["name"] if acc else "основной"

    async def run(self, text: str, mode: str, trigger: str = "manual",
                  progress: Progress | None = None) -> dict:
        media = self.media()
        if not feature_on(self.db, "push_enabled"):
            return {"status": "disabled", "sent": 0, "failed": 0, "total": 0}
        if self.running:
            return {"status": "busy", "sent": 0, "failed": 0, "total": 0}
        text = text.strip()
        if not text and not media:
            return {"status": "empty", "sent": 0, "failed": 0, "total": 0}

        self.running = True
        self._stop = False
        aud = self.audience(mode)
        items = aud["items"]
        unreachable = aud["unreachable"]
        bid = self.db.start_broadcast(mode, trigger, len(items), int(time.time()))
        self.current_id = bid
        stat = {"sent": 0, "failed": 0, "done": 0, "aborted": ""}
        reasons: dict[str, int] = {}
        per_folder: dict[int, list[int]] = {}

        def fail(reason: str, fid: int) -> None:
            stat["failed"] += 1
            reasons[reason] = reasons.get(reason, 0) + 1
            per_folder.setdefault(fid, [0, 0])[1] += 1

        async def tick_progress() -> None:
            stat["done"] += 1
            if stat["done"] % PROGRESS_EVERY == 0:
                self.db.update_broadcast(bid, stat["sent"], stat["failed"])
                if progress:
                    try:
                        await progress(stat["sent"], stat["failed"], len(items))
                    except Exception:
                        pass

        # аккаунты работают ПАРАЛЛЕЛЬНО (у каждого своя пауза), внутри аккаунта — строго по очереди
        groups: dict[object, list] = {}
        for chat_id, topic_id, fid in items:
            if mode == "bot":
                key = "bot"
            else:
                folder = self.db.folder(fid)
                key = (folder["account_id"] if folder and folder["account_id"] else 0)
            groups.setdefault(key, []).append((chat_id, topic_id, fid))

        async def worker(key, group) -> None:
            sender = None
            if mode == "userbot":
                sender = self.pool.sender(key or None) if self.pool is not None else self.userbot
                if sender is None or sender.state != "online":
                    name = self.sender_name(group[0][2])
                    for _, _, fid in group:
                        fail(f"аккаунт «{name}» не в сети", fid)
                        await tick_progress()
                    return
            for i, (chat_id, topic_id, fid) in enumerate(group):
                if self._stop:
                    return
                try:
                    await self._send_one(chat_id, topic_id, text, mode, media, sender)
                    self.db.mark_pushed(chat_id, topic_id, int(time.time()))
                    stat["sent"] += 1
                    per_folder.setdefault(fid, [0, 0])[0] += 1
                except SendSkip as exc:
                    fail(str(exc) or "SendSkip", fid)
                except SendAbort as exc:
                    stat["aborted"] = str(exc)
                    for _, _, f2 in group[i:]:
                        fail(f"остановлено: {str(exc)[:60]}", f2)
                        await tick_progress()
                    return
                except Exception as exc:
                    fail(f"{type(exc).__name__}: {str(exc)[:80]}", fid)
                    log.warning("Рассылка: ошибка отправки %s/%s: %s", chat_id, topic_id, type(exc).__name__)
                await tick_progress()
                if i + 1 < len(group):
                    await self._sleep(random.uniform(*self.delays()))

        status = "done"
        try:
            await asyncio.gather(*(worker(k, g) for k, g in groups.items()))
            if self._stop:
                status = "stopped"
            elif stat["aborted"] and not stat["sent"]:
                status = "aborted"
        except asyncio.CancelledError:
            status = "interrupted"
            raise
        finally:
            self.db.update_broadcast(bid, stat["sent"], stat["failed"])
            self.db.finish_broadcast(bid, status, int(time.time()))
            self.running = False
            self.current_id = None

        folder_lines = []
        for fid, (ok, bad) in per_folder.items():
            f = self.db.folder(fid)
            folder_lines.append(f"{f['name'] if f else fid} ({self.sender_name(fid)}): {ok}" + (f", ошибок {bad}" if bad else ""))
        return {"status": status, "sent": stat["sent"], "failed": stat["failed"], "total": len(items),
                "unreachable": unreachable, "reason": stat["aborted"], "mode": mode, "reasons": reasons,
                "requested": aud["limit"], "matched": aud["matched"], "ready": aud["ready"],
                "skipped": {"новые лиды": aud["fresh"], "недавно писали": aud["cooled"],
                            "уже получали": aud["repeated"], "не вечерний контакт": aud["slot_skipped"]},
                "folders": folder_lines}

    async def _send_one(self, chat_id: int, topic_id: int, text: str, mode: str, media: dict | None = None,
                        sender=None) -> None:
        if mode == "userbot":
            sender = sender or self.userbot
            if media:
                ids = await sender.send_media(chat_id, topic_id, media, text)
            else:
                ids = [await sender.send_text(chat_id, topic_id, text)]
            for msg_id in ids:
                self.db.ignore_msg(chat_id, msg_id, int(time.time()))
            return
        await self._send_via_bot(chat_id, topic_id, text, media)

    BOT_METHODS = {"voice": "send_voice", "video": "send_video", "video_note": "send_video_note",
                   "photo": "send_photo", "document": "send_document", "audio": "send_audio",
                   "animation": "send_animation"}

    async def _bot_send(self, chat_id: int, topic_id: int, text: str, media: dict | None) -> list[int]:
        thread = topic_id if topic_id > 1 else None
        ids: list[int] = []
        if media:
            method = getattr(self.bot, self.BOT_METHODS.get(media["kind"], "send_document"))
            kw = {"message_thread_id": thread}
            if media["kind"] == "video_note":
                if text:                                  # у кружка не бывает подписи — текст отдельным сообщением
                    ids.append((await self.bot.send_message(chat_id, text, message_thread_id=thread)).message_id)
            elif text:
                kw["caption"] = text
            ids.append((await method(chat_id, media["file_id"], **kw)).message_id)
        else:
            ids.append((await self.bot.send_message(chat_id, text, message_thread_id=thread)).message_id)
        return ids

    async def _send_via_bot(self, chat_id: int, topic_id: int, text: str, media: dict | None = None) -> None:
        from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter

        for attempt in (1, 2):
            try:
                for mid in await self._bot_send(chat_id, topic_id, text, media):
                    self.db.ignore_msg(chat_id, int(mid), int(time.time()))
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

    async def send_test(self, admin_chat_id: int) -> None:
        """Пробное сообщение ТОЛЬКО администратору в личку (через бота): как увидит клиент."""
        text = self.db.get("push_text").strip()
        media = self.media()
        if not text and not media:
            raise ValueError("нет содержимого")
        await self._bot_send(admin_chat_id, 0, text, media)

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
        if result.get("requested") and result["total"] < result["requested"]:
            sk = "; ".join(f"{k} {v}" for k, v in result.get("skipped", {}).items() if v)
            lines.append(f"ℹ️ Просили {result['requested']}, подходящих лидов оказалось {result['total']}"
                         f" (по статусу и периоду: {result.get('matched', 0)})" + (f". Пропущено: {sk}" if sk else ""))
        if result.get("folders"):
            lines.append("📁 " + "; ".join(result["folders"]))
        if result.get("unreachable"):
            lines.append(f"Недоступно через бота (не нажали «Старт»): {result['unreachable']}")
        if result.get("reasons"):
            top = sorted(result["reasons"].items(), key=lambda kv: -kv[1])[:3]
            lines.append("Причины ошибок: " + "; ".join(f"{k} ×{v}" for k, v in top))
        if result.get("reason"):
            lines.append(f"Причина: {result['reason']}")
        return "\n".join(lines)
