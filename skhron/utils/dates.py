"""Отображение дат в локальном часовом поясе.

В БД всё хранится в UTC (naive). Пояс для показа берётся из env
DISPLAY_TZ (по умолчанию Europe/Moscow); если tzdata недоступна —
фиксированный UTC+3.
"""

import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


def _resolve_tz():
    name = os.environ.get("DISPLAY_TZ", "Europe/Moscow")
    try:
        return ZoneInfo(name)
    except Exception:
        return timezone(timedelta(hours=3))


DISPLAY_TZ = _resolve_tz()


def fmt_date(dt: datetime) -> str:
    """UTC из БД -> локальная дата «31.12.2026»."""
    return dt.replace(tzinfo=timezone.utc).astimezone(DISPLAY_TZ).strftime("%d.%m.%Y")


def fmt_datetime(dt: datetime) -> str:
    """UTC из БД -> локальные дата и время «31.12.2026 23:59»."""
    return (
        dt.replace(tzinfo=timezone.utc)
        .astimezone(DISPLAY_TZ)
        .strftime("%d.%m.%Y %H:%M")
    )


def now_local_str() -> str:
    return datetime.now(DISPLAY_TZ).strftime("%d.%m.%Y %H:%M")
