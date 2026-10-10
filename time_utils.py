"""Time and timezone helper utilities."""

import os
import re
from datetime import datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from data.database import parse_db_datetime

TZ = ZoneInfo(os.getenv("APP_TIMEZONE", "Europe/Moscow"))


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
        if 0 <= hours <= 14 and 0 <= mins < 60:
            offset = timedelta(hours=hours, minutes=mins) * sign
            canonical = f"UTC{m.group(1)}{hours:02d}:{mins:02d}"
            tz = timezone(offset, name=canonical)
            return tz, canonical

    return None, None


def local_now() -> datetime:
    return datetime.now(TZ)


def display_db_datetime(value: str, tz: tzinfo | None = None) -> datetime:
    return parse_db_datetime(value).astimezone(tz or TZ)
