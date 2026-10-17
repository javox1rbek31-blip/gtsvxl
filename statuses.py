"""Статус лида = эмодзи в начале имени чата/контакта (🔥 горячий, ⚡ и 💊 — исключённые).

Используется только название чата (метаданные), не текст сообщений.
"""
from __future__ import annotations

_IGNORED = {"️", "‍", ",", ";"}


def parse_emojis(text: str) -> list[str]:
    """'🔥 ⚡️, 💊' -> ['🔥', '⚡', '💊']. Каждый символ без пробелов — отдельный эмодзи."""
    out: list[str] = []
    for ch in text or "":
        if ch.isspace() or ch in _IGNORED:
            continue
        if ch not in out:
            out.append(ch)
    return out


def _norm(title: str) -> str:
    return "".join(ch for ch in (title or "").lstrip() if ch not in ("️", "‍"))


def starts_with_any(title: str, emojis: list[str]) -> bool:
    t = _norm(title)
    return any(t.startswith(e) for e in emojis)


def classify(title: str, push_emojis: list[str], skip_emojis: list[str]) -> str:
    """'skip' | 'push' | 'none'. Исключение (⚡/💊) сильнее всего."""
    if starts_with_any(title, skip_emojis):
        return "skip"
    if not push_emojis or starts_with_any(title, push_emojis):   # список пуст = все, кроме исключённых
        return "push"
    return "none"


# Служебные сообщения нового чата от бота-посредника: это НЕ сообщение клиента, отвечать на них не нужно.
OPENING_PHRASES = ("новый чат", "автоставки запущены")


def looks_like_opening(head: str) -> bool:
    """head — только начало (до 40 символов) сообщения; весь текст сюда не передаётся и нигде не хранится."""
    h = (head or "")[:40].lower()
    return any(p in h for p in OPENING_PHRASES)
