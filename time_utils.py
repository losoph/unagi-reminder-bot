"""Time and timezone helper utilities."""

import os
from datetime import datetime
from zoneinfo import ZoneInfo

from data.database import parse_db_datetime

TZ = ZoneInfo(os.getenv("APP_TIMEZONE", "Europe/Moscow"))


def get_app_timezone() -> ZoneInfo:
    return ZoneInfo(os.getenv("APP_TIMEZONE", "Europe/Moscow"))


def local_now() -> datetime:
    return datetime.now(TZ)


def display_db_datetime(value: str) -> datetime:
    return parse_db_datetime(value).astimezone(TZ)
