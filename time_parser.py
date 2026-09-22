"""Strict parser for short Russian reminder times: «завтра 11», «24/09 в 9», «через 2 дня».

Anything it does not fully understand yields None — a wrong guess is worse than
asking again, so leftover words make the whole phrase invalid.
"""
import calendar
import re
from datetime import datetime, timedelta

DEFAULT_HOUR = 9  # «завтра», «в пятницу», «24.09» without a time — same as the «Утро» button
DAY_PARTS = {"утром": 9, "днем": 14, "вечером": 20}

_FILLERS = {"в", "во", "на", "к", "и", "около", "часов", "часа", "час", "ч", "мин", "минут"}
_WEEKDAYS = [
    ("понедельник", "пн"),
    ("вторник", "вт"),
    ("сред", "ср"),
    ("четверг", "чт"),
    ("пятниц", "пт"),
    ("суббот", "сб"),
    ("воскресен", "вс"),
]
_MONTHS = ["янв", "фев", "мар", "апр", "ма", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]
_DAY_WORDS = {"сегодня": 0, "завтра": 1, "послезавтра": 2}

_RELATIVE_RE = re.compile(
    r"через\s+(?:(\d{1,3}|пару|пол)\s*)?"
    r"(полчаса|минут\w*|мин\b|м\b|час\w*|ч\b|дн(?:я|ей)\b|день|сут\w*|недел\w*|нед\b|месяц\w*|мес\b)"
)
_CLOCK_RE = re.compile(r"(?<![\d.:/])(\d{1,2}):(\d{2})(?![\d:])")
_DOTTED_TIME_RE = re.compile(r"\bв\s+(\d{1,2})\.(\d{2})(?![\d.])")
_HOUR_WITH_PART_RE = re.compile(r"(?<![\d.:/])(\d{1,2})\s*(утра|дня|вечера|ночи)\b")
_AT_HOUR_RE = re.compile(r"\bв\s+(\d{1,2})(?![\d.:/])")
_NUMERIC_DATE_RE = re.compile(r"(?<![\d.:/])(\d{1,2})[./-](\d{1,2})(?:[./-](\d{4}|\d{2}))?(?![\d.:/-])")
_MONTH_NAME_DATE_RE = re.compile(r"(?<!\d)(\d{1,2})\s+(" + "|".join(_MONTHS) + r")[а-я]*\b")
_WEEKDAY_RE = re.compile(
    r"\b(?:" + "|".join(f"{full}[а-я]*|{short}" for full, short in _WEEKDAYS) + r")\b"
)
_BARE_HOUR_RE = re.compile(r"^(\d{1,2})$")


def _normalize(text: str) -> str:
    text = text.lower().replace("ё", "е").replace("днём", "днем")
    return re.sub(r"[,;!?«»\"']", " ", text).strip()


def _add_months(value: datetime, months: int) -> datetime:
    month_index = value.month - 1 + months
    year, month = value.year + month_index // 12, month_index % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def _cut(text: str, match: re.Match) -> str:
    return text[: match.start()] + " " + text[match.end() :]


