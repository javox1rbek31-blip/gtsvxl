"""Построение ссылок на сообщения и чаты (только по ID, без текста сообщений)."""
from __future__ import annotations


def message_link(chat_id: int, msg_id: int, topic_id: int = 0) -> str:
    s = str(chat_id)
    if s.startswith("-100"):                 # супергруппа / канал
        if topic_id > 1:                      # топик форума: t.me/c/<чат>/<топик>/<сообщение>
            return f"https://t.me/c/{s[4:]}/{topic_id}/{msg_id}"
        return f"https://t.me/c/{s[4:]}/{msg_id}"
    if chat_id > 0:                           # личный чат
        return f"tg://openmessage?user_id={chat_id}&message_id={msg_id}"
    return f"tg://openmessage?chat_id={abs(chat_id)}&message_id={msg_id}"  # обычная группа


def parse_message_link(text: str) -> tuple[int, int, int] | None:
    """Ссылка на сообщение супергруппы → (chat_id, topic_id или 0, msg_id). Форматы:
    t.me/c/<чат>/<топик>/<сообщение>, t.me/c/<чат>/<сообщение>, t.me/c/<чат>/<сообщение>?thread=<топик>."""
    import re
    m = re.search(r"t\.me/c/(\d+)/(\d+)(?:/(\d+))?(?:\?(?:[^ ]*&)?thread=(\d+))?", text or "")
    if not m:
        return None
    chat = int("-100" + m.group(1))
    if m.group(3):
        return chat, int(m.group(2)), int(m.group(3))
    return chat, int(m.group(4) or 0), int(m.group(2))


def chat_link(chat_id: int) -> str:
    s = str(chat_id)
    if s.startswith("-100"):
        return f"https://t.me/c/{s[4:]}/1"
    if chat_id > 0:
        return f"tg://user?id={chat_id}"
    return f"tg://openmessage?chat_id={abs(chat_id)}"


def normalize_username(raw: str) -> str:
    """'@Ivan', 'https://t.me/Ivan', 'ivan' -> 'ivan' (нижний регистр, без @)."""
    s = (raw or "").strip()
    for prefix in ("https://t.me/", "http://t.me/", "t.me/"):
        if s.lower().startswith(prefix):
            s = s[len(prefix):]
    return s.lstrip("@").split("/")[0].split("?")[0].strip().lower()


def is_valid_username(username: str) -> bool:
    import re
    return bool(re.fullmatch(r"[a-z][a-z0-9_]{3,31}", username))


def mention(username: str) -> str:
    return "@" + username.lstrip("@")
