"""Отправка Push от имени аккаунта (Telethon): текст и вложения.

Отдельно от мониторинга: этот код ничего не читает и не слушает, только отправляет.
Используется основным юзерботом и аккаунтами Push (через класс-примесь PushSender).
Требует от «хозяина»: client, state, mute(chat_id, seconds).
"""
from __future__ import annotations

import asyncio


class SendAbort(RuntimeError):
    """Рассылку нужно остановить целиком (лимиты Telegram, нет подключения)."""


class SendSkip(RuntimeError):
    """Этому получателю отправить нельзя — идём дальше."""


class PushSender:
    async def send_media(self, chat_id: int, topic_id: int, media: dict, text: str = "") -> list[int]:
        """Push с вложением (голосовое, кружок, видео, фото, файл). Файл загружается ОДИН раз,
        дальше переиспользуется на сервере Telegram. Возвращает ID отправленных сообщений."""
        from telethon import errors
        import os

        if self.client is None or self.state != "online":
            raise SendAbort("Юзербот не подключён")
        path = media.get("path", "")
        if not path or not os.path.exists(path):
            raise SendAbort("Файл вложения не найден — загрузите медиа заново")
        kind = media["kind"]
        key = f"{path}:{os.path.getmtime(path)}"
        cache = getattr(self, "_media_cache", None)
        if cache is None:
            cache = self._media_cache = {}
        skip_names = ("UserIsBlockedError", "InputUserDeactivatedError", "UserPrivacyRestrictedError",
                      "PeerIdInvalidError", "ChatWriteForbiddenError", "ChatAdminRequiredError",
                      "UserBannedInChannelError", "TopicClosedError", "TopicDeletedError",
                      "ChatSendMediaForbiddenError", "ChatSendVoicesForbiddenError", "ChatSendGifsForbiddenError")
        skip_errs = tuple(getattr(errors, n) for n in skip_names if hasattr(errors, n))
        self.mute(chat_id, 120)
        ids: list[int] = []
        for attempt in (1, 2):
            try:
                if kind == "video_note" and text:
                    sent = await self.client.send_message(chat_id, text, reply_to=(topic_id or None),
                                                          parse_mode=None, link_preview=False)
                    ids.append(int(sent.id))
                    text = ""
                file = cache.get(key) or path
                kw = {}
                if file is path:
                    if kind == "voice":
                        kw["voice_note"] = True
                    elif kind == "video_note":
                        kw["video_note"] = True
                    elif kind == "video":
                        kw["supports_streaming"] = True
                    elif kind == "document":
                        kw["force_document"] = True
                msg = await self.client.send_file(
                    chat_id, file, caption=(text or None), reply_to=(topic_id or None), parse_mode=None, **kw)
                if getattr(msg, "media", None) is not None:
                    cache[key] = msg.media
                ids.append(int(msg.id))
                return ids
            except errors.FloodWaitError as exc:
                if exc.seconds > 300 or attempt == 2:
                    raise SendAbort(f"Telegram просит подождать {exc.seconds} с (FloodWait)") from exc
                await asyncio.sleep(exc.seconds + 1)
            except errors.PeerFloodError as exc:
                raise SendAbort("Telegram ограничил рассылку с аккаунта (PeerFlood)") from exc
            except skip_errs as exc:
                raise SendSkip(f"{type(exc).__name__}") from exc
            except ValueError as exc:
                raise SendSkip(f"ValueError: {str(exc)[:60]}") from exc
        raise SendAbort("не удалось отправить")

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
