"""Time and timezone helper utilities."""

import os
import re
from datetime import datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from data.database import parse_db_datetime

TZ = ZoneInfo(os.getenv("APP_TIMEZONE", "Europe/Moscow"))
_DAY_PART_HOURS = {"morning": 9, "day": 14, "evening": 20}
TIME_TEXT_EXAMPLES = "«завтра 11», «24/09 в 9», «через 2 дня»"


def get_app_timezone() -> ZoneInfo:
    return ZoneInfo(os.getenv("APP_TIMEZONE", "Europe/Moscow"))


def parse_timezone(tz_input: str | None) -> tuple[tzinfo | None, str | None]:
    """Parses IANA timezone name (e.g. Europe/Moscow) or offset (e.g. UTC+3, +07:00, -5).
    Returns (tz_object, canonical_name) or (None, None).
    """
    if not tz_input:
        return None, None
    text = tz_input.strip()
    if text.upper() in ("UTC", "GMT", "Z"):
        return timezone.utc, "UTC"

    # Try IANA name
    try:
        return ZoneInfo(text), text
    except (ZoneInfoNotFoundError, ValueError):
        pass

    # Try UTC / GMT offset: UTC+3, UTC+03:00, +5, -4:30
    m = re.match(r"^(?:UTC|GMT)?\s*([+-])\s*(\d{1,2})(?::?(\d{2}))?$", text, re.IGNORECASE)
    if m:
        sign = 1 if m.group(1) == "+" else -1
        hours = int(m.group(2))
        mins = int(m.group(3) or 0)
        if 0 <= mins < 60:
            total_mins = hours * 60 + mins
            # Earth timezone offsets: -12:00 to +14:00
            if (sign == 1 and total_mins <= 14 * 60) or (sign == -1 and total_mins <= 12 * 60):
                offset = timedelta(minutes=total_mins) * sign
                canonical = f"UTC{m.group(1)}{hours:02d}:{mins:02d}"
                tz = timezone(offset, name=canonical)
                return tz, canonical

    return None, None


def local_now() -> datetime:
    return datetime.now(TZ)


def ai_today_key() -> str:
    """Ключ дня для учёта дневного лимита ИИ в общем часовом поясе приложения."""
    return local_now().strftime("%Y-%m-%d")


def display_db_datetime(value: str, tz: tzinfo | None = None) -> datetime:
    return parse_db_datetime(value).astimezone(tz or TZ)


def format_when(moment: datetime, now: datetime) -> str:
    day = {0: "сегодня", 1: "завтра", 2: "послезавтра"}.get((moment.date() - now.date()).days)
    return f"{day or moment.strftime('%d.%m')} в {moment.strftime('%H:%M')}"


def get_quick_scheduled_time(
    action: str, now: datetime, hours: dict[str, int] | None = None
) -> tuple[datetime | None, str]:
    day_hours = hours or _DAY_PART_HOURS
    if action in day_hours:
        # The nearest such moment: «Утро» at 02:00 means this morning, not tomorrow's.
        scheduled_time = now.replace(hour=day_hours[action], minute=0, second=0, microsecond=0)
        if scheduled_time <= now:
            scheduled_time += timedelta(days=1)
        return scheduled_time, f"на {format_when(scheduled_time, now)}"

    if action == "hour":
        return now + timedelta(hours=1), "через час"

    if action == "now":
        return now + timedelta(hours=3), "через 3 часа"

    return None, ""


def describe_quick_times(now: datetime, hours: dict[str, int] | None = None) -> str:
    """One line that tells what the day-part buttons resolve to right now."""
    emojis = {"morning": "🌅", "day": "☀️", "evening": "🌙"}
    day_hours = hours or _DAY_PART_HOURS
    return " · ".join(
        f"{emojis[action]} {format_when(get_quick_scheduled_time(action, now, day_hours)[0], now)}"
        for action in day_hours
    )


def build_time_selection_keyboard(prefix: str, *, back_callback: str | None = None) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="🌅 Утро", callback_data=f"{prefix}_morning"),
            InlineKeyboardButton(text="☀️ День", callback_data=f"{prefix}_day"),
            InlineKeyboardButton(text="🌙 Вечер", callback_data=f"{prefix}_evening"),
        ],
        [
            InlineKeyboardButton(text="⏱ 1 час", callback_data=f"{prefix}_hour"),
            InlineKeyboardButton(text="⏱ 3 часа", callback_data=f"{prefix}_now"),
            InlineKeyboardButton(text="🗓 Дата…", callback_data=f"{prefix}_custom"),
        ],
    ]
    if back_callback:
        rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=back_callback)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_standard_message_keyboard() -> InlineKeyboardMarkup:
    kb = build_time_selection_keyboard("time").inline_keyboard
    kb.append([InlineKeyboardButton(text="📁 В закладки (База знаний)", callback_data="bookmark_setup")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

