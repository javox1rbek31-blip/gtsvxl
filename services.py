"""Фоновые сервисы: ежедневный отчёт, рейтинг сотрудников, резервная копия, контроль работоспособности.

Используются только метаданные (счётчики, ID, время). Тексты сообщений не читаются и не хранятся.
"""
from __future__ import annotations

import html
import logging
import os
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .db import Database
from .features import emoji_lists, feature_on, master_on
from .links import message_link, mention
from .monitor import DECISIONS, stavka_limits, stavka_running
from .statuses import classify
from .notifier import Notifier
from .worktime import is_work_time, local_date_str, local_day_start, parse_date, parse_hhmm

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


def esc(s) -> str:
    return html.escape(str(s), quote=False)


def limit_seconds(db: Database) -> int:
    return max(1, db.get_int("limit_minutes")) * 60


def people_lines(db: Database, since: int, until: int, markup: bool = True) -> list[str]:
    """Кто сколько клиентам ответил: «1. @ali — ответов 34 · ср. 6 мин · макс. 25 мин · позже лимита 3».
    Ответ = первый ответ сотрудника на неотвеченного клиента; время — рабочее, без перерывов."""
    rows = db.reply_stats(since, until, limit_seconds(db))
    out = []
    for i, r in enumerate(rows, start=1):
        name = db.emp_name(r["emp_id"])
        name = esc(name) if markup else name
        if r["late"]:
            late = f" · позже лимита {'<b>%d</b>' % r['late'] if markup else r['late']}"
        else:
            late = " · ✅ без опозданий"
        out.append(f"{i}. {name} — ответов {r['n']} · ср. {fmt_wait(r['avg_s'])} · макс. {fmt_wait(r['max_s'])}{late}")
    return out


def lead_periods_rows(db: Database, tz: ZoneInfo, now: int) -> tuple[list[tuple[str, int, int, int, int]], tuple[int, int, int, int]]:
    """Лиды по папкам: [(папка, сегодня, вчера, 7 дней, всего)] и итоговая строка."""
    today = local_day_start(now, tz)
    track = 0
    try:
        track = int(parse_date(db.get("track_from")).replace(tzinfo=tz).timestamp())
    except ValueError:
        pass
    per = db.lead_periods(today, today - 86400, today - 6 * 86400, track)
    rows = []
    for f in db.folders():
        d0, d1, w, t = per.get(f["id"], (0, 0, 0, 0))
        rows.append((f["name"], d0, d1, w, t))
    total = tuple(sum(r[i] for r in rows) for i in range(1, 5))
    return rows, total


def lead_stats_text(db: Database, tz: ZoneInfo, now: int, markup: bool = True) -> str:
    rows, total = lead_periods_rows(db, tz, now)
    b = (lambda x: f"<b>{x}</b>") if markup else (lambda x: str(x))
    lines = [f"📁 {b('Лиды по папкам')}", "(лид = отдельный чат/топик, по его первому сообщению)", ""]
    for name, d0, d1, w, t in sorted(rows, key=lambda r: -r[4]):
        lines.append(f"{b(esc(name) if markup else name)}: сегодня {d0} · вчера {d1} · 7 дн. {w} · всего {t}")
    lines.append("")
    lines.append(f"{b('ИТОГО')}: сегодня {total[0]} · вчера {total[1]} · 7 дн. {total[2]} · всего {total[3]} "
                 f"(с {db.get('track_from')})")
    return "\n".join(lines)


def ranking_text(db: Database, since: int, until: int, title: str) -> str:
    lines = people_lines(db, since, until)
    if not lines:
        return f"👥 <b>Сотрудники — {title}</b>\n\nОтветов пока нет."
    note = ("\n\n<i>Ответ — первый ответ сотрудника клиенту, который ждал. Время ожидания считается "
            f"только в рабочие часы и без перерывов. Лимит: {db.get_int('limit_minutes')} мин.</i>")
    return f"👥 <b>Сотрудники — {title}</b> (сверху — кто ответил больше)\n\n" + "\n".join(lines) + note


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
        lines.append(f"{esc(r['name'])}: {r['n']} × {money(r['fine'])} = <b>{money(sum_)}</b> ({who})")
    return lines, total


