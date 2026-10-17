"""Выключатели функций бота (главный + по каждой функции) и сброс настроек."""
from __future__ import annotations

import time

from .config import DEFAULT_SETTINGS
from .db import Database
from .statuses import parse_emojis

# ключ настройки -> (название, пояснение)
FEATURES: dict[str, tuple[str, str]] = {
    "delay_enabled": ("⏱ Контроль задержек", "уведомления в «Проёб» о неотвеченных лидах"),
    "breaks_enabled": ("☕ Контроль перерывов", "атайду / перерыв / тут"),
    "leads_enabled": ("📅 Учёт лидов", "подсчёт и список лидов"),
    "botwatch_enabled": ("🤖 Контроль автоставок", "в «Проёб»: если после нового чата не пришли автоставки"),
    "opening_enabled": ("🆕 Распознавание «Новый чат»", "служебные сообщения нового чата не считаются сообщением клиента (читается только их начало, текст не сохраняется)"),
    "report_enabled": ("📊 Ежедневный отчёт", "итоги дня вам в личку"),
    "weekly_enabled": ("📅 Итог недели в «Проёб»", "воскресенье 21:00 + по команде «неделя» в группе"),
    "pay_enabled": ("💳 Напоминание об оплате", "в «Проёб» сотруднику: после «реквизиты отправлены» нет ответа/чека"),
    "newlead_enabled": ("🆕 Новый лид без контакта", "в «Проёб»: сегодняшний лид молчит дольше N минут"),
    "backup_enabled": ("💾 Авто-копия настроек", "раз в неделю файл вам в личку"),
    "health_enabled": ("🩺 Контроль работоспособности", "предупредить, если юзербот отключился"),
    "push_enabled": ("📣 Push-рассылки", "ручная рассылка горячим лидам"),
}

KEEP_ON_RESET = {"userbot_session", "proyob_group_id", "push_text", "push_media", "report_last", "weekly_last", "backup_last", "panel_kb_sent"}


def master_on(db: Database) -> bool:
    return db.get("bot_enabled") == "1"


def feature_on(db: Database, key: str) -> bool:
    """Функция работает, только если включён и главный выключатель, и сама функция."""
    return master_on(db) and db.get(key) == "1"


def set_feature(db: Database, key: str, on: bool, now: int | None = None) -> None:
    """Включение контроля задержек начинает отсчёт «с чистого листа» — старые
    неотвеченные сообщения не порождают уведомлений."""
    now = int(time.time()) if now is None else now
    db.set(key, "1" if on else "0")
    if on and key in ("delay_enabled", "bot_enabled"):
        db.set("alerts_from", str(now))
    if on and key == "newlead_enabled":
        db.set("newlead_from", str(now))
    if on and key == "botwatch_enabled":
        db.set("botwatch_from", str(now))                 # только новые чаты, старые не проверяем


def reset_settings(db: Database, now: int | None = None) -> None:
    """Вернуть настройки к заводским (папки, сотрудники, чаты, лиды, сессия и группа сохраняются)."""
    for key, value in DEFAULT_SETTINGS.items():
        if key not in KEEP_ON_RESET:
            db.set(key, value)
    db.set("delay_enabled", "1" if db.get("proyob_group_id") else "0")
    db.set("alerts_from", str(int(time.time()) if now is None else now))


def emoji_lists(db: Database) -> tuple[list[str], list[str]]:
    return parse_emojis(db.get("push_emojis")), parse_emojis(db.get("skip_emojis"))


SPEED_ORDER = ["slow", "normal", "fast"]
SPEED_NAMES = {"fast": "быстро 1–2 с", "normal": "обычно 2–3 с", "slow": "осторожно 3–5 с"}
