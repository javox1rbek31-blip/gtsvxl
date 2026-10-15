"""Юзербот на Telethon: «глаза» системы.

Правило безопасности №1: из каждого сообщения берутся ТОЛЬКО метаданные
(id чата, id сообщения, время, id отправителя, признак исходящего, признак служебного).
Атрибуты с текстом сообщений не читаются и не логируются нигде в этом модуле.
"""
from __future__ import annotations

import asyncio
import logging
import time
from zoneinfo import ZoneInfo

from .config import Config
from .db import Database
from .features import feature_on
from .monitor import Monitor
from .notifier import Notifier

log = logging.getLogger("userbot")


def thread_of(m, is_forum: bool) -> int:
    """Номер топика сообщения: в форум-группе лид = топик, в обычном чате — 0."""
    rt = getattr(m, "reply_to", None)
    if rt is not None and getattr(rt, "forum_topic", False):
        return int(getattr(rt, "reply_to_top_id", None) or getattr(rt, "reply_to_msg_id", None) or 1)
    return 1 if is_forum else 0


class SendAbort(RuntimeError):
    """Рассылку нужно остановить целиком (лимиты Telegram, нет подключения)."""


class SendSkip(RuntimeError):
    """Этому получателю отправить нельзя — идём дальше."""


class LoginFlow:
    """Вход в аккаунт прямо из чата с ботом (на телефоне нет терминала)."""

    def __init__(self, api_id: int, api_hash: str):
        self.api_id = api_id
        self.api_hash = api_hash
        self.client = None
        self.phone = ""
        self.code_hash = ""

    async def start(self, phone: str) -> None:
        from telethon import TelegramClient
        from telethon.sessions import StringSession

        await self.cancel()
        self.client = TelegramClient(StringSession(), self.api_id, self.api_hash)
        await self.client.connect()
        self.phone = phone
        sent = await self.client.send_code_request(phone)
        self.code_hash = sent.phone_code_hash

    async def submit_code(self, code: str) -> str:
        """'ok' — вошли, 'password' — нужен пароль двухэтапной проверки."""
        from telethon import errors

        try:
            await self.client.sign_in(phone=self.phone, code=code, phone_code_hash=self.code_hash)
        except errors.SessionPasswordNeededError:
            return "password"
        return "ok"

    async def submit_password(self, password: str) -> None:
        await self.client.sign_in(password=password)

    def session_string(self) -> str:
        return self.client.session.save()

    async def cancel(self) -> None:
        if self.client is not None:
            try:
                await self.client.disconnect()
            except Exception:
                pass
            self.client = None


