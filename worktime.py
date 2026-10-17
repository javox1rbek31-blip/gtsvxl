"""Рабочее время: подсчёт «рабочих» секунд между двумя моментами.

Таймер задержки считает только время внутри рабочего окна (по умолчанию 10:00–23:00
по Ташкенту). Сообщение, пришедшее ночью, начинает отсчёт в 10:00 следующего дня.
"""
from __future__ import annotations

import re
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

_HHMM = re.compile(r"^\s*(\d{1,2})(?:[:.](\d{2}))?\s*$")


def parse_hhmm(value: str) -> time:
    """'10:00', '10.00', '9:30' или '10' -> time. Бросает ValueError при ошибке."""
    m = _HHMM.match(value or "")
    if not m:
        raise ValueError("Ожидается время в формате ЧЧ:ММ, например 10:00")
    hour, minute = int(m.group(1)), int(m.group(2) or 0)
    if hour > 23 or minute > 59:
        raise ValueError("Часы 0–23, минуты 0–59")
    return time(hour, minute)


def fmt_hhmm(t: time) -> str:
    return f"{t.hour:02d}:{t.minute:02d}"


def is_work_time(ts: float, tz: ZoneInfo, ws: time, we: time) -> bool:
    local = datetime.fromtimestamp(ts, tz)
    return ws <= local.time().replace(tzinfo=None) < we


def work_seconds_between(start_ts: float, end_ts: float, tz: ZoneInfo, ws: time, we: time) -> int:
    """Сколько секунд из интервала [start_ts, end_ts] приходится на рабочее окно."""
    if end_ts <= start_ts:
        return 0
    start_ts = max(start_ts, end_ts - 400 * 86400)  # защита от бесконечных циклов
    start = datetime.fromtimestamp(start_ts, tz)
    end = datetime.fromtimestamp(end_ts, tz)

    total = 0.0
    day = start.date()
    last = end.date()
    while day <= last:
        w0 = datetime.combine(day, ws, tzinfo=tz)
        w1 = datetime.combine(day, we, tzinfo=tz)
        lo = max(w0, start)
        hi = min(w1, end)
        if hi > lo:
            total += (hi - lo).total_seconds()
        day += timedelta(days=1)
    return int(total)


def local_day_start(ts: float, tz: ZoneInfo) -> int:
    """Unix-время начала локальных суток для момента ts."""
    local = datetime.fromtimestamp(ts, tz)
    midnight = datetime.combine(local.date(), time(0, 0), tzinfo=tz)
    return int(midnight.timestamp())


def local_date_str(ts: float, tz: ZoneInfo) -> str:
    return datetime.fromtimestamp(ts, tz).strftime("%Y-%m-%d")


def fmt_local(ts: float, tz: ZoneInfo, pattern: str = "%d.%m %H:%M") -> str:
    return datetime.fromtimestamp(ts, tz).strftime(pattern)


def parse_date(value: str) -> datetime:
    """'2026-08-26' или '26.08.2026' -> datetime (naive). ValueError при ошибке."""
    value = (value or "").strip()
    for pattern in ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y"):
        try:
            return datetime.strptime(value, pattern)
        except ValueError:
            continue
    raise ValueError("Ожидается дата, например 26.08.2026")


def parse_duration_minutes(value: str) -> int:
    """'30' -> 30 мин; '30м'/'30m' -> 30; '2ч'/'2h' -> 120; '1д'/'1d' -> 1440; '0'/'выкл' -> 0."""
    import re
    v = (value or "").strip().lower().replace(",", ".")
    if v in ("", "0", "выкл", "off", "нет"):
        return 0
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([a-zа-я]*)", v)
    if not m:
        raise ValueError("bad duration")
    num, unit = float(m.group(1)), m.group(2)
    if unit in ("", "m", "min", "м", "мин"):
        mult = 1
    elif unit in ("h", "ч", "час", "часа", "часов"):
        mult = 60
    elif unit in ("d", "д", "дн", "день", "дня", "дней"):
        mult = 1440
    else:
        raise ValueError("bad unit")
    return int(round(num * mult))
