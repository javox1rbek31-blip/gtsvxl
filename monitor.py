"""Ядро мониторинга: задержки ответа и ставка нового лида.

Работает ТОЛЬКО с метаданными: ID чата, ID сообщения, время, ID отправителя, признак «исходящее»,
тип вложения (кружок). Текст сообщений сюда не передаётся вообще.

Ставка нового лида (не путать с задержкой):
  • начинается, когда появился новый лид (новый топик форума);
  • заканчивается, когда в нити пришло N кружков (video_note, по умолчанию 3) — это метка конца ставки;
  • пока ставка идёт, НИ сообщения клиента, НИ сообщения сотрудников не учитываются: таймер задержки не
    запускается и ответом это не считается. Считаются только кружки;
  • если кружков нет к сроку — одно напоминание «ставка» в «Проёб» (это не задержка), ставка продолжается;
  • страховка: ставка длится не дольше botwatch_cap минут, дальше нить под обычным контролем.
Задержка считается только по сообщениям после окончания ставки.
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

NEW_GAP = 25           # новый топик: первое сообщение лида идёт сразу за служебным «топик создан» (id топика)

# Решения по сообщениям (пишутся в журнал; «🔍 Проверить лид» показывает их по-русски)
DECISIONS: dict[str, str] = {
    "push": "наш Push — не учитываем",
    "requisites": "«Реквизиты отправлены» — ждём чек или ответ",
    "stavka_start": "новый лид — ставка началась",
    "stavka_bet": "кружок ставки",
    "stavka_end": "кружок — ставка закончилась",
    "stavka_skip": "идёт ставка — не учитываем",
    "reply": "ответ сотрудника — ожидание закрыто",
    "staff": "сотрудник написал (ответа никто не ждал)",
    "reply_old": "ответ старше сообщения клиента — пропуск",
    "before_track": "раньше даты начала учёта — пропуск",
    "answered": "уже отвечено — пропуск",
    "opening": "«Новый чат» — не сообщение клиента",
    "auto_bet": "кружок бота (автоставка) — ответа не ждёт",
    "wait": "клиент ждёт ответа",
    "wait_more": "клиент дописал — ждёт ответа (отсчёт с первого)",
    "alert": "«задержка» в «Проёб»",
    "stavka_remind": "«ставка» в «Проёб» (кружков нет)",
    "stavka_found": "ставка распознана при сверке",
    "wait_dropped": "ожидание снято — сообщение было во время ставки",
    "wait_moved": "ожидание перенесено на сообщение после ставки",
    "wait_after": "после ставки клиент ждёт ответа",
}


def stavka_limits(db: Database) -> tuple[int, int, int]:
    """(сколько кружков завершают ставку; через сколько секунд от начала ставки напомнить «ставка»,
    если кружков меньше; предел длительности ставки в секундах). Кружки идут примерно раз в 13 минут."""
    need = max(1, db.get_int("botwatch_count"))
    remind = max(5, db.get_int("botwatch_remind")) * 60
    cap = max(remind, db.get_int("botwatch_cap") * 60)
    return need, remind, cap


def stavka_running(lead, need: int, cap: int, at_ts: int) -> bool:
    """Шла ли ставка у строки лида (таблица leads) в момент at_ts (время сообщения или «сейчас»):
    ставка начата, at_ts раньше N-го кружка (stavka_end) и раньше предела. По времени, а не по порядку
    обработки: сообщение, написанное до 3-го кружка, относится к ставке, даже если обработано позже."""
    if lead is None or lead["stavka_from"] is None:
        return False
    limit = int(lead["stavka_from"]) + cap
    end = lead["stavka_end"]
    if end is not None:
        limit = min(limit, int(end))
    elif int(lead["bet_count"]) >= need:
        return False                       # кружков уже N, а время конца неизвестно — ставка закончилась
    return at_ts < limit


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
        self._watch_fail_until = 0.0
        # async (chat_id, topic_id) -> [(msg_id, ts), ...] кружков нити; None — Telegram недоступен целиком
        # (юзербот не подключён); исключение — не удалось именно для этой нити. Задаёт юзербот: нужно, чтобы
        # не потерять кружок, если событие о нём не дошло.
        self.bet_counter = None
        self._recount_fails: dict[tuple[int, int], int] = {}
        self._recount_retry: dict[tuple[int, int], float] = {}

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
                             proof: bool = False, requisites: bool = False, from_bot: bool | None = None,
                             opening: bool = False, live: bool = True) -> str | None:
        """Одно сообщение нити → решение (код из DECISIONS; пишется в журнал). None — чат не под контролем.
        bet=True — кружок (video_note): определяется по ТИПУ вложения, текст не читаем.
        topic_id: id топика форума (лид = топик) либо 0 для обычного чата.
        live=False — сверка по последнему сообщению нити, а не живое событие."""
        self._fresh()
        if not master_on(self.db) or chat_id not in self._chat_ids or service:
            return None
        if self.db.is_ignored(chat_id, msg_id) or (sender_id is not None and sender_id in self._push_ids):
            decision = "push"                        # наша рассылка / наш аккаунт Push — не клиент и не сотрудник
        else:
            decision = await self._decide(chat_id, topic_id, msg_id, ts, sender_id, out, bet, proof, requisites,
                                          from_bot, opening, live)
        who = ("Push" if decision == "push" else "мы" if out else
               "бот" if from_bot else "человек" if from_bot is False else "?")
        kind = "кружок" if bet else "фото/PDF" if proof else "сообщение"
        self._journal(ts, chat_id, topic_id, msg_id, sender_id, who, kind, "событие" if live else "сверка", decision)
        return decision

    def _journal(self, ts: int, chat_id: int, topic_id: int, msg_id: int | None, sender_id: int | None,
                 who: str, kind: str, source: str, decision: str) -> None:
        try:
            self.db.journal_add(int(self.clock()), int(ts), chat_id, topic_id, msg_id, sender_id, who, kind,
                                source, decision)
        except Exception:
            log.warning("Журнал: запись не удалась", exc_info=True)

    async def _decide(self, chat_id: int, topic_id: int, msg_id: int, ts: int, sender_id: int | None, out: bool,
                      bet: bool, proof: bool, requisites: bool, from_bot: bool | None, opening: bool,
                      live: bool) -> str:
        state = self.db.state(chat_id, topic_id)
        self.db.mark_seen(chat_id, topic_id, msg_id)
        self.db.touch_activity(chat_id, topic_id, ts)

        if requisites:                               # посредник отправил реквизиты: ждём чек/ответ сотрудника
            if feature_on(self.db, "pay_enabled"):
                self.db.start_payment(chat_id, topic_id, ts)
            return "requisites"

        # Клиенты пишут ТОЛЬКО через бота-посредника, поэтому любой живой человек (не бот) — сотрудник:
        # отвечать может не только закреплённый за папкой, но и любой другой.
        human = from_bot is False and sender_id is not None
        staff = out or human or (sender_id is not None and sender_id in self._emp_ids)

        absorbed = self._absorb_stavka(chat_id, topic_id, msg_id, ts, sender_id, staff, bet, live)
        if absorbed:
            if staff or proof:                       # напоминание об оплате — отдельная функция, ей ответ/чек важен
                self.db.done_payment(chat_id, topic_id)
            return absorbed                          # идёт ставка нового лида — для задержки сообщение не учитываем

        if staff:
            self.db.done_payment(chat_id, topic_id)   # сотрудник написал — напоминать клиенту не нужно
            if state is not None and state["last_lead_ts"] and ts < state["last_lead_ts"]:
                return "reply_old"                     # ответ старше последнего сообщения лида
            self._record_reply(chat_id, topic_id, state, sender_id, out, ts)
            was_waiting = self.db.set_idle(chat_id, topic_id, ts)
            await self._resolve_replied(chat_id, topic_id)
            return "reply" if was_waiting else "staff"

        if proof:
            self.db.done_payment(chat_id, topic_id)   # клиент прислал фото/PDF
        if ts < self.track_from_ts():
            return "before_track"
        if state is not None and state["last_reply_ts"] and ts < state["last_reply_ts"]:
            return "answered"                          # на это сообщение уже ответили
        if opening:
            # «Новый чат» / «Автоставки запущены» (нить без отслеживаемой ставки): нить появилась, но это
            # не сообщение клиента — таймера задержки нет, ответа на него никто не ждёт
            if feature_on(self.db, "leads_enabled"):
                self.db.touch_lead(chat_id, topic_id, sender_id, msg_id, ts)
            return "opening"
        # кружок от бота в нити без отслеживаемой ставки (старый лид, ставка выключена): автоставка, а не
        # сообщение клиента — см. _early_bet
        early_bet = bet and from_bot is not False and self._early_bet(chat_id, topic_id, msg_id, ts, live)
        if feature_on(self.db, "leads_enabled"):
            self.db.touch_lead(chat_id, topic_id, sender_id, msg_id, ts)
            if not bet:
                self.db.bump_count(chat_id, topic_id)
        if bet:
            self.db.add_bet(chat_id, topic_id, msg_id, ts)       # кружки учитываем всегда (каждый один раз)
        if early_bet:
            return "auto_bet"
        waiting = state is not None and state["status"] in ("pending", "alerted")
        restart = state is not None and (
            state["status"] == "alerted"
            or (state["status"] == "pending" and (state["pending_since"] or 0) < self.db.get_int("alerts_from")))
        self.db.set_pending(chat_id, topic_id, msg_id, ts, restart_streak=restart)
        return "wait_more" if waiting and not restart else "wait"

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
                            out: bool, service: bool, from_bot: bool | None = None, bet: bool = False,
                            opening: bool = False) -> None:
        """Сверка по последнему сообщению нити (страховка от пропущенных событий)."""
        self._fresh()
        if not master_on(self.db) or chat_id not in self._chat_ids or service:
            return
        if sender_id is not None and sender_id in self._push_ids:
            return
        state = self.db.state(chat_id, topic_id)
        if state is not None and state["seen_msg_id"] and msg_id <= state["seen_msg_id"]:
            return                                     # уже обработано (живым событием или прошлой сверкой)
        is_employee = out or (from_bot is False and sender_id is not None) or (sender_id is not None and sender_id in self._emp_ids)
        if is_employee:
            if state is not None and state["status"] in ("pending", "alerted"):
                await self.handle_message(chat_id, topic_id, msg_id, ts, sender_id, out, from_bot=from_bot, live=False)
            else:
                self.db.touch_activity(chat_id, topic_id, ts)
            return
        if state is not None and state["last_lead_msg_id"] == msg_id:
            return                                     # это сообщение уже учтено
        await self.handle_message(chat_id, topic_id, msg_id, ts, sender_id, out, bet=bet, from_bot=from_bot,
                                  opening=opening, live=False)

    # ------------------------------------------------------------ ставка нового лида
    @staticmethod
    def looks_new(topic_id: int, msg_id: int) -> bool:
        """Сообщение из только что созданного топика: id топика = id служебного «топик создан»,
        а первые сообщения нового лида идут сразу за ним."""
        return topic_id > 1 and 0 < msg_id - topic_id <= NEW_GAP

    def in_stavka(self, chat_id: int, topic_id: int, now: int | None = None) -> bool:
        """Нить сейчас в ставке (кружков меньше N, предел не вышел)."""
        if topic_id <= 1 or not feature_on(self.db, "botwatch_enabled"):
            return False
        need, _, cap = stavka_limits(self.db)
        return stavka_running(self.db.lead_row(chat_id, topic_id), need, cap,
                              int(self.clock() if now is None else now))

    def stavka_owns(self, chat_id: int, topic_id: int, msg_id: int, ts: int, live: bool = True) -> bool:
        """У нити есть ставка нового лида (идёт или была) либо это сообщение её начнёт? Тогда запасное
        распознавание «Новый чат» по тексту не нужно и юзербот в текст не смотрит вообще.
        Условия начала — те же, что в _absorb_stavka."""
        if topic_id <= 1 or not feature_on(self.db, "botwatch_enabled"):
            return False
        lead = self.db.lead_row(chat_id, topic_id)
        if lead is None:
            return (live and self.looks_new(topic_id, msg_id)
                    and ts >= max(self.db.get_int("botwatch_from"), self.track_from_ts()))
        return lead["stavka_from"] is not None

    def _absorb_stavka(self, chat_id: int, topic_id: int, msg_id: int, ts: int, sender_id: int | None,
                       staff: bool, bet: bool, live: bool) -> str | None:
        """Сообщение во время ставки: считаем только кружки, остальное (клиент или сотрудник) не учитываем.
        Возвращает решение (stavka_start / stavka_bet / stavka_end / stavka_skip) или None — ставки нет."""
        if topic_id <= 1 or not feature_on(self.db, "botwatch_enabled") or ts < self.track_from_ts():
            return None
        lead = self.db.lead_row(chat_id, topic_id)
        started = False
        if lead is None:
            # Первое сообщение нити. Новый лид = свежий топик, и пишет не сотрудник (посредник/клиент).
            # Сверка (live=False) ставку не начинает — для пропущенных топиков есть discover_topic.
            if staff or not live or not self.looks_new(topic_id, msg_id) or ts < self.db.get_int("botwatch_from"):
                return None
            self.db.touch_lead(chat_id, topic_id, sender_id, msg_id, ts)
            self.db.start_stavka(chat_id, topic_id, ts)
            log.info("Ставка: новый лид %s/%s", chat_id, topic_id)
            lead = self.db.lead_row(chat_id, topic_id)
            started = True
        need, _, cap = stavka_limits(self.db)
        if not stavka_running(lead, need, cap, ts):
            return None
        if not staff:
            self.db.touch_lead(chat_id, topic_id, sender_id, msg_id, ts)
            if live and not bet:
                self.db.bump_count(chat_id, topic_id)
        decision = "stavka_start" if started else "stavka_skip"
        if bet and self.db.add_bet(chat_id, topic_id, msg_id, ts):   # кружок — от кого угодно
            decision = "stavka_bet"
            if self.db.finish_stavka(chat_id, topic_id, need) is not None:
                log.info("Ставка: %s/%s завершена (%s-й кружок)", chat_id, topic_id, need)
                decision = "stavka_end"
        return decision

    def _early_bet(self, chat_id: int, topic_id: int, msg_id: int, ts: int, live: bool) -> bool:
        """Кружок от бота вне идущей ставки: автоставка (таймер не запускаем) или кружок клиента?
        • Ставка нити отслежена и закончилась 3-м кружком — решает время: до её конца — ставка, после — клиент.
        • Иначе (старый лид, ставка выключена): сверка, а не живое событие (по последнему сообщению нити не
          отличить) — ставка; уже учтённый кружок — ставка; пока в нити учтено меньше N кружков — ставка.
          Кружок клиента запускает таймер, только если пришёл живым событием после N учтённых кружков."""
        lead = self.db.lead_row(chat_id, topic_id)
        if lead is not None and lead["stavka_end"] is not None:
            return ts <= int(lead["stavka_end"])
        if not live or self.db.has_bet(chat_id, topic_id, msg_id):
            return True
        return self.db.bet_rows(chat_id, topic_id) < stavka_limits(self.db)[0]

    async def _bets_from_telegram(self, chat_id: int, topic_id: int) -> tuple[list | None, bool]:
        """Кружки нити по данным Telegram (вдруг событие не дошло): ([(msg_id, ts), ...] | None, offline).
        offline=True — Telegram недоступен целиком (юзербот не подключён); иначе None — ошибка именно этой нити."""
        if self.bet_counter is None:
            return None, True
        try:
            found = await self.bet_counter(chat_id, topic_id)
        except Exception as exc:
            log.warning("Ставка: не удалось сверить кружки %s/%s: %s", chat_id, topic_id, type(exc).__name__)
            return None, False
        return found, found is None

    def _apply_bets(self, chat_id: int, topic_id: int, found: list) -> None:
        for mid, ts in found:
            self.db.add_bet(chat_id, topic_id, int(mid), int(ts))

    async def discover_topic(self, chat_id: int, topic_id: int, created_ts: int, creator_id: int | None = None) -> bool:
        """Страховка: свежий топик, который не распознали по первому сообщению (юзербот был офлайн,
        пропустил событие и т.п.). Начинаем ставку с даты создания топика; сколько кружков уже пришло —
        спрашиваем у Telegram. Если спросить не удалось — ставку не начинаем (обычный контроль надёжнее,
        чем молча выключенный). Возвращает True, если ставку отметили."""
        self._fresh()
        now = int(self.clock())
        if (topic_id <= 1 or chat_id not in self._chat_ids or not feature_on(self.db, "botwatch_enabled")
                or created_ts < max(self.db.get_int("botwatch_from"), self.track_from_ts())):
            return False
        if creator_id is not None and (creator_id in self._emp_ids or creator_id in self._push_ids):
            return False                                 # топик создал сотрудник — это не новый лид
        need, _, cap = stavka_limits(self.db)
        if now >= created_ts + cap:
            return False
        lead = self.db.lead_row(chat_id, topic_id)
        if lead is not None and lead["stavka_from"] is not None:
            return False                                 # ставка уже отслеживается
        found, _ = await self._bets_from_telegram(chat_id, topic_id)
        if found is None:
            return False
        if self.db.lead_row(chat_id, topic_id) is None:
            self.db.touch_lead(chat_id, topic_id, None, topic_id, created_ts)
        if not self.db.start_stavka(chat_id, topic_id, created_ts):
            return False                                 # пока спрашивали, ставку уже отметило живое событие
        self._apply_bets(chat_id, topic_id, found)
        self.db.recount_bets(chat_id, topic_id)          # счёт по данным Telegram, а не старый (мог быть завышен)
        self.db.finish_stavka(chat_id, topic_id, need)
        self._journal(created_ts, chat_id, topic_id, topic_id, creator_id, "", f"кружков {len(found)}", "сверка",
                      "stavka_found")
        await self._forget_stavka_wait(chat_id, topic_id, need, cap)
        self._wait_after_stavka(chat_id, topic_id)
        log.info("Ставка: топик %s/%s найден при сверке (кружков: %s)", chat_id, topic_id, len(found))
        return True

    def _wait_after_stavka(self, chat_id: int, topic_id: int) -> None:
        """Конец ставки узнали задним числом (кружок досчитали из Telegram): последнее сообщение клиента
        после 3-го кружка, на которое никто не ответил, должно ждать ответа — как если бы знали сразу."""
        lead = self.db.lead_row(chat_id, topic_id)
        if lead is None or lead["stavka_end"] is None:
            return
        last_ts, last_id = int(lead["last_seen"]), int(lead["last_msg_id"])
        if last_ts <= int(lead["stavka_end"]) or self.db.has_bet(chat_id, topic_id, last_id):
            return                                       # после ставки клиент не писал (последнее — кружок)
        if int(lead["last_activity_ts"] or 0) > last_ts:
            return                                       # после клиента писал сотрудник — уже отвечено
        st = self.db.state(chat_id, topic_id)
        if st is not None and (st["status"] in ("pending", "alerted")
                               or (st["last_reply_ts"] and st["last_reply_ts"] >= last_ts)):
            return
        if last_ts < max(self.track_from_ts(), self.db.get_int("alerts_from")):
            return
        self.db.set_pending(chat_id, topic_id, last_id, last_ts, restart_streak=False)
        self._journal(last_ts, chat_id, topic_id, last_id, lead["sender_id"], "", "сообщение", "сверка", "wait_after")

    async def _forget_stavka_wait(self, chat_id: int, topic_id: int, need: int, cap: int) -> None:
        """Ожидание ответа, начатое сообщением, написанным во время ставки, — не задержка (ставку распознали
        задним числом). Если клиент писал и после ставки — ждём ответа на то сообщение."""
        st = self.db.state(chat_id, topic_id)
        if st is None or st["status"] not in ("pending", "alerted") or st["pending_since"] is None:
            return
        lead = self.db.lead_row(chat_id, topic_id)
        if not stavka_running(lead, need, cap, int(st["pending_since"])):
            return                                       # ждут ответа на сообщение после ставки — всё верно
        if st["last_lead_ts"] is not None and not stavka_running(lead, need, cap, int(st["last_lead_ts"])):
            self.db.rebase_pending(chat_id, topic_id, int(st["last_lead_msg_id"]), int(st["last_lead_ts"]))
            self._journal(int(st["last_lead_ts"]), chat_id, topic_id, int(st["last_lead_msg_id"]), None, "",
                          "сообщение", "сверка", "wait_moved")
        else:
            self.db.drop_pending(chat_id, topic_id)
            self._journal(int(st["pending_since"]), chat_id, topic_id, st["first_lead_msg_id"], None, "",
                          "сообщение", "сверка", "wait_dropped")
        if st["status"] == "alerted":
            await self._resolve_replied(chat_id, topic_id, "это была ставка нового лида", void=True)

    # ------------------------------------------------------------ закрытие алертов
    async def _resolve_replied(self, chat_id: int, topic_id: int, by: str = "ответил менеджер",
                               void: bool = False) -> None:
        """Убрать нить из открытых уведомлений. void=True — это была не задержка (ставка нового лида):
        такая нить не идёт в штрафы и отчёты."""
        key = [chat_id, topic_id]
        for alert in self.db.open_alerts():
            remaining = json.loads(alert["remaining"])
            if key not in remaining:
                continue
            remaining.remove(key)
            left = self.db.void_alert_item(alert["id"]) if void else None
            if remaining:
                self.db.set_alert_remaining(alert["id"], remaining)
            else:
                await self._close_alert(alert, by, "void" if left == 0 else "done")

    async def _close_alert(self, alert, by: str, status: str = "done") -> None:
        self.db.close_alert(alert["id"], status, by, int(self.clock()))
        if alert["group_chat_id"] and alert["message_id"]:
            mark = "ℹ️ Не задержка" if status == "void" else "✅ Выполнено"
            try:
                await self.notifier.edit_alert(
                    alert["group_chat_id"], alert["message_id"], f"{alert['text']}\n\n{mark} ({by})"
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
        stavka_on = feature_on(self.db, "botwatch_enabled")
        need, _, cap = stavka_limits(self.db)
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
            if stavka_on and row["topic_id"] > 1 and stavka_running(
                    self.db.lead_row(row["chat_id"], row["topic_id"]), need, cap, now):
                continue                                # идёт ставка нового лида — это не задержка
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
        lines = [message_link(r["chat_id"], r["first_lead_msg_id"] or r["last_lead_msg_id"], r["topic_id"]) for r in rows]
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
        for r in rows:
            self._journal(now, r["chat_id"], r["topic_id"], r["first_lead_msg_id"] or r["last_lead_msg_id"], None,
                          "", "", "таймер", "alert")
        return True

    # ------------------------------------------------------------ напоминание «ставка»
    async def watch_bots(self, now: int | None = None) -> int:
        """Ставка нового лида не дошла до конца: к сроку пришло меньше N кружков. Одно напоминание «ставка»
        в «Проёб» (ссылки по папке + сотрудники папки). Это НЕ задержка: ставка продолжается, сообщения лида
        по-прежнему не считаются, пока не придут кружки (или не выйдет предел)."""
        now = int(now if now is not None else self.clock())
        if not feature_on(self.db, "botwatch_enabled") or self.clock() < self._watch_fail_until:
            return 0
        ws, we = self.window()
        if not is_work_time(now, self.tz, ws, we):
            return 0                                     # ночью не пишем — напомним в начале рабочего дня
        need, remind, _ = stavka_limits(self.db)
        since = max(self.db.get_int("botwatch_from"), now - 2 * 86400)
        rows = self.db.silent_bot_threads(since, need, now - remind)
        if not rows:
            return 0
        push_e, skip_e = emoji_lists(self.db)
        folders = {f["id"]: f for f in self.db.folders()}
        due: dict[int, list] = defaultdict(list)
        for r in rows:
            folder = folders.get(r["folder_id"])
            if folder is None or not folder["delay_on"] or classify(r["label"] or "", push_e, skip_e) == "skip":
                self.db.mark_watch_warned(r["chat_id"], r["topic_id"])      # папка без контроля / ⚡ / 💊
                continue
            if self.bet_counter is not None:
                key = (r["chat_id"], r["topic_id"])
                if self.clock() < self._recount_retry.get(key, 0):
                    continue                                                # эту нить недавно не удалось сверить
                found, offline = await self._bets_from_telegram(*key)
                if found is None:
                    if offline:                                             # Telegram недоступен — всё позже
                        self._watch_fail_until = self.clock() + 300
                        return 0
                    fails = self._recount_fails.get(key, 0) + 1          # не удалось именно для этой нити
                    self._recount_fails[key] = fails
                    self._recount_retry[key] = self.clock() + 300
                    if fails >= 6:                                          # ~30 мин: топик удалён / нет доступа
                        self.db.mark_watch_warned(*key)
                        self._recount_fails.pop(key, None)
                        self._recount_retry.pop(key, None)
                        await self.notifier.notify_admin(
                            "⚠️ Не удаётся проверить кружки ставки нового лида (топик удалён или у аккаунта нет "
                            f"доступа): {message_link(r['chat_id'], r['last_msg_id'], r['topic_id'])}",
                            key="recount-fail", min_interval=3600)
                    continue
                self._recount_fails.pop(key, None)
                self._recount_retry.pop(key, None)
                self._apply_bets(*key, found)
                if self.db.finish_stavka(*key, need) is not None:          # кружки на самом деле пришли
                    self._wait_after_stavka(*key)
                lead = self.db.lead_row(*key)
                if lead is not None and int(lead["bet_count"]) >= need:
                    continue
            due[folder["id"]].append(r)
        sent = 0
        for fid, items in due.items():
            folder = folders[fid]
            who = " ".join(mention(e["username"]) for e in self.db.employees(fid))
            for i in range(0, len(items), MAX_LINKS_PER_ALERT):
                chunk = items[i:i + MAX_LINKS_PER_ALERT]
                links = [message_link(r["chat_id"], r["last_msg_id"], r["topic_id"]) for r in chunk]
                text = "\n".join(links + [f"{who} {folder['name']} ставка".strip()])
                try:
                    await self.notifier.send_group_text(text)
                except Exception as exc:
                    log.warning("Ставка: не удалось написать в «Проёб»: %s", exc)
                    await self.notifier.notify_admin(
                        f"⚠️ У новых лидов не пришли кружки ставки ({len(chunk)}), а в «Проёб» написать не удалось.\n"
                        + "\n".join(links[:5]), key="botwatch-fail", min_interval=600)
                    self._watch_fail_until = self.clock() + 600          # не терять напоминания: повторим позже
                    return sent
                for r in chunk:
                    self.db.mark_watch_warned(r["chat_id"], r["topic_id"])
                    self._journal(now, r["chat_id"], r["topic_id"], r["last_msg_id"], None, "",
                                  f"кружков {r['bet_count']}", "таймер", "stavka_remind")
                sent += 1
        return sent
