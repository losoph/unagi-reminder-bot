from datetime import datetime
from zoneinfo import ZoneInfo

from config import APP_TIMEZONE, DB_TIMEZONE, DEFAULT_USER_TIMEZONE

LEGACY_DB_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"
LEGACY_DB_ISO_NO_COLON_FORMAT = "%Y-%m-%dT%H:%M:%S%z"


def now_local(tz: ZoneInfo = APP_TIMEZONE) -> datetime:
    return datetime.now(tz)


def now_utc() -> datetime:
    return datetime.now(DB_TIMEZONE)


def get_user_timezone(timezone_name: str | None) -> ZoneInfo:
    if not timezone_name:
        return DEFAULT_USER_TIMEZONE
    return ZoneInfo(timezone_name)


def parse_telegram_datetime(value: str, target_tz: ZoneInfo = APP_TIMEZONE) -> datetime:
    telegram_dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if telegram_dt.tzinfo is None:
        telegram_dt = telegram_dt.replace(tzinfo=DB_TIMEZONE)
    return telegram_dt.astimezone(target_tz)


def serialize_db_datetime(value: datetime) -> str:
    """Store all timestamps in DB as timezone-aware UTC ISO-8601 strings."""
    if value.tzinfo is None:
        raise ValueError("Expected timezone-aware datetime for DB serialization")
    return value.astimezone(DB_TIMEZONE).isoformat(timespec='seconds')


def parse_db_datetime(value: str | None, fallback_tz: ZoneInfo = APP_TIMEZONE) -> datetime:
    """Read DB timestamps in UTC, while staying compatible with legacy naive values."""
    if not value:
        return datetime.min.replace(tzinfo=DB_TIMEZONE)

    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        try:
            parsed = datetime.strptime(value, LEGACY_DB_ISO_NO_COLON_FORMAT)
        except ValueError:
            parsed = datetime.strptime(value, LEGACY_DB_TIMESTAMP_FORMAT).replace(tzinfo=fallback_tz)

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=fallback_tz)

    return parsed.astimezone(DB_TIMEZONE)


def format_for_user(value: str | datetime, user_tz: ZoneInfo = DEFAULT_USER_TIMEZONE, fmt: str = "%d.%m.%Y в %H:%M") -> str:
    dt_value = parse_db_datetime(value) if isinstance(value, str) else value.astimezone(DB_TIMEZONE)
    return dt_value.astimezone(user_tz).strftime(fmt)


def parse_user_input_datetime(value: str, user_tz: ZoneInfo = DEFAULT_USER_TIMEZONE) -> datetime:
    return datetime.strptime(value, "%d.%m.%Y %H:%M").replace(tzinfo=user_tz)
