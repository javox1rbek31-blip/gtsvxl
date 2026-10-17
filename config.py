"""Конфигурация из переменных окружения + значения настроек по умолчанию."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class ConfigError(RuntimeError):
    pass


# Значения по умолчанию. Все они меняются из админ-панели (хранятся в SQLite).
DEFAULT_SETTINGS: dict[str, str] = {
    "limit_minutes": "20",          # лимит времени ответа
    "work_start": "10:00",          # начало рабочего дня (Ташкент)
    "work_end": "23:00",            # конец рабочего дня (Ташкент)
    "proyob_group_id": "",          # ID группы «Проёб»
    "track_from": "2026-08-26",     # лиды, написавшие раньше этой даты, игнорируются
    "timer_from": "first",          # first = от первого неотвеченного, last = от последнего
    "break_minutes": "30",          # длительность перерыва
    "breaks_per_day": "3",          # перерывов в день
    "bot_enabled": "1",             # главный выключатель: 0 = бот ничего не делает (панель работает)
    "delay_enabled": "0",           # контроль задержек (включается при настройке группы)
    "breaks_enabled": "1",          # контроль перерывов
    "leads_enabled": "1",           # учёт лидов
    "push_enabled": "1",            # разрешить ручной Push
    "alerts_from": "0",             # задержки считаются только по сообщениям после этого момента
    "push_emojis": "",            # Push только лидам, чьё имя начинается с этих эмодзи
    "skip_emojis": "⚡ 💊",         # такие лиды не учитываются в задержках и не получают Push
    "push_limit": "50",             # сколько человек за один Push (0 = все подходящие)
    "push_speed": "normal",         # fast 1–2 с · normal 2–3 с · slow 3–5 с
    "push_newlead_minutes": "60",   # не слать Push новым лидам первые N минут (автоставки)
    "alert_via": "bot",             # кто пишет в «Проёб»: bot | main | acc:<id>
    "lang": "ru",                   # язык панели: ru / uz
    "push_quiet_from": "",          # общее «тихое время» Push, например 21:00
    "push_quiet_to": "",            # …до 09:00
    "push_repeat_minutes": "0",     # не слать повторно тем, кому слали за последние N минут (0 = нет)
    "push_folders": "",             # id папок для Push через запятую; пусто = все с включённым Push
    "push_slot": "all",             # all | day | evening
    "newlead_enabled": "1",
    "newlead_minutes": "120",
    "newlead_from": "0",
    "pay_enabled": "0",
    "pay_minutes": "15",
    "pay_text": "клиенту отправили реквизиты, но нет чека/ответа — напишите ему и напомните об оплате",
    "midbot_id": "0",
    "midbot_name": "",
    "push_media": "",              # JSON текущего медиа Push
    "weekly_enabled": "1",
    "weekly_last": "",
    "report_enabled": "1", "report_time": "23:30", "report_last": "",
    "backup_enabled": "1", "backup_last": "0",
    "health_enabled": "1",
    "push_idle_minutes": "0",      # не писать лидам, активным недавно (минут; 0 = выкл)
    "opening_enabled": "1",        # «Новый чат» / «Автоставки запущены» — служебные, не сообщение клиента
    "botwatch_enabled": "0",       # контроль автоставок
    "botwatch_from": "0",
    "botwatch_count": "3",         # сколько автоставок после «Новый чат»
    "botwatch_wait": "20",         # минут тишины до предупреждения
    "push_from": "",                # Push лидам, появившимся с этой даты (пусто = с даты начала учёта)
    "push_to": "",                  # ...по эту дату включительно (пусто = по сегодня)
    "push_mode": "userbot",         # userbot = с вашего аккаунта, bot = через Bot API (только нажавшим «Старт»)
    "push_text": "",                # сохранённый текст Push
    "leads_backfill_ts": "0",       # когда последний раз пересчитывали лидов за историю
    "userbot_session": "",          # StringSession юзербота (создаётся через панель)
}


@dataclass(frozen=True)
class Config:
    bot_token: str
    admin_ids: tuple[int, ...]
    api_id: int
    api_hash: str
    session_string: str
    data_dir: Path
    tz_name: str
    reconcile_minutes: int
    log_level: str

    @property
    def db_path(self) -> Path:
        return self.data_dir / "tgcontrol.sqlite3"


def _parse_ids(raw: str) -> tuple[int, ...]:
    ids: list[int] = []
    for part in raw.replace(";", ",").replace(" ", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.append(int(part))
        except ValueError as exc:
            raise ConfigError(f"ADMIN_IDS: «{part}» не является числом") from exc
    return tuple(ids)


def load_config(env: dict[str, str] | None = None) -> Config:
    e = os.environ if env is None else env

    token = e.get("BOT_TOKEN", "").strip()
    if not token:
        raise ConfigError("Не задана переменная BOT_TOKEN (токен из @BotFather)")

    admin_ids = _parse_ids(e.get("ADMIN_IDS", ""))
    if not admin_ids:
        raise ConfigError("Не задана переменная ADMIN_IDS (ваш числовой Telegram ID, узнать у @userinfobot)")

    api_id_raw = e.get("API_ID", "").strip()
    try:
        api_id = int(api_id_raw) if api_id_raw else 0
    except ValueError as exc:
        raise ConfigError("API_ID должен быть числом (my.telegram.org)") from exc

    data_dir = Path(e.get("DATA_DIR", "./data")).expanduser()
    data_dir.mkdir(parents=True, exist_ok=True)

    try:
        reconcile = max(2, int(e.get("RECONCILE_MINUTES", "10")))
    except ValueError:
        reconcile = 10

    return Config(
        bot_token=token,
        admin_ids=admin_ids,
        api_id=api_id,
        api_hash=e.get("API_HASH", "").strip(),
        session_string=e.get("SESSION_STRING", "").strip(),
        data_dir=data_dir,
        tz_name=e.get("TZ_NAME", "Asia/Tashkent").strip() or "Asia/Tashkent",
        reconcile_minutes=reconcile,
        log_level=e.get("LOG_LEVEL", "INFO").strip().upper() or "INFO",
    )
