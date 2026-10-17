"""Фоновые сервисы: ежедневный отчёт, рейтинг сотрудников, резервная копия, контроль работоспособности.

Используются только метаданные (счётчики, ID, время). Тексты сообщений не читаются и не хранятся.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .db import Database
from .features import emoji_lists, feature_on, master_on
from .links import message_link, mention
from .statuses import classify
from .notifier import Notifier
from .worktime import is_work_time, local_date_str, local_day_start, parse_hhmm

log = logging.getLogger("services")


def fmt_wait(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    s = int(seconds)
    if s < 60:
        return f"{s} с"
    m = s // 60
    if m < 60:
        return f"{m} мин"
    return f"{m // 60} ч {m % 60} мин"


def day_bounds(ts: int, tz: ZoneInfo) -> tuple[int, int]:
    start = local_day_start(ts, tz)
    end = int((datetime.fromtimestamp(start, tz) + timedelta(days=1)).timestamp())
    return start, end


def ranking_text(db: Database, since: int, until: int, title: str) -> str:
    rows = db.reply_stats(since, until)
    if not rows:
        return f"🏆 <b>Сотрудники — {title}</b>\n\nОтветов пока нет."
    lines = []
    for i, r in enumerate(rows, start=1):
        lines.append(f"{i}. {db.emp_name(r['emp_id'])} — ответов {r['n']}, "
                     f"в среднем {fmt_wait(r['avg_s'])}, максимум {fmt_wait(r['max_s'])}")
    note = "\n\n<i>Время ожидания считается только в рабочие часы и без перерывов.</i>"
    return f"🏆 <b>Сотрудники — {title}</b> (быстрые сверху)\n\n" + "\n".join(lines) + note


def money(n: int) -> str:
    return f"{int(n):,}".replace(",", " ")


def fine_lines(db: Database, since: int, until: int) -> tuple[list[str], int]:
    """Штраф = число задержек папки × сумма штрафа папки. Сотрудники — из этой папки."""
    lines, total = [], 0
    for r in db.alerts_by_folder(since, until):
        if not r["fine"]:
            continue
        sum_ = int(r["n"]) * int(r["fine"])
        total += sum_
        who = ", ".join("@" + e["username"] for e in db.employees(r["fid"])) or "—"
        lines.append(f"{r['name']}: {r['n']} × {money(r['fine'])} = <b>{money(sum_)}</b> ({who})")
    return lines, total


def fines_text(db: Database, since: int, until: int, title: str) -> str:
    lines, total = fine_lines(db, since, until)
    if not lines:
        has = any(f["fine"] for f in db.folders())
        return (f"💰 <b>Штрафы — {title}</b>\n\n" +
                ("Задержек со штрафом нет 👏" if has else "Штраф не задан: Папки → папка → «💰 Штраф за задержку»."))
    return f"💰 <b>Штрафы — {title}</b>\n\n" + "\n".join(lines) + f"\n\nИтого: <b>{money(total)}</b>"


def daily_text(db: Database, tz: ZoneInfo, since: int, until: int, title: str) -> str:
    leads = db.new_leads_by_folder(since, until)
    delays = db.alerts_by_folder(since, until)
    replies = db.reply_stats(since, until)
    total_replies = sum(r["n"] for r in replies)
    total_avg = (sum(r["avg_s"] * r["n"] for r in replies) / total_replies) if total_replies else None
    lines = [f"📊 <b>Отчёт — {title}</b>", ""]
    lines.append(f"🆕 Новых лидов: <b>{sum(r['n'] for r in leads)}</b>"
                 + (" (" + ", ".join(f"{r['name']}: {r['n']}" for r in leads[:6]) + ")" if leads else ""))
    lines.append(f"✅ Ответов менеджеров: <b>{total_replies}</b>, среднее ожидание: <b>{fmt_wait(total_avg)}</b>")
    lines.append(f"🚨 Задержек (уведомлений в «Проёб»): <b>{sum(r['n'] for r in delays)}</b>"
                 + (" (" + ", ".join(f"{r['name']}: {r['n']}" for r in delays[:6]) + ")" if delays else ""))
    lines.append(f"📣 Push отправлено: <b>{db.pushed_between(since, until)}</b>")
    by = db.count_by_status()
    lines.append(f"⏳ Сейчас ждут ответа: <b>{by.get('pending', 0)}</b>, в «Проёб»: <b>{by.get('alerted', 0)}</b>")
    fl, ftotal = fine_lines(db, since, until)
    if fl:
        lines.append(f"💰 Штрафы: <b>{money(ftotal)}</b> (" + "; ".join(fl) + ")")
    if replies:
        best, worst = replies[0], replies[-1]
        lines.append("")
        lines.append(f"🥇 Быстрее всех: {db.emp_name(best['emp_id'])} ({fmt_wait(best['avg_s'])})")
        if len(replies) > 1:
            lines.append(f"🐢 Медленнее всех: {db.emp_name(worst['emp_id'])} ({fmt_wait(worst['avg_s'])})")
    return "\n".join(lines)


def weekly_text(db: Database, tz: ZoneInfo, now: int) -> str:
    """Короткий итог недели для «Проёб»: рейтинг по скорости ответа и задержки по папкам."""
    _, end = day_bounds(now, tz)
    since = end - 7 * 86400
    a = datetime.fromtimestamp(since, tz).strftime("%d.%m")
    b = datetime.fromtimestamp(end - 1, tz).strftime("%d.%m")
    rows = db.reply_stats(since, end)
    lines = [f"📅 Итог недели {a}–{b}"]
    if rows:
        for i, r in enumerate(rows[:12], start=1):
            lines.append(f"{i}. {db.emp_name(r['emp_id'])} — {r['n']} отв., ср. {fmt_wait(r['avg_s'])}")
    else:
        lines.append("Ответов за неделю нет.")
    delays = db.alerts_by_folder(since, end)
    lines.append("🚨 Задержек: " + (", ".join(f"{r['name']} {r['n']}" for r in delays[:8]) +
                                   f" (всего {sum(r['n'] for r in delays)})" if delays else "нет 👏"))
    fl, ftotal = fine_lines(db, since, end)
    if fl:
        lines.append("💰 Штрафы: " + "; ".join(l.replace("<b>", "").replace("</b>", "") for l in fl) + f" · всего {money(ftotal)}")
    return "\n".join(lines)


class Services:
    def __init__(self, db: Database, tz: ZoneInfo, notifier: Notifier, userbot, clock=time.time, data_dir: str = "/tmp"):
        self.db, self.tz, self.notifier, self.userbot, self.clock = db, tz, notifier, userbot, clock
        self.data_dir = data_dir
        self._last_check = 0.0
        self._offline_since: float | None = None
        self._offline_notified = False
        self._started = clock()
        self.pool = None
        self._group_fail_until = 0.0          # после неудачной отправки в «Проёб» пауза 10 мин (не спамить логом)

    # -------------------------------------------------------------- отчёт
    async def daily_report(self, now: int) -> bool:
        if not feature_on(self.db, "report_enabled"):
            return False
        try:
            at = parse_hhmm(self.db.get("report_time"))
        except ValueError:
            return False
        local = datetime.fromtimestamp(now, self.tz)
        today = local_date_str(now, self.tz)
        if self.db.get("report_last") == today or (local.hour, local.minute) < (at.hour, at.minute):
            return False
        self.db.set("report_last", today)                      # сначала отметка: не слать дважды
        since, until = day_bounds(now, self.tz)
        await self.notifier.notify_admin(daily_text(self.db, self.tz, since, until, f"сегодня, {today}"))
        return True

    async def weekly_report(self, now: int) -> bool:
        """Воскресенье после 21:00 — короткий итог недели в «Проёб» (раз в неделю)."""
        if not feature_on(self.db, "weekly_enabled"):
            return False
        local = datetime.fromtimestamp(now, self.tz)
        today = local_date_str(now, self.tz)
        if local.weekday() != 6 or local.hour < 21 or self.db.get("weekly_last") == today:
            return False
        self.db.set("weekly_last", today)
        await self.notifier.send_group_text(weekly_text(self.db, self.tz, now))
        return True

    # -------------------------------------------------------------- напоминание сотруднику об оплате
    def _who_folder(self, chat_id: int, cmap: dict):
        folder = self.db.folder(cmap.get(chat_id, 0))
        who = " ".join(mention(e["username"]) for e in self.db.employees(folder["id"])) if folder else ""
        return folder, who

    async def payment_reminders(self, now: int) -> int:
        """После «реквизиты отправлены» N минут нет ни сообщения сотрудника, ни чека/PDF от клиента —
        напоминание СОТРУДНИКУ в «Проёб». Клиенту бот ничего не пишет (кроме Push)."""
        if not feature_on(self.db, "pay_enabled") or now < self._group_fail_until:
            return 0
        text = self.db.get("pay_text").strip()
        mins = max(1, self.db.get_int("pay_minutes"))
        cmap = self.db.chat_folder_map()
        sent = 0
        for r in self.db.due_payments(now - mins * 60):
            folder, who = self._who_folder(r["chat_id"], cmap)
            if folder is not None and not folder["delay_on"]:
                self.db.mark_payment_reminded(r["chat_id"], r["topic_id"])
                continue
            link = message_link(r["chat_id"], r["topic_id"] if r["topic_id"] > 1 else 1, r["topic_id"])
            msg = f"{link}\n{who} {folder['name'] if folder else ''} {text}".replace("  ", " ").replace("\n ", "\n")
            try:
                await self.notifier.send_group_text(msg)
            except Exception as exc:
                log.warning("Напоминание об оплате не отправлено: %s", type(exc).__name__)
                self._group_fail_until = now + 600
                break
            self.db.mark_payment_reminded(r["chat_id"], r["topic_id"])
            sent += 1
        return sent

    # -------------------------------------------------------------- новый лид без контакта
    async def new_lead_reminders(self, now: int) -> int:
        """Сегодняшний лид, в котором нет активности дольше N минут (по умолчанию 2 часа) — один раз напоминаем в «Проёб»."""
        if not feature_on(self.db, "newlead_enabled") or now < self._group_fail_until:
            return 0
        ws, we = parse_hhmm(self.db.get("work_start")), parse_hhmm(self.db.get("work_end"))
        if not is_work_time(now, self.tz, ws, we):
            return 0
        mins = max(1, self.db.get_int("newlead_minutes"))
        border = now - mins * 60
        day_start = local_day_start(now, self.tz)
        since = max(day_start, self.db.get_int("newlead_from"))
        push_e, skip_e = emoji_lists(self.db)
        cmap = self.db.chat_folder_map()
        folders = {f["id"]: f for f in self.db.folders()}
        by_folder: dict[int, list] = {}
        for r in self.db.leads_with_labels():
            if r["newlead_warned"] or r["first_seen"] < since:
                continue
            if max(r["last_seen"], r["last_activity_ts"] or 0) > border:
                continue
            if classify(r["label"], push_e, skip_e) == "skip":
                continue
            fid = cmap.get(r["chat_id"])
            if fid is None or not folders[fid]["delay_on"]:
                continue
            by_folder.setdefault(fid, []).append(r)
        sent = 0
        for fid, rows in by_folder.items():
            rows.sort(key=lambda r: r["first_seen"])
            who = " ".join(mention(e["username"]) for e in self.db.employees(fid))
            for i in range(0, len(rows), 30):
                chunk = rows[i:i + 30]
                links = [message_link(r["chat_id"], r["last_msg_id"] or r["topic_id"], r["topic_id"]) for r in chunk]
                text = "\n".join(links + [f"{who} {folders[fid]['name']} новый лид без контакта {fmt_wait(mins * 60)}".replace("  ", " ")])
                try:
                    await self.notifier.send_group_text(text)
                except Exception as exc:
                    log.warning("Напоминание о новом лиде не отправлено: %s", type(exc).__name__)
                    self._group_fail_until = now + 600
                    return sent
                for r in chunk:
                    self.db.mark_newlead_warned(r["chat_id"], r["topic_id"])
                sent += 1
        return sent

    # -------------------------------------------------------------- резервная копия
    def make_backup(self, now: int) -> str:
        path = os.path.join(self.data_dir, f"backup-{datetime.fromtimestamp(now, self.tz):%Y%m%d-%H%M}.sqlite3")
        self.db.backup_to(path)
        return path

    async def weekly_backup(self, now: int) -> bool:
        if not feature_on(self.db, "backup_enabled"):
            return False
        if now - self.db.get_int("backup_last") < 7 * 86400:
            return False
        self.db.set("backup_last", str(now))
        path = self.make_backup(now)
        try:
            await self.notifier.send_admin_document(
                path, "💾 Резервная копия настроек (без текста сообщений и без сессии аккаунта).")
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
        return True

    # -------------------------------------------------------------- работоспособность
    async def health(self, now: float) -> None:
        if not feature_on(self.db, "health_enabled"):
            return
        state = getattr(self.userbot, "state", "")
        if state in ("no_session", "disabled", "starting"):
            self._offline_since = None
            return
        if state == "online":
            if self._offline_notified:
                self._offline_notified = False
                await self.notifier.notify_admin("✅ Юзербот снова онлайн.", key="ub-up", min_interval=60)
            self._offline_since = None
            last = getattr(self.userbot, "last_event", 0.0) or 0.0
            ws, we = parse_hhmm(self.db.get("work_start")), parse_hhmm(self.db.get("work_end"))
            if last and now - last > 90 * 60 and is_work_time(now, self.tz, ws, we) \
                    and now - self._started > 600:
                await self.notifier.notify_admin(
                    "🩺 Давно нет событий от Telegram (90+ мин в рабочее время). Если в чатах есть активность — "
                    "нажмите «🔑 Аккаунт юзербота → 🔄 Обновить».", key="ub-silent", min_interval=3 * 3600)
            return
        if self._offline_since is None:
            self._offline_since = now
        if now - self._offline_since > 180:
            self._offline_notified = True
            await self.notifier.notify_admin(
                f"⚠️ Юзербот не в сети более 3 минут (состояние: {state}). Бот не видит новые сообщения. "
                "Проверьте «🔑 Аккаунт юзербота».", key="ub-down", min_interval=1800)

    # -------------------------------------------------------------- общий вызов (раз в несколько секунд)
    async def tick(self, now: float | None = None) -> None:
        now = self.clock() if now is None else now
        if now - self._last_check < 30:
            return
        self._last_check = now
        if not master_on(self.db):
            return
        for step in (self.health, self.daily_report, self.weekly_report, self.payment_reminders, self.new_lead_reminders, self.weekly_backup):
            try:
                await step(now if step == self.health else int(now))
            except Exception:
                log.exception("Ошибка сервиса %s", step.__name__)
