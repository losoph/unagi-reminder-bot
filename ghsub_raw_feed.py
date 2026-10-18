"""Недельная выгрузка постов Ленты для ghsub (kind: raw_feed, контракт JSON v1).

unagi отдаёт сырые посты каналов Ленты с метриками; отбор, дедупликацию и ранжирование
делает ghsub. Контракт: docs/sprint/ghsub-contract-json-v1.md, раздел «raw_feed».
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime, time as dt_time, timedelta, timezone
import json
import logging
import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from config import settings
from data.database import (
    get_app_meta,
    get_feed_candidate_posts,
    list_feed_email_recipients,
    list_feed_subscriptions,
    set_app_meta,
    utc_now,
)
import feed
from ghsub_outbox import clean_recipient_email, format_iso8601, strip_html, write_edition_atomically
from post_text import _truncate_by_word, clean_post_for_render

logger = logging.getLogger(__name__)

# Расписание ghsub задано в МСК: склейка недельного письма в воскресенье 07:10 (повторы 07:30 и 08:00),
# файлы должны лежать в ящике к 06:15. Выгрузка стартует в 05:30, конец окна — это же время.
GHSUB_TZ = ZoneInfo("Europe/Moscow")
EXPORT_WEEKDAY = 6
EXPORT_AT = dt_time(5, 30)
# После последнего повтора склейки выгрузка бесполезна: ghsub опоздавшую Ленту пропускает.
EXPORT_GIVE_UP_AT = dt_time(8, 0)
CHECK_INTERVAL_SECONDS = 300
WINDOW_DAYS = 7

MAX_ITEMS = 400
MAX_BYTES = 1_000_000
TEXT_LIMIT = 500
MIN_MEDIA_CAPTION = 40

META_LAST_WEEK = "ghsub_raw_feed_last_week"

_TEXT_URL_RE = re.compile(r"https?://[^\s<>\"'«»]+", re.IGNORECASE)
_TRACKING_PARAMS = {"fbclid", "yclid"}
_INTERNAL_HOSTS = {"t.me", "telegram.me", "www.t.me", "www.telegram.me"}


def export_slot(now: datetime) -> datetime:
    """Воскресный слот выгрузки (МСК) той ISO-недели, в которую попадает now."""
    local = now.astimezone(GHSUB_TZ)
    sunday = local.date() + timedelta(days=EXPORT_WEEKDAY - local.weekday())
    return datetime.combine(sunday, EXPORT_AT, tzinfo=GHSUB_TZ)


def iso_week(slot: datetime) -> str:
    year, week, _ = slot.isocalendar()
    return f"{year}-W{week:02d}"


def clean_external_url(url: str) -> str | None:
    """http(s)-ссылка без utm_*/fbclid/yclid; ссылки на сам Telegram не считаются внешними."""
    url = url.strip().rstrip(".,;:!?…»\"'")
    while url.endswith(")") and url.count(")") > url.count("("):
        url = url[:-1]
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return None
    if parts.hostname.lower() in _INTERNAL_HOSTS:
        return None
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in _TRACKING_PARAMS
    ]
    return urlunsplit((parts.scheme.lower(), parts.netloc, parts.path, urlencode(query), parts.fragment))


def extract_external_urls(text: str, link_urls: Any) -> list[str]:
    """Ссылки из текста поста и скрытые под словами (link_urls), очищенные и без повторов."""
    hidden: list[str] = []
    if isinstance(link_urls, str) and link_urls:
        try:
            loaded = json.loads(link_urls)
        except ValueError:
            loaded = []
        hidden = [u for u in loaded if isinstance(u, str)] if isinstance(loaded, list) else []
    elif isinstance(link_urls, list):
        hidden = [u for u in link_urls if isinstance(u, str)]
    cleaned = (clean_external_url(u) for u in [*_TEXT_URL_RE.findall(text or ""), *hidden])
    return list(dict.fromkeys(u for u in cleaned if u))


def post_skip_reason(post: dict, clean_text: str) -> str | None:
    """Почему пост не выгружается: репост, опрос, медиа с короткой подписью или пустой текст."""
    if post.get("is_forward") or post.get("fwd_from_channel_id") is not None or post.get("fwd_from_post_id") is not None:
        # Пересланное сообщение в Telegram не несёт своего текста: подпись к репосту — отдельный пост.
        return "repost"
    if post.get("media_kind") == "poll":
        return "poll"
    if not clean_text:
        return "empty"
    if post.get("media_kind") == "media" and len(clean_text) < MIN_MEDIA_CAPTION:
        return "short_caption"
    return None


def _metric(value: Any) -> int:
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0


def build_raw_feed_item(post: dict, channel_titles: dict[str, str]) -> dict | None:
    username = (post.get("channel_username") or "").lstrip("@")
    post_id = post.get("post_id") if post.get("post_id") is not None else post.get("id")
    if not username or post_id is None:
        return None
    raw_text = post.get("text") or post.get("post_text") or ""
    clean_text = " ".join(clean_post_for_render(strip_html(raw_text)).split())
    if post_skip_reason(post, clean_text):
        return None
    return {
        "post_id": int(post_id),
        "published_at": format_iso8601(post.get("published_at") or post.get("post_time") or post.get("time")),
        "channel": {
            "username": username,
            "title": strip_html(post.get("channel_title") or channel_titles.get(username) or "") or username,
        },
        "url": f"https://t.me/{username}/{int(post_id)}",
        "text": _truncate_by_word(clean_text, TEXT_LIMIT),
        "external_urls": extract_external_urls(raw_text, post.get("link_urls")),
        "metrics": {
            "views": _metric(post.get("views")),
            "forwards": _metric(post.get("forwards")),
            "reactions": _metric(post.get("reactions_total")),
        },
    }


def _round_robin_by_channel(items: list[dict]) -> list[dict]:
    """Порядок урезания: свежие посты каждого канала по очереди, чтобы болтливый канал не вытеснил остальные."""
    by_channel: dict[str, list[dict]] = defaultdict(list)
    for item in sorted(items, key=lambda it: (it["published_at"], it["post_id"]), reverse=True):
        by_channel[item["channel"]["username"]].append(item)
    queues = [by_channel[name] for name in sorted(by_channel)]
    ordered: list[dict] = []
    depth = 0
    while len(ordered) < len(items):
        for queue in queues:
            if depth < len(queue):
                ordered.append(queue[depth])
        depth += 1
    return ordered


def _chronological(items: list[dict]) -> list[dict]:
    return sorted(items, key=lambda it: (it["published_at"], it["channel"]["username"], it["post_id"]))


def _serialized_size(payload: dict) -> int:
    return len(json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))


def build_raw_feed_edition(
    user_id: int,
    address: str,
    posts: list[dict],
    slot: datetime,
    *,
    channel_titles: dict[str, str] | None = None,
    generated_at: datetime | None = None,
) -> dict:
    """Выпуск raw_feed пользователя: шапка JSON v1 и плоский список постов за 7 дней до слота."""
    recipient = f"unagi-{user_id}"
    window_to = slot.astimezone(timezone.utc)
    window_from = window_to - timedelta(days=WINDOW_DAYS)
    payload: dict = {
        "schema_version": "1.0",
        "edition_id": f"unagi:raw_feed:{recipient}:{iso_week(slot)}",
        "kind": "raw_feed",
        "recipient": recipient,
        "period": "weekly",
        "generated_at": format_iso8601(generated_at or utc_now()),
        "window": {
            "from": format_iso8601(window_from),
            "to": format_iso8601(window_to),
            "timezone": str(GHSUB_TZ),
        },
        "items": [],
    }
    email = clean_recipient_email(address)
    if email:
        payload["recipient_email"] = email

    titles = channel_titles or {}
    items: list[dict] = []
    seen: set[tuple[str, int]] = set()
    for post in posts:
        item = build_raw_feed_item(post, titles)
        if item is None:
            continue
        key = (item["channel"]["username"], item["post_id"])
        if key not in seen:
            seen.add(key)
            items.append(item)

    kept = _round_robin_by_channel(items)[:MAX_ITEMS]
    payload["items"] = _chronological(kept)
    while kept and _serialized_size(payload) > MAX_BYTES:
        overshoot = _serialized_size(payload) - MAX_BYTES
        average = max(_serialized_size(payload) // len(kept), 1)
        kept = kept[: max(len(kept) - overshoot // average - 1, 0)]
        payload["items"] = _chronological(kept)
    if len(kept) < len(items):
        logger.info("raw_feed user=%s trimmed items %d -> %d", user_id, len(items), len(kept))
    return payload


def write_raw_feed_edition(payload: dict, outbox_dir: str | None = None) -> str:
    return write_edition_atomically(payload, outbox_dir or settings().email.outbox_dir)


async def export_raw_feed(slot: datetime, channel_source: Any = None, *, now: datetime | None = None) -> int:
    """Выгрузить raw_feed всем пользователям с почтовой Лентой. Возвращает число записанных выпусков.

    Каналы дочитываются и метрики освежаются один раз на канал для всех пользователей; сбой чтения
    канала не мешает выгрузке — берутся уже сохранённые посты. Ошибка записи пробрасывается.
    """
    window_to = slot.astimezone(timezone.utc)
    window_from = window_to - timedelta(days=WINDOW_DAYS)
    recipients = await asyncio.to_thread(list_feed_email_recipients)
    subs_by_user: dict[int, list] = {}
    for user_id, _ in recipients:
        subs_by_user[user_id] = await asyncio.to_thread(list_feed_subscriptions, user_id, False)
    channels = sorted({sub["channel_username"] for subs in subs_by_user.values() for sub in subs})

    if channels:
        try:
            await feed.ingest_window(channels, window_from, now=now or utc_now(), channel_source=channel_source)
        except Exception:
            logger.exception("raw_feed: не удалось дочитать каналы Ленты, выгружаю сохранённые посты")

    posts_by_user: dict[int, list[dict]] = {}
    post_ids_by_channel: dict[str, set[int]] = defaultdict(set)
    for user_id, _ in recipients:
        posts = await asyncio.to_thread(get_feed_candidate_posts, user_id, window_from, window_to)
        posts_by_user[user_id] = posts
        for post in posts:
            post_ids_by_channel[post["channel_username"]].add(post["post_id"])

    if channel_source is not None and hasattr(channel_source, "refresh_metrics"):
        refreshed = 0
        for channel, post_ids in post_ids_by_channel.items():
            try:
                refreshed += await channel_source.refresh_metrics(channel, sorted(post_ids), period="week")
            except Exception as exc:
                logger.warning("raw_feed: не удалось обновить метрики @%s: %s", channel, exc)
        if refreshed:
            for user_id, _ in recipients:
                posts_by_user[user_id] = await asyncio.to_thread(
                    get_feed_candidate_posts, user_id, window_from, window_to
                )

    written = 0
    for user_id, address in recipients:
        titles = {
            sub["channel_username"]: sub["channel_title"] or ""
            for sub in subs_by_user.get(user_id, [])
        }
        payload = build_raw_feed_edition(user_id, address, posts_by_user.get(user_id, []), slot, channel_titles=titles)
        if "recipient_email" not in payload:
            logger.warning("raw_feed user=%s: адрес не прошёл проверку, выпуск не пишется", user_id)
            continue
        await asyncio.to_thread(write_raw_feed_edition, payload)
        written += 1
        logger.info("raw_feed user=%s edition=%s items=%d", user_id, payload["edition_id"], len(payload["items"]))
    return written


async def run_raw_feed_export_once(channel_source: Any = None, now: datetime | None = None) -> str:
    """Один шаг расписания. Возвращает 'not_due' | 'done' | 'exported' | 'missed'."""
    now_dt = now or utc_now()
    slot = export_slot(now_dt)
    week = iso_week(slot)
    if now_dt < slot:
        return "not_due"
    if await asyncio.to_thread(get_app_meta, META_LAST_WEEK) == week:
        return "done"
    if now_dt.astimezone(GHSUB_TZ).time() >= EXPORT_GIVE_UP_AT:
        logger.warning("raw_feed %s: выгрузка не успела до %s МСК — ghsub её уже не возьмёт, пропускаю неделю", week, EXPORT_GIVE_UP_AT)
        await asyncio.to_thread(set_app_meta, META_LAST_WEEK, week)
        return "missed"
    written = await export_raw_feed(slot, channel_source, now=now_dt)
    await asyncio.to_thread(set_app_meta, META_LAST_WEEK, week)
    logger.info("raw_feed %s: записано выпусков %d", week, written)
    return "exported"


async def run_raw_feed_export_loop(channel_source: Any = None) -> None:
    logger.info("📤 Выгрузка Ленты для ghsub запущена (воскресенье %s МСК)", EXPORT_AT.strftime("%H:%M"))
    while True:
        try:
            await run_raw_feed_export_once(channel_source)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Неделя не отмечена — следующая проверка повторит выгрузку целиком (edition_id тот же).
            logger.exception("Выгрузка Ленты для ghsub упала, повторю через %d сек", CHECK_INTERVAL_SECONDS)
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)