def fines_text(db: Database, since: int, until: int, title: str) -> str:
    lines, total = fine_lines(db, since, until)
    if not lines:
        has = any(f["fine"] for f in db.folders())
        return (f"💰 <b>Штрафы — {title}</b>\n\n" +
                ("Задержек со штрафом нет 👏" if has else "Штраф не задан: Папки → папка → «💰 Штраф за задержку»."))
    return f"💰 <b>Штрафы — {title}</b>\n\n" + "\n".join(lines) + f"\n\nИтого: <b>{money(total)}</b>"


def daily_text(db: Database, tz: ZoneInfo, since: int, until: int, title: str) -> str:
    """Дневной отчёт (HTML): лиды по папкам, кто как ответил, задержки, Push, штрафы."""
    leads = db.new_leads_by_folder(since, until)
    delays = db.alerts_by_folder(since, until)
    lines = [f"📊 <b>Отчёт — {esc(title)}</b>", ""]
    lines.append(f"🆕 <b>Новые лиды: {sum(r['n'] for r in leads)}</b>")
    lines += [f"   • {esc(r['name'])} — {r['n']}" for r in leads]
    people = people_lines(db, since, until)
    lines.append("")
    lines.append("👥 <b>Сотрудники</b> (ответы клиентам):" if people else "👥 Сотрудники: ответов за период нет.")
    lines += people
    lines.append("")
    lines.append(f"🚨 <b>Задержек в «Проёб»: {sum(r['n'] for r in delays)}</b>"
                 + (" (" + ", ".join(f"{esc(r['name'])} {r['n']}" for r in delays) + ")" if delays else ""))
    lines.append(f"📣 Push отправлено: <b>{db.pushed_between(since, until)}</b>")
    by = db.count_by_status()
    lines.append(f"⏳ Сейчас ждут ответа: <b>{by.get('pending', 0)}</b>, из них уже в «Проёб»: <b>{by.get('alerted', 0)}</b>")
    fl, ftotal = fine_lines(db, since, until)
    if fl:
        lines.append(f"💰 Штрафы: <b>{money(ftotal)}</b> (" + "; ".join(fl) + ")")
    if people:
        lines.append("")
        lines.append(f"<i>Ответ — первый ответ клиенту, который ждал; ожидание — рабочее время без перерывов; "
                     f"лимит {db.get_int('limit_minutes')} мин.</i>")
    return "\n".join(lines)


def who_label(db: Database, row) -> str:
    """Кто написал — для журнала: имя человека (если знаем), «бот», «мы», «Push»."""
    who = row["who"] or ""
    if who == "человек":
        return db.person_name(row["sender_id"]) or db.emp_name(row["sender_id"])
    return who


def journal_lines(db: Database, tz: ZoneInfo, rows, name=None) -> list[str]:
    """Журнал нити простыми словами: «21:05 · бот · кружок → кружок ставки».
    name — для HTML-экрана: функция, которая экранирует и помечает имя человека (остальное экранируется esc)."""
    plain = (lambda x: x) if name is None else esc
    out = []
    for r in rows:
        parts = [datetime.fromtimestamp(r["ts"], tz).strftime("%d.%m %H:%M")]
        who = who_label(db, r)
        if who:
            parts.append(name(who) if name is not None and r["who"] == "человек" else plain(who))
        if r["kind"]:
            parts.append(plain(r["kind"]))
        if r["source"] in ("сверка", "таймер"):
            parts.append(plain(r["source"]))
        out.append(" · ".join(parts) + " → " + plain(DECISIONS.get(r["decision"], r["decision"])))
    return out