class Userbot:
    def __init__(self, cfg: Config, db: Database, monitor: Monitor, notifier: Notifier, tz: ZoneInfo):
        self.cfg = cfg
        self.db = db
        self.monitor = monitor
        self.notifier = notifier
        self.tz = tz
        self.client = None
        self.state = "starting"       # starting|disabled|no_session|connecting|online|offline|unauthorized
        self.me_label = ""
        self.last_reconcile = 0.0
        self.last_sync = 0.0
        self.last_event = 0.0
        self.missing_folders: list[str] = []
        self._wake = asyncio.Event()
        self._muted: dict[int, float] = {}
        self._was_down = False
        self.backfilling = False
        self.backfill_result: tuple[int, int] | None = None

    # ------------------------------------------------------------ управление
    def session(self) -> str:
        return self.db.get("userbot_session") or self.cfg.session_string

    def wake(self) -> None:
        self._wake.set()

    async def set_session(self, session: str) -> None:
        self.db.set("userbot_session", session)
        self.state = "connecting"
        self.wake()

    async def logout(self) -> None:
        self.db.set("userbot_session", "")
        self.state = "no_session"
        self.wake()

    def mute(self, chat_id: int, seconds: int = 60) -> None:
        """Исходящие в этот чат в ближайшие секунды — наша рассылка, не ответ менеджера."""
        self._muted[chat_id] = time.time() + seconds

    def _is_muted(self, chat_id: int) -> bool:
        until = self._muted.get(chat_id)
        if until is None:
            return False
        if until < time.time():
            self._muted.pop(chat_id, None)
            return False
        return True

    # ------------------------------------------------------------ основной цикл
    async def run(self) -> None:
        backoff = 5
        while True:
            if not (self.cfg.api_id and self.cfg.api_hash):
                self.state = "disabled"
                await self._sleep_or_wake(60)
                continue
            session = self.session()
            if not session:
                self.state = "no_session"
                await self._sleep_or_wake(60)
                continue
            try:
                started = time.monotonic()
                await self._serve(session)
                await self._disconnect()          # сессию сменили/удалили — закрыть старое соединение
                if time.monotonic() - started > 120:
                    backoff = 5
            except asyncio.CancelledError:
                await self._disconnect()
                raise
            except Exception as exc:
                self.state = "offline"
                self._was_down = True
                log.exception("Юзербот: ошибка подключения/работы")
                await self.notifier.notify_admin(
                    f"⚠️ Юзербот отключился: {type(exc).__name__}. Переподключаюсь автоматически.",
                    key="userbot-down", min_interval=900,
                )
                await self._disconnect()
                await self._sleep_or_wake(backoff)
                backoff = min(backoff * 2, 300)

    async def _sleep_or_wake(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass
        self._wake.clear()

    async def _disconnect(self) -> None:
        client, self.client = self.client, None
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                pass

    async def _serve(self, session: str) -> None:
        from telethon import TelegramClient, errors, events
        from telethon.sessions import StringSession

        self.state = "connecting"
        client = TelegramClient(
            StringSession(session), self.cfg.api_id, self.cfg.api_hash,
            connection_retries=-1, retry_delay=2, auto_reconnect=True, request_retries=5,
            flood_sleep_threshold=60,
        )
        self.client = client
        await client.connect()
        if not await client.is_user_authorized():
            self.state = "unauthorized"
            await self.notifier.notify_admin(
                "🔑 Сессия юзербота недействительна. Откройте Меню → Аккаунт юзербота и подключите аккаунт заново.",
                key="unauth", min_interval=3600,
            )
            await self._disconnect()
            await self._sleep_or_wake(300)
            return

        me = await client.get_me()
        self.me_label = " ".join(x for x in (getattr(me, "first_name", "") or "", f"@{me.username}" if getattr(me, "username", None) else "") if x) or str(me.id)
        client.add_event_handler(self._on_message, events.NewMessage())
        self.state = "online"
        log.info("Юзербот подключён: %s", self.me_label)
        if self._was_down:
            self._was_down = False
            await self.notifier.notify_admin(f"✅ Юзербот снова онлайн ({self.me_label}).")

        await self.refresh_all()
        failures = 0
        last_ping = last_refresh = time.monotonic()
        while True:
            await self._sleep_or_wake(15)
            if self.client is not client or self.session() != session:
                return                                   # сессию сменили/удалили — перезапуск цикла
            now = time.monotonic()
            if now - last_ping >= 60:
                last_ping = now
                try:
                    await asyncio.wait_for(client.get_me(), timeout=30)
                    failures = 0
                    if self.state != "online":
                        self.state = "online"
                except getattr(errors, "AuthKeyError", ()):
                    raise
                except Exception as exc:
                    failures += 1
                    log.warning("Пинг юзербота не прошёл (%s/3): %s", failures, type(exc).__name__)
                    if failures >= 3:
                        raise ConnectionError("нет ответа от Telegram") from exc
            if now - last_refresh >= self.cfg.reconcile_minutes * 60:
                last_refresh = now
                await self.refresh_all()

    # ------------------------------------------------------------ события
    async def _on_message(self, event) -> None:
        try:
            chat_id = event.chat_id
            if chat_id is None or not self.monitor.is_monitored(chat_id):
                return
            m = event.message
            if m.out and self._is_muted(chat_id):
                return
            forum = self.monitor.is_forum(chat_id)
            topic = thread_of(m, forum)
            if forum and (getattr(m, "action", None) is not None or (
                    topic > 1 and self.db.one("SELECT 1 FROM topics WHERE chat_id=? AND topic_id=?", (chat_id, topic)) is None)):
                self._schedule_topic_sync(chat_id)       # новый/изменённый топик: иконка-статус подтянется сразу
            if forum and topic == 1:
                return                                   # «General» форума — не лид
            self.last_event = time.time()
            await self.monitor.handle_message(
                chat_id, topic, int(m.id), int(m.date.timestamp()), m.sender_id, bool(m.out),
                getattr(m, "action", None) is not None,
            )
        except Exception:
            log.exception("Ошибка обработки сообщения (метаданные)")

    def _schedule_topic_sync(self, chat_id: int) -> None:
        """Быстрое обновление топиков чата (не чаще раза в 20 с на чат)."""
        pending = getattr(self, "_topic_sync", None)
        if pending is None:
            pending = self._topic_sync = {}
        if chat_id in pending:
            return
        pending[chat_id] = True

        async def job() -> None:
            try:
                await asyncio.sleep(4)                   # дать Telegram применить иконку/название
                peer = await self.client.get_input_entity(chat_id)
                await self.sync_topics(chat_id, peer)
                await asyncio.sleep(16)
            except Exception:
                log.info("Быстрое обновление топиков чата %s не удалось", chat_id)
            finally:
                pending.pop(chat_id, None)

        asyncio.create_task(job())

    # ------------------------------------------------------------ синхронизация
    async def refresh_all(self) -> None:
        for step in (self.sync_folders, self.resolve_employees, self.reconcile, self.maybe_backfill):
            try:
                await step()
            except Exception:
                log.exception("Шаг обновления %s завершился ошибкой", step.__name__)

    async def sync_folders(self) -> tuple[int, int]:
        """Подтянуть чаты из папок Telegram (по совпадению названия) в наши папки."""
        from telethon import utils
        from telethon.tl.functions.messages import GetDialogFiltersRequest

        if self.client is None:
            return 0, 0
        res = await self.client(GetDialogFiltersRequest())
        filters = getattr(res, "filters", res)
        by_title: dict[str, set[int]] = {}
        for f in filters:
            title = getattr(f, "title", None)
            title = getattr(title, "text", title)
            if not isinstance(title, str):
                continue
            peers = list(getattr(f, "include_peers", None) or []) + list(getattr(f, "pinned_peers", None) or [])
            ids: set[int] = set()
            for p in peers:
                try:
                    ids.add(int(utils.get_peer_id(p)))
                except Exception:
                    continue
            by_title.setdefault(title.strip().casefold(), set()).update(ids)

        added = removed = 0
        missing: list[str] = []
        for folder in self.db.folders():
            ids = by_title.get(folder["name"].strip().casefold())
            if ids is None:
                missing.append(folder["name"])
                continue
            a, r = self.db.sync_folder_chats(folder["id"], {i: "" for i in ids})
            added += a
            removed += r
        self.missing_folders = missing
        self.last_sync = time.time()
        self.monitor.reload()
        return added, removed

    async def resolve_employees(self) -> int:
        """Определить числовые ID сотрудников по @username."""
        if self.client is None:
            return 0
        done = 0
        for row in self.db.unresolved_employees():
            uname = row["username"]
            try:
                ent = await self.client.get_entity(uname)
                self.db.set_employee_user_id(uname, int(ent.id))
                done += 1
            except Exception as exc:
                log.info("Не удалось определить @%s: %s", uname, type(exc).__name__)
            await asyncio.sleep(1)
        if done:
            self.monitor.reload()
        return done

    async def reconcile(self) -> None:
        """Сверка с реальностью: последнее сообщение каждой нити (только метаданные)."""
        if self.client is None:
            return
        self.monitor.reload()
        wanted = self.db.chat_ids()
        if not wanted:
            self.last_reconcile = time.time()
            return
        forums: list[tuple[int, object]] = []
        async for d in self.client.iter_dialogs(limit=None):
            if d.id not in wanted:
                continue
            if d.name:
                self.db.set_chat_title(d.id, d.name)
            is_forum = bool(getattr(d.entity, "forum", False))
            self.db.set_forum(d.id, is_forum)
            if is_forum:
                forums.append((d.id, d.input_entity))
                continue
            m = d.message
            if m is None or self.db.is_ignored(d.id, int(m.id)):
                continue
            await self.monitor.reconcile_top(
                d.id, 0, int(m.id), int(m.date.timestamp()), m.sender_id, bool(m.out),
                getattr(m, "action", None) is not None,
            )
        self.monitor.reload()
        for chat_id, peer in forums:
            try:
                await self.sync_topics(chat_id, peer)
            except Exception:
                log.exception("Не удалось получить топики чата %s", chat_id)
        self.last_reconcile = time.time()

    async def _icon_chars(self, doc_ids: set[int]) -> dict[int, str]:
        """id кастомного эмодзи (иконка топика) -> обычный символ (🔥 ⚡ 💊 …)."""
        from telethon.tl.functions.messages import GetCustomEmojiDocumentsRequest

        known = self.db.emoji_chars(doc_ids)
        missing = [i for i in doc_ids if i not in known]
        for i in range(0, len(missing), 100):
            docs = await self.client(GetCustomEmojiDocumentsRequest(document_id=missing[i:i + 100]))
            for doc in docs:
                for attr in getattr(doc, "attributes", []):
                    if type(attr).__name__ == "DocumentAttributeCustomEmoji":
                        ch = (getattr(attr, "alt", "") or "").replace("\ufe0f", "")
                        if ch:
                            self.db.set_emoji_char(int(doc.id), ch)
                            known[int(doc.id)] = ch
        return known

    async def sync_topics(self, chat_id: int, peer) -> int:
        """Топики форум-группы: название + иконка (статус лида) и состояние последнего сообщения."""
        from telethon.tl.functions.channels import GetForumTopicsRequest

        offset_date, offset_id, offset_topic, total = None, 0, 0, 0
        while True:
            res = await self.client(GetForumTopicsRequest(
                channel=peer, offset_date=offset_date, offset_id=offset_id, offset_topic=offset_topic, limit=100))
            topics = [t for t in res.topics if hasattr(t, "title")]       # без удалённых
            if not res.topics:
                break
            msgs = {int(m.id): m for m in res.messages}
            icons = await self._icon_chars({int(t.icon_emoji_id) for t in topics if getattr(t, "icon_emoji_id", None)})
            for t in topics:
                if int(t.id) == 1:
                    continue                                              # «General»
                icon = icons.get(int(t.icon_emoji_id), "") if getattr(t, "icon_emoji_id", None) else ""
                self.db.upsert_topic(chat_id, int(t.id), t.title or "", icon)
                m = msgs.get(int(t.top_message))
                if m is not None and not self.db.is_ignored(chat_id, int(m.id)):
                    await self.monitor.reconcile_top(
                        chat_id, int(t.id), int(m.id), int(m.date.timestamp()), m.sender_id, bool(m.out),
                        getattr(m, "action", None) is not None,
                    )
                total += 1
            last = res.topics[-1]
            last_msg = msgs.get(int(getattr(last, "top_message", 0)))
            if len(res.topics) < 100 or last_msg is None:
                break
            offset_date, offset_id, offset_topic = last_msg.date, int(last.top_message), int(last.id)
            await asyncio.sleep(0.5)
        return total

    async def maybe_backfill(self) -> None:
        """Первый подсчёт лидов за историю — когда группа «Проёб» уже задана."""
        if self.db.get("proyob_group_id") and self.db.get("leads_backfill_ts") == "0":
            self.start_backfill()

    def start_backfill(self) -> bool:
        if self.backfilling or self.client is None:
            return False
        self.backfilling = True
        asyncio.create_task(self._backfill_task())
        return True

    async def _backfill_task(self) -> None:
        try:
            self.backfill_result = await self.backfill_leads()
        except Exception:
            log.exception("Пересчёт лидов не удался")
        finally:
            self.backfilling = False

    async def backfill_leads(self) -> tuple[int, int]:
        """Пересчёт лидов с даты начала учёта: просматриваем историю контролируемых чатов.
        Берутся только метаданные (id, дата, отправитель); текст не используется и не сохраняется."""
        if self.client is None or not feature_on(self.db, "leads_enabled"):
            return 0, 0
        since = self.monitor.track_from_ts()
        employees = self.db.employee_user_ids()
        forums = self.db.forum_ids()
        chats = found = 0
        for cid in sorted(self.db.chat_ids()):
            is_forum = cid in forums
            try:
                async for m in self.client.iter_messages(cid, limit=None):   # от новых к старым
                    ts = int(m.date.timestamp())
                    if ts < since:
                        break
                    if getattr(m, "action", None) is not None or m.out:
                        continue
                    if (m.sender_id is not None and m.sender_id in employees) or self.db.is_ignored(cid, int(m.id)):
                        continue
                    topic = thread_of(m, is_forum)
                    if is_forum and topic == 1:
                        continue
                    self.db.touch_lead(cid, topic, m.sender_id, int(m.id), ts)
                    found += 1
            except Exception as exc:
                log.info("Чат %s пропущен при пересчёте лидов: %s", cid, type(exc).__name__)
            chats += 1
            await asyncio.sleep(0.5)
        self.db.set("leads_backfill_ts", str(int(time.time())))
        log.info("Пересчёт лидов: чатов %s, сообщений лидов %s", chats, found)
        return chats, found

    # ------------------------------------------------------------ сервис для панели
    async def search_dialogs(self, query: str, limit: int = 15) -> list[tuple[int, str, str]]:
        """Поиск чата по названию → (id, название, тип). Нужен, т.к. Telegram не отдаёт ID группы при пересылке."""
        if self.client is None or self.state != "online":
            raise RuntimeError("Юзербот не подключён")
        q = query.strip().casefold()
        out: list[tuple[int, str, str]] = []
        async for d in self.client.iter_dialogs(limit=None):
            if q and q not in (d.name or "").casefold():
                continue
            kind = "канал" if (d.is_channel and not d.is_group) else "группа" if d.is_group else "личный чат"
            out.append((int(d.id), d.name or "(без названия)", kind))
            if len(out) >= limit:
                break
        return out

    async def send_text(self, chat_id: int, topic_id: int, text: str) -> int:
        """Push от имени аккаунта: в топик (reply_to=topic) или в обычный чат. Бросает SendAbort/SendSkip."""
        from telethon import errors

        if self.client is None or self.state != "online":
            raise SendAbort("Юзербот не подключён")
        skip_names = ("UserIsBlockedError", "InputUserDeactivatedError", "UserPrivacyRestrictedError",
                      "PeerIdInvalidError", "ChatWriteForbiddenError", "ChatAdminRequiredError",
                      "UserBannedInChannelError", "TopicClosedError", "TopicDeletedError")
        skip_errs = tuple(getattr(errors, n) for n in skip_names if hasattr(errors, n)) + (ValueError,)
        self.mute(chat_id, 90)
        for attempt in (1, 2):
            try:
                msg = await self.client.send_message(
                    chat_id, text, reply_to=(topic_id or None), parse_mode=None, link_preview=False)
                return int(msg.id)
            except errors.FloodWaitError as exc:
                if exc.seconds > 300 or attempt == 2:
                    raise SendAbort(f"Telegram просит подождать {exc.seconds} с (FloodWait)") from exc
                await asyncio.sleep(exc.seconds + 1)
            except errors.PeerFloodError as exc:
                raise SendAbort("Telegram ограничил рассылку с аккаунта (PeerFlood)") from exc
            except ValueError as exc:
                # чат не в кэше сессии: один раз обновляем список диалогов и пробуем снова
                if attempt == 1 and not getattr(self, "_dialogs_refreshed", False):
                    self._dialogs_refreshed = True
                    try:
                        await self.client.get_dialogs(limit=None)
                        continue
                    except Exception:
                        pass
                raise SendSkip(f"ValueError: {str(exc)[:80]}") from exc
            except skip_errs as exc:
                raise SendSkip(f"{type(exc).__name__}: {str(exc)[:60]}") from exc
        raise SendAbort("не удалось отправить")