def parse_reminder_time(text: str, now: datetime) -> datetime | None:
    """Resolve a phrase to an aware datetime in now's timezone, or None if unsure."""
    s = _normalize(text or "")
    if not s or len(s) > 60:
        return None

    relative: timedelta | None = None
    relative_months = 0
    relative_is_clock = False  # minutes/hours: an extra time-of-day makes no sense
    date = None
    clock: tuple[int, int] | None = None

    def set_clock(hour: int, minute: int) -> bool:
        nonlocal clock
        if clock is not None or not (0 <= hour <= 23 and 0 <= minute <= 59):
            return False
        clock = (hour, minute)
        return True

    def set_date(value) -> bool:
        nonlocal date
        if date is not None:
            return False
        date = value
        return True

    if match := _RELATIVE_RE.search(s):
        count_text, unit = match.group(1), match.group(2)
        if unit == "полчаса":
            count, unit = 30, "мин"
        elif count_text == "пол":
            if not unit.startswith("час"):
                return None
            count, unit = 30, "мин"
        else:
            count = 2 if count_text == "пару" else int(count_text or 1)
        if unit.startswith("мес"):
            relative, relative_months = timedelta(0), count
        elif unit.startswith(("мин", "м")):
            relative, relative_is_clock = timedelta(minutes=count), True
        elif unit.startswith(("час", "ч")):
            relative, relative_is_clock = timedelta(hours=count), True
        elif unit.startswith(("дн", "день", "сут")):
            relative = timedelta(days=count)
        else:
            relative = timedelta(weeks=count)
        s = _cut(s, match)

    if match := _CLOCK_RE.search(s):
        if not set_clock(int(match.group(1)), int(match.group(2))):
            return None
        s = _cut(s, match)
    # «в 10.30» is a time, but «в 24.09» is a date — leave impossible times to the date parser.
    if (match := _DOTTED_TIME_RE.search(s)) and int(match.group(1)) <= 23 and int(match.group(2)) <= 59:
        if not set_clock(int(match.group(1)), int(match.group(2))):
            return None
        s = _cut(s, match)

    if match := _HOUR_WITH_PART_RE.search(s):
        hour, part = int(match.group(1)), match.group(2)
        if part in ("дня", "вечера") and hour < 12:
            hour += 12
        elif part == "ночи" and hour == 12:
            hour = 0
        elif part == "утра" and hour == 12:
            hour = 0
        if not set_clock(hour, 0):
            return None
        s = _cut(s, match)

    for word, hour in DAY_PARTS.items():
        if re.search(rf"\b{word}\b", s):
            if not set_clock(hour, 0):
                return None
            s = re.sub(rf"\b{word}\b", " ", s)

    today = now.date()
    for word, offset in _DAY_WORDS.items():
        if re.search(rf"\b{word}\b", s):
            if not set_date(today + timedelta(days=offset)):
                return None
            s = re.sub(rf"\b{word}\b", " ", s)

    if match := _WEEKDAY_RE.search(s):
        token = match.group(0)
        weekday = next(i for i, (full, short) in enumerate(_WEEKDAYS) if token.startswith(full) or token == short)
        days_ahead = (weekday - now.weekday()) % 7 or 7
        if not set_date(today + timedelta(days=days_ahead)):
            return None
        s = _cut(s, match)

    for pattern in (_MONTH_NAME_DATE_RE, _NUMERIC_DATE_RE):
        if match := pattern.search(s):
            day = int(match.group(1))
            if pattern is _MONTH_NAME_DATE_RE:
                month, year_text = _MONTHS.index(match.group(2)) + 1, None
            else:
                month, year_text = int(match.group(2)), match.group(3)
            year = int(year_text) + (2000 if year_text and len(year_text) == 2 else 0) if year_text else today.year
            try:
                value = today.replace(year=year, month=month, day=day)
            except ValueError:
                return None
            if not year_text and value < today:
                try:
                    value = value.replace(year=year + 1)
                except ValueError:  # 29.02
                    return None
            if not set_date(value):
                return None
            s = _cut(s, match)

    if match := _AT_HOUR_RE.search(s):
        if not set_clock(int(match.group(1)), 0):
            return None
        s = _cut(s, match)

    leftover = [token for token in s.split() if token not in _FILLERS]
    if len(leftover) == 1 and (match := _BARE_HOUR_RE.match(leftover[0])) and clock is None:
        if not set_clock(int(match.group(1)), 0):
            return None
        leftover = []
    if leftover:
        return None

    if relative is not None:
        if date is not None or (relative_is_clock and clock is not None):
            return None
        target = _add_months(now + relative, relative_months) if relative_months else now + relative
        if relative_is_clock:
            return target.replace(second=0, microsecond=0)
        hour, minute = clock if clock else (now.hour, now.minute)
        return target.replace(hour=hour, minute=minute, second=0, microsecond=0)

    if date is None and clock is None:
        return None
    if date is None:
        hour, minute = clock
        result = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        return result if result > now else result + timedelta(days=1)

    hour, minute = clock if clock else (DEFAULT_HOUR, 0)
    return now.replace(
        year=date.year, month=date.month, day=date.day, hour=hour, minute=minute, second=0, microsecond=0
    )