def journal_csv(db: Database, tz: ZoneInfo, since: int) -> bytes:
    """Журнал решений за период — файл CSV (открывается в Excel/Numbers). Без текста сообщений."""
    import csv
    import io

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Время сообщения", "Решено", "Ссылка", "Чат", "Топик", "Сообщение", "Отправитель", "Кто", "Тип",
                "Источник", "Решение", "Что это значит"])
    titles = {c: (t or "") for c, t in ((r["chat_id"], r["title"]) for r in db.all("SELECT chat_id, title FROM chats"))}
    for r in db.journal_since(since):
        fmt = lambda ts: datetime.fromtimestamp(ts, tz).strftime("%d.%m.%Y %H:%M:%S")   # noqa: E731
        link = message_link(r["chat_id"], r["msg_id"], r["topic_id"]) if r["msg_id"] else ""
        w.writerow([fmt(r["ts"]), fmt(r["at"]), link, titles.get(r["chat_id"], str(r["chat_id"])), r["topic_id"],
                    r["msg_id"] or "", f"{r['sender_id'] or ''} {who_label(db, r)}".strip(), r["who"], r["kind"],
                    r["source"], r["decision"], DECISIONS.get(r["decision"], "")])
    return ("\ufeff" + buf.getvalue()).encode("utf-8")


def weekly_text(db: Database, tz: ZoneInfo, now: int, markup: bool = False) -> str:
    """Итог недели. Для группы «Проёб» — простой текст (markup=False), для панели — HTML (markup=True)."""
    _, end = day_bounds(now, tz)
    since = end - 7 * 86400
    a = datetime.fromtimestamp(since, tz).strftime("%d.%m")
    b = datetime.fromtimestamp(end - 1, tz).strftime("%d.%m")
    lines = [f"📅 Итог недели {a}–{b}", ""]
    rows, total = lead_periods_rows(db, tz, now)
    lines.append(f"🆕 Новых лидов за 7 дней: {total[2]}")
    lines += [f"   • {esc(n) if markup else n} — {w}" for n, d0, d1, w, t in sorted(rows, key=lambda r: -r[3]) if w]
    lines.append("")
    people = people_lines(db, since, end, markup=markup)
    lines.append("👥 Сотрудники (ответов · среднее · максимум · позже лимита):" if people else "👥 Ответов за неделю нет.")
    lines += people
    delays = db.alerts_by_folder(since, end)
    lines.append("")
    lines.append("🚨 Задержек: " + (", ".join(f"{esc(r['name']) if markup else r['name']} {r['n']}" for r in delays[:8]) +
                                   f" (всего {sum(r['n'] for r in delays)})" if delays else "нет 👏"))
    fl, ftotal = fine_lines(db, since, end)
    if fl:
        plain = [l.replace("<b>", "").replace("</b>", "") for l in fl]
        lines.append("💰 Штрафы: " + "; ".join(plain) + f" · всего {money(ftotal)}")
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
        await self.notifier.notify_admin(daily_text(self.db, self.tz, since, until, f"сегодня, {today}"), html=True)
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
        need, _, cap = stavka_limits(self.db)
        stavka_on = feature_on(self.db, "botwatch_enabled")
        by_folder: dict[int, list] = {}
        for r in self.db.leads_with_labels():
            if r["newlead_warned"] or r["first_seen"] < since:
                continue
            if max(r["last_seen"], r["last_activity_ts"] or 0) > border:
                continue
            if stavka_on and stavka_running(r, need, cap, now):
                continue                      # идёт ставка нового лида — для неё своё напоминание «ставка»
            if classify(r["label"], push_e, skip_e) == "skip":
                continue
            fid = cmap.get(r["chat_id"])
            if fid is None or not folders[fid]["delay_on"]:
                continue
            st = self.db.state(r["chat_id"], r["topic_id"])
            if st is not None and st["status"] in ("pending", "alerted"):
                continue                      # клиент ждёт ответа — это уже зона «задержки», не дублируем
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
