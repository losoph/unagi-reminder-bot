"""One digest edition per user per day.

When the daily, weekly and monthly digests fall on the same day, the user gets a single
edition at the time of the senior one (monthly > weekly > daily). Junior subscriptions
that become due earlier are deferred to that moment; those scheduled later the same day
are pulled into it. Inside the edition every period is its own group, juniors first:
«За сутки», then «За неделю», then «За месяц».

Days are counted in APP_TIMEZONE: digest schedules do not follow the user's own zone yet.
"""
from datetime import datetime, timedelta

from data.database import parse_db_datetime, serialize_datetime
from time_utils import TZ

PERIOD_ORDER = ("daily", "weekly", "monthly")
PERIOD_RANK = {period: rank for rank, period in enumerate(PERIOD_ORDER)}
PERIOD_GROUP_LABELS = {"daily": "☀️ За сутки", "weekly": "🗓 За неделю", "monthly": "📆 За месяц"}

# Row shape of get_due_subscriptions / get_user_subscriptions_due_until.
_PERIOD = 4
_NEXT_SEND_AT = 8


def local_day_end(now: datetime) -> str:
    """Last second of the local (APP_TIMEZONE) day of ``now``, serialized for the DB."""
    local = now.astimezone(TZ)
    next_midnight = (local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return serialize_datetime(next_midnight - timedelta(seconds=1))


def _rank(period: str | None) -> int:
    return PERIOD_RANK.get(period or "", 0)


def plan_daily_edition(subs: list, now: datetime) -> tuple[datetime | None, list]:
    """Decide what to do with a user's subscriptions scheduled up to the end of today.

    Returns ``(defer_until, subs)``:
    - ``defer_until`` is set when the senior period's time is still ahead: ``subs`` are the
      due juniors that must wait for it;
    - otherwise ``defer_until`` is None and ``subs`` is the whole edition (due ones plus
      the rest of today's subscriptions, pulled forward).
    """
    if not subs:
        return None, []
    senior_rank = max(_rank(sub[_PERIOD]) for sub in subs)
    senior_at = min(parse_db_datetime(sub[_NEXT_SEND_AT]) for sub in subs if _rank(sub[_PERIOD]) == senior_rank)
    if senior_at > now:
        return senior_at, [sub for sub in subs if parse_db_datetime(sub[_NEXT_SEND_AT]) <= now]
    return None, list(subs)


def next_send_base(now_local: datetime, scheduled_at: str | None) -> datetime:
    """Moment after which the next run of a subscription is computed.

    A subscription pulled forward into an earlier edition must not fire again at its own
    time later the same day, so the base is the later of «now» and its scheduled time.
    """
    if not scheduled_at:
        return now_local
    scheduled_local = parse_db_datetime(scheduled_at).astimezone(now_local.tzinfo or TZ)
    return max(now_local, scheduled_local)


def dedupe_edition_sections(sections: list[dict]) -> list[dict]:
    """A channel subscribed in two periods would repeat its posts in both groups.

    Keep each post (by link) in the first group it appears in — juniors come first — and
    drop channels left without posts.
    """
    seen: set[str] = set()
    result: list[dict] = []
    for section in sections:
        posts = []
        for post in section.get("posts") or []:
            key = post.get("link") or ""
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            posts.append(post)
        if posts:
            result.append({**section, "posts": posts})
    return result


def arrange_edition_sections(sections: list[dict]) -> list[dict]:
    """Order sections juniors-first and label groups when the edition mixes periods."""
    ordered = sorted(sections, key=lambda section: _rank(section.get("period")))
    periods = {section.get("period") for section in ordered}
    if len(periods) <= 1:
        return [{key: value for key, value in section.items() if key != "group"} for section in ordered]
    return [{**section, "group": PERIOD_GROUP_LABELS.get(section.get("period"), "")} for section in ordered]


def edition_title(sections: list[dict], now_local: datetime) -> str:
    """Title by the senior period inside; a daily edition is named by its time of day."""
    periods = {section.get("period") for section in sections}
    if "monthly" in periods:
        return "Твоя газета за месяц"
    if "weekly" in periods:
        return "Твоя газета за неделю"
    if now_local.hour < 12:
        return "Твоя утренняя газета"
    if now_local.hour < 18:
        return "Твоя дневная газета"
    return "Твоя вечерняя газета"
