"""ghsub outbox adapter for Unagi (GH2).

Builds and atomically serializes JSON v1 editions for delivery via ghsub.
Contract: docs/sprint/ghsub-contract-json-v1.md
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import html
import json
import logging
import os
import re

from config import settings
from post_text import (
    _extract_bare_url_domain,
    _normalize_caps_title,
    _truncate_by_word,
    clean_post_for_render,
)
from telegraph_publisher import _select_channel_posts

logger = logging.getLogger(__name__)


def strip_html(text: str) -> str:
    """Remove HTML tags and decode HTML entities to plain text."""
    if not text:
        return ""
    clean = re.sub(r"<[^>]+>", "", text)
    clean = html.unescape(clean)
    return clean.strip()


def format_iso8601(val: datetime | str | None) -> str:
    """Format a datetime or DB datetime string into deterministic ISO 8601 UTC string with 'Z'."""
    if val is None:
        from data.database import utc_now

        dt = utc_now()
    elif isinstance(val, datetime):
        dt = val
    elif isinstance(val, str):
        val_clean = val.strip()
        try:
            dt = datetime.fromisoformat(val_clean.replace("Z", "+00:00"))
        except ValueError:
            try:
                from data.database import parse_db_datetime

                dt = parse_db_datetime(val_clean)
            except Exception:
                from data.database import utc_now

                dt = utc_now()
    else:
        from data.database import utc_now

        dt = utc_now()

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)

    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def get_bot_username() -> str:
    """Retrieve the bot username without leading '@'."""
    bot_user = ""
    try:
        import main

        bot_user = getattr(main, "BOT_USERNAME", "")
    except Exception:
        pass
    if not bot_user:
        bot_user = os.getenv("BOT_USERNAME", "")
    return bot_user.lstrip("@").strip()


def resolve_user_id(
    user_id: int | str | None = None,
    sections: list[dict] | None = None,
    address: str | None = None,
) -> int | str:
    """Determine the user ID for recipient addressing."""
    if user_id is not None:
        return user_id
    if sections:
        for sec in sections:
            if sec.get("user_id") is not None:
                return sec["user_id"]
    if address:
        try:
            from data.database import get_connection

            with get_connection() as conn:
                row = conn.execute("SELECT user_id FROM user_emails WHERE email = ?", (address,)).fetchone()
                if row and row["user_id"] is not None:
                    return row["user_id"]
        except Exception:
            pass
        m = re.search(r"unagi-(\d+)", address)
        if m:
            return int(m.group(1))
    return "0"


def resolve_scheduled_at(
    scheduled_at: datetime | str | None = None,
    sections: list[dict] | None = None,
    user_id: int | str | None = None,
) -> str:
    """Determine deterministic ISO 8601 scheduled_at timestamp."""
    if scheduled_at:
        return format_iso8601(scheduled_at)
    if sections:
        for sec in sections:
            if sec.get("scheduled_at"):
                return format_iso8601(sec["scheduled_at"])
    if user_id is not None and str(user_id) != "0":
        try:
            from data.database import get_connection

            with get_connection() as conn:
                row = conn.execute(
                    "SELECT MIN(next_send_at) as sched FROM subscriptions WHERE user_id = ? AND status = 'active'",
                    (user_id,),
                ).fetchone()
                if row and row["sched"]:
                    return format_iso8601(row["sched"])
        except Exception:
            pass
    from time_utils import local_now

    return format_iso8601(local_now())


def resolve_period(sections: list[dict], explicit_period: str | None = None) -> str:
    """Determine edition period ('daily', 'weekly', 'monthly')."""
    if explicit_period:
        return explicit_period
    periods = [s.get("period") for s in sections if s.get("period")]
    if not periods:
        return "daily"
    if "monthly" in periods:
        return "monthly"
    if "weekly" in periods:
        return "weekly"
    return periods[0]


def safe_post_url(post: dict, channel_username: str, post_id: int | str) -> str:
    """Return a validated http(s) URL for a post."""
    raw = (post.get("link") or post.get("url") or "").strip()
    if raw.startswith("http://") or raw.startswith("https://"):
        return raw
    if channel_username and post_id:
        return f"https://t.me/{channel_username}/{post_id}"
    if channel_username:
        return f"https://t.me/{channel_username}"
    return "https://t.me"


def compute_window(to_iso: str, period: str, tz_name: str | None = None) -> dict[str, str]:
    """Compute edition window from, to and timezone."""
    if tz_name is None:
        tz_name = settings().time.app_timezone
    to_dt = datetime.fromisoformat(to_iso.replace("Z", "+00:00"))
    if period == "weekly":
        from_dt = to_dt - timedelta(days=7)
    elif period == "monthly":
        from_dt = to_dt - timedelta(days=30)
    else:
        from_dt = to_dt - timedelta(days=1)
    return {
        "from": format_iso8601(from_dt),
        "to": to_iso,
        "timezone": tz_name,
    }


def build_ghsub_edition(
    sections: list[dict],
    title_plain: str | None = None,
    *,
    user_id: int | str | None = None,
    address: str | None = None,
    scheduled_at: datetime | str | None = None,
    period: str | None = None,
    telegraph_url: str | None = None,
) -> dict:
    """Construct JSON v1 edition dictionary following ghsub contract."""
    resolved_uid = resolve_user_id(user_id=user_id, sections=sections, address=address)
    recipient = f"unagi-{resolved_uid}"
    ed_period = resolve_period(sections, period)
    scheduled_at_iso = resolve_scheduled_at(scheduled_at=scheduled_at, sections=sections, user_id=resolved_uid)

    edition_id = f"unagi:digest:{recipient}:{ed_period}:{scheduled_at_iso}"

    if not title_plain:
        from time_utils import local_now

        now_local = local_now()
        date_str = now_local.strftime("%d.%m.%Y")
        title = f"Дайджест Telegram-каналов · {date_str}"
    else:
        clean_title = strip_html(title_plain)
        from time_utils import local_now

        date_str = local_now().strftime("%d.%m.%Y")
        if "—" in clean_title or "·" in clean_title:
            title = clean_title
        else:
            title = f"{clean_title} — {date_str}"

    window = compute_window(scheduled_at_iso, ed_period)

    clean_telegraph: str | None = None
    if telegraph_url and (telegraph_url.startswith("http://") or telegraph_url.startswith("https://")):
        clean_telegraph = telegraph_url

    items: list[dict] = []
    item_counter = 0

    for section in sections:
        ch_user = (section.get("username") or section.get("channel_username") or "").lstrip("@")
        ch_title = strip_html(section.get("title") or section.get("channel_title") or ch_user or "Канал")
        channel_obj = {"username": ch_user, "title": ch_title}

        raw_posts = section.get("posts") or []
        cleaned_posts = []
        for p in raw_posts:
            cleaned_text = strip_html(p.get("text") or "")
            cleaned_posts.append({**p, "text": cleaned_text})

        cards, shorts = _select_channel_posts(cleaned_posts)

        for card in cards:
            item_counter += 1
            post = card["post"]
            post_id = post.get("id") or post.get("post_id")
            if post_id is None:
                raw_link = str(post.get("link") or post.get("url") or "")
                m = re.search(r"/(\d+)$", raw_link)
                post_id = int(m.group(1)) if m else item_counter

            pub_val = post.get("published_at") or post.get("time") or post.get("post_time")
            pub_iso = format_iso8601(pub_val) if pub_val else scheduled_at_iso

            post_title = strip_html(card.get("title") or "") or "Без названия"
            post_summary = strip_html(card.get("essence") or "")

            item_url = safe_post_url(post, ch_user, post_id)
            metrics = {
                "views": int(post.get("views") or 0),
                "forwards": int(post.get("forwards") or 0),
            }

            items.append({
                "post_id": post_id,
                "published_at": pub_iso,
                "channel": channel_obj,
                "tier": "card",
                "title": post_title,
                "summary": post_summary,
                "url": item_url,
                "metrics": metrics,
            })

        for item in shorts:
            item_counter += 1
            post = item["post"]
            post_id = post.get("id") or post.get("post_id")
            if post_id is None:
                raw_link = str(post.get("link") or post.get("url") or "")
                m = re.search(r"/(\d+)$", raw_link)
                post_id = int(m.group(1)) if m else item_counter

            pub_val = post.get("published_at") or post.get("time") or post.get("post_time")
            pub_iso = format_iso8601(pub_val) if pub_val else scheduled_at_iso

            clean = item.get("clean") or clean_post_for_render(strip_html(post.get("text") or ""))
            domain = _extract_bare_url_domain(clean) or _extract_bare_url_domain(item.get("title") or "")
            if domain:
                short_title = f"🔗 ссылка · {domain}"
            else:
                clean_len = len(" ".join(clean.split()))
                if clean_len < 80:
                    short_title = _normalize_caps_title(" ".join(clean.split()))
                else:
                    short_title = item.get("title") or ""
                if len(short_title) > 140:
                    short_title = _truncate_by_word(short_title or "Без названия", 140)

            short_title = strip_html(short_title) or "Без названия"
            short_summary = strip_html(item.get("essence") or "")

            item_url = safe_post_url(post, ch_user, post_id)
            metrics = {
                "views": int(post.get("views") or 0),
                "forwards": int(post.get("forwards") or 0),
            }

            items.append({
                "post_id": post_id,
                "published_at": pub_iso,
                "channel": channel_obj,
                "tier": "brief",
                "title": short_title,
                "summary": short_summary,
                "url": item_url,
                "metrics": metrics,
            })

    payload: dict = {
        "schema_version": "1.0",
        "edition_id": edition_id,
        "kind": "digest",
        "recipient": recipient,
        "title": title,
        "period": ed_period,
        "window": window,
        "telegraph_url": clean_telegraph,
        "items": items,
    }

    bot_username = get_bot_username()
    if bot_username:
        payload["manage_url"] = f"https://t.me/{bot_username}?start=email"

    return payload


def write_ghsub_outbox(
    sections: list[dict],
    title_plain: str | None = None,
    *,
    user_id: int | str | None = None,
    address: str | None = None,
    scheduled_at: datetime | str | None = None,
    period: str | None = None,
    outbox_dir: str | None = None,
    telegraph_url: str | None = None,
) -> str:
    """Build JSON v1 edition and atomically write it to EMAIL_OUTBOX_DIR."""
    if outbox_dir is None:
        outbox_dir = settings().email.outbox_dir

    payload = build_ghsub_edition(
        sections=sections,
        title_plain=title_plain,
        user_id=user_id,
        address=address,
        scheduled_at=scheduled_at,
        period=period,
        telegraph_url=telegraph_url,
    )

    edition_id = payload["edition_id"]
    os.makedirs(outbox_dir, exist_ok=True)
    target_path = os.path.join(outbox_dir, f"{edition_id}.json")
    tmp_path = os.path.join(outbox_dir, f".tmp_edition_{edition_id}.json")

    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, target_path)
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        raise

    logger.info("Saved ghsub edition %s to %s", edition_id, target_path)
    return target_path
