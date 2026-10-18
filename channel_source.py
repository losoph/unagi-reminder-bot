import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

import aiohttp

from config import settings
from scraper import ChannelFetchError, get_latest_posts

logger = logging.getLogger(__name__)

FEED_METRICS_STALE_THRESHOLDS: dict[str, float] = {
    "day": 1800.0,       # 30 min
    "daily": 1800.0,
    "день": 1800.0,
    "week": 10800.0,     # 3 h
    "weekly": 10800.0,
    "неделя": 10800.0,
    "month": 43200.0,    # 12 h
    "monthly": 43200.0,
    "месяц": 43200.0,
}


def _describe_media_and_links(message) -> tuple[str, list[str]]:
    """Kind of attached media ('' — none, 'poll', 'media') and URLs hidden from the plain text.

    A link preview (MessageMediaWebPage) is not media: the post is still text. URLs visible
    in the text are taken from the text itself by the exporters.
    """
    media = getattr(message, "media", None)
    media_name = type(media).__name__ if media is not None else ""
    if not media_name or media_name == "MessageMediaWebPage":
        media_kind = ""
    elif media_name == "MessageMediaPoll":
        media_kind = "poll"
    else:
        media_kind = "media"

    urls: list[str] = []
    for entity in getattr(message, "entities", None) or []:
        url = getattr(entity, "url", None)
        if isinstance(url, str) and url:
            urls.append(url)
    if media_name == "MessageMediaWebPage":
        url = getattr(getattr(media, "webpage", None), "url", None)
        if isinstance(url, str) and url:
            urls.append(url)
    return media_kind, list(dict.fromkeys(urls))


class HybridChannelSource:
    """Read channel history through MTProto, with an optional Telegram Web fallback."""

    def __init__(self):
        cfg = settings().channel_source
        self.mode = cfg.mode
        self.api_id = cfg.api_id
        self.api_hash = cfg.api_hash
        self.session_string = cfg.session_string
        self.web_fallback = cfg.web_fallback
        self.fetch_limit = cfg.fetch_limit
        self._client = None
        self.account_id: int | None = None
        self._fetch_semaphore = None

        if self.mode not in {"auto", "mtproto", "web"}:
            raise ValueError("CHANNEL_SOURCE must be one of: auto, mtproto, web")

    @property
    def fetch_semaphore(self) -> asyncio.Semaphore:
        if self._fetch_semaphore is None:
            concurrency = settings().channel_source.fetch_concurrency
            self._fetch_semaphore = asyncio.Semaphore(concurrency)
        return self._fetch_semaphore

    @fetch_semaphore.setter
    def fetch_semaphore(self, sem: asyncio.Semaphore) -> None:
        self._fetch_semaphore = sem

    @property
    def mtproto_configured(self) -> bool:
        return bool(self.api_id and self.api_hash and self.session_string)

    async def start(self) -> None:
        if self.mode == "web":
            logger.info("Channel source: Telegram Web")
            return
        if not self.mtproto_configured:
            if self.mode == "mtproto":
                raise RuntimeError(
                    "CHANNEL_SOURCE=mtproto requires TELEGRAM_API_ID, TELEGRAM_API_HASH and TELEGRAM_USER_SESSION"
                )
            logger.warning("MTProto credentials are not configured; channel source: Telegram Web fallback")
            return

        try:
            from telethon import TelegramClient
            from telethon.sessions import StringSession
        except ImportError as exc:
            raise RuntimeError("Telethon is required for MTProto channel ingestion") from exc

        try:
            self._client = TelegramClient(
                StringSession(self.session_string),
                int(self.api_id),
                self.api_hash,
            )
            await self._client.connect()
            if not await self._client.is_user_authorized():
                raise RuntimeError("TELEGRAM_USER_SESSION is not authorized")
            me = await self._client.get_me()
            self.account_id = getattr(me, "id", None)
            logger.info("Channel source: MTProto user %s", getattr(me, "id", "unknown"))
        except Exception:
            if self._client is not None:
                await self._client.disconnect()
                self._client = None
            if self.mode == "mtproto" and not self.web_fallback:
                raise
            logger.exception("MTProto startup failed; channel source: Telegram Web fallback")

    async def close(self) -> None:
        if self._client is not None:
            await self._client.disconnect()
            self._client = None

    @staticmethod
    def _extract_reactions(message) -> tuple[int | None, str | None]:
        reactions = getattr(message, "reactions", None)
        if not reactions:
            return None, None
        results = getattr(reactions, "results", None)
        if not results:
            return None, None
        total = 0
        items = []
        for r in results:
            count = getattr(r, "count", 0)
            total += count
            reaction_obj = getattr(r, "reaction", None)
            emoji = getattr(reaction_obj, "emoticon", str(reaction_obj)) if reaction_obj else "?"
            items.append({"emoji": emoji, "count": count})
        return total, json.dumps(items, ensure_ascii=False)

    async def _fetch_subscribers_count(self, clean_username: str) -> int | None:
        if not callable(self._client):
            return None
        try:
            from telethon.tl.functions.channels import GetFullChannelRequest

            full = await self._client(GetFullChannelRequest(clean_username))
            full_chat = getattr(full, "full_chat", None)
            return getattr(full_chat, "participants_count", None)
        except Exception:
            return None

    async def _fetch_mtproto(self, channel_username: str, last_scraped_at_str: str | None) -> list[dict]:
        if self._client is None:
            raise ChannelFetchError("MTProto client is not connected", permanent=False)

        from data.database import parse_db_datetime

        marker = parse_db_datetime(last_scraped_at_str)
        posts: list[dict] = []
        inspected = 0
        reached_marker = False
        clean_username = channel_username.lstrip("@")

        # Update subscribers count if needed (not more often than once every 24h)
        try:
            from data.database import needs_channel_subscribers_update, update_channel_subscribers

            if needs_channel_subscribers_update(clean_username):
                count = await self._fetch_subscribers_count(clean_username)
                if count is not None:
                    update_channel_subscribers(clean_username, count)
        except Exception:
            logger.debug("Failed to update subscriber count for @%s", clean_username, exc_info=True)

        found_channel_id = None
        try:
            async for message in self._client.iter_messages(clean_username, limit=self.fetch_limit):
                inspected += 1
                if found_channel_id is None:
                    peer_id = getattr(message, "peer_id", None)
                    cid = getattr(peer_id, "channel_id", None)
                    if cid is not None:
                        found_channel_id = cid
                post_time = message.date
                if post_time.tzinfo is None:
                    post_time = post_time.replace(tzinfo=timezone.utc)
                else:
                    post_time = post_time.astimezone(timezone.utc)
                if post_time <= marker:
                    reached_marker = True
                    break
                text = (message.message or "").strip()
                if not text:
                    continue

                views = getattr(message, "views", None)
                forwards = getattr(message, "forwards", None)
                replies_obj = getattr(message, "replies", None)
                replies = getattr(replies_obj, "replies", None) if replies_obj else None
                reactions_total, reactions_json = self._extract_reactions(message)
                metrics_at = datetime.now(timezone.utc)

                fwd_from = getattr(message, "fwd_from", None)
                fwd_from_channel_id = None
                fwd_from_post_id = None
                if fwd_from is not None:
                    from_id = getattr(fwd_from, "from_id", None)
                    fwd_from_channel_id = getattr(from_id, "channel_id", None)
                    fwd_from_post_id = getattr(fwd_from, "channel_post", None)
                media_kind, link_urls = _describe_media_and_links(message)

                posts.append(
                    {
                        "id": message.id,
                        "time": post_time,
                        "text": text,
                        "link": f"https://t.me/{clean_username}/{message.id}",
                        "views": views,
                        "reactions_total": reactions_total,
                        "reactions_json": reactions_json,
                        "forwards": forwards,
                        "replies": replies,
                        "metrics_at": metrics_at,
                        "fwd_from_channel_id": fwd_from_channel_id,
                        "fwd_from_post_id": fwd_from_post_id,
                        "media_kind": media_kind,
                        "is_forward": fwd_from is not None,
                        "link_urls": link_urls,
                    }
                )

            if found_channel_id is not None:
                try:
                    from data.database import update_channel_id

                    update_channel_id(clean_username, found_channel_id)
                except Exception:
                    logger.debug("Failed to update channel_id for @%s", clean_username, exc_info=True)
        except Exception as exc:
            try:
                from telethon.errors import (
                    ChannelPrivateError,
                    UsernameInvalidError,
                    UsernameNotOccupiedError,
                )

                permanent = isinstance(
                    exc,
                    (ChannelPrivateError, UsernameInvalidError, UsernameNotOccupiedError),
                )
            except ImportError:
                permanent = False
            raise ChannelFetchError(
                f"MTProto не смог прочитать @{clean_username}: {exc}",
                permanent=permanent,
            ) from exc

        if inspected >= self.fetch_limit and posts and not reached_marker:
            raise ChannelFetchError(
                f"Для @{clean_username} найдено больше {self.fetch_limit} новых сообщений; курсор не продвинут",
                permanent=False,
            )

        posts.reverse()
        logger.info("MTProto channel @%s produced %s new posts", clean_username, len(posts))
        return posts

    async def refresh_metrics(
        self,
        channel_username: str,
        post_ids: list[int],
        *,
        period: str | None = None,
        stale_threshold_seconds: float | None = None,
        now: datetime | None = None,
    ) -> int:
        """Обновить views, forwards, replies, metrics_at для постов канала через GetMessagesViewsRequest.

        Батчами по <=100 сообщений.
        Обновляет только посты, metrics_at которых старше порога.
        При ошибках (в т.ч. FloodWait) логирует WARNING и прекращает обновление этого канала.
        Без MTProto (web) — обновление пропускается (возвращает 0).
        """
        if self._client is None or not callable(self._client):
            return 0
        if not post_ids:
            return 0

        clean_username = channel_username.lstrip("@")
        from data.database import get_stale_metric_post_ids, update_post_metrics, utc_now

        now_dt = now or utc_now()
        threshold: float | None = None
        if stale_threshold_seconds is not None:
            threshold = stale_threshold_seconds
        elif period:
            threshold = FEED_METRICS_STALE_THRESHOLDS.get(period.lower(), 1800.0)

        if threshold is not None:
            cutoff = now_dt - timedelta(seconds=threshold)
            stale_ids = get_stale_metric_post_ids(clean_username, post_ids, cutoff)
        else:
            stale_ids = list(post_ids)

        if not stale_ids:
            return 0

        batch_size = 100
        total_updated = 0

        for i in range(0, len(stale_ids), batch_size):
            batch = stale_ids[i : i + batch_size]
            try:
                from telethon.tl.functions.messages import GetMessagesViewsRequest

                req = GetMessagesViewsRequest(peer=clean_username, id=batch, increment=False)
                result = await self._client(req)
            except Exception as exc:
                logger.warning(
                    "Failed to refresh metrics for @%s: %s; aborting refresh for this channel",
                    clean_username,
                    exc,
                )
                break

            views_list = getattr(result, "views", None)
            if views_list is None and isinstance(result, list):
                views_list = result
            elif views_list is None:
                views_list = []

            if len(views_list) != len(batch):
                logger.warning(
                    "Length mismatch in GetMessagesViewsRequest for @%s: expected %d, got %d; aborting refresh for this channel",
                    clean_username,
                    len(batch),
                    len(views_list),
                )
                break

            metrics_to_update = []
            for post_id, v in zip(batch, views_list):
                if isinstance(v, dict):
                    v_views = v.get("views")
                    v_forwards = v.get("forwards")
                    v_replies = v.get("replies")
                else:
                    v_views = getattr(v, "views", None)
                    v_forwards = getattr(v, "forwards", None)
                    replies_obj = getattr(v, "replies", None)
                    if replies_obj is not None:
                        if isinstance(replies_obj, int):
                            v_replies = replies_obj
                        else:
                            v_replies = getattr(replies_obj, "replies", None)
                    else:
                        v_replies = None
                metrics_to_update.append(
                    {
                        "post_id": post_id,
                        "views": v_views,
                        "forwards": v_forwards,
                        "replies": v_replies,
                        "metrics_at": now_dt,
                    }
                )

            if metrics_to_update:
                update_post_metrics(clean_username, metrics_to_update)
                total_updated += len(metrics_to_update)

        return total_updated

    async def fetch(
        self,
        channel_username: str,
        last_scraped_at_str: str | None,
        *,
        web_session: aiohttp.ClientSession,
    ) -> tuple[list[dict], str]:
        if self._client is not None:
            try:
                return await self._fetch_mtproto(channel_username, last_scraped_at_str), "mtproto"
            except ChannelFetchError:
                if not self.web_fallback:
                    raise
                logger.warning(
                    "MTProto fetch failed for @%s; trying Telegram Web fallback",
                    channel_username,
                    exc_info=True,
                )

        if self.mode == "mtproto" and not self.web_fallback:
            raise ChannelFetchError("MTProto client is unavailable", permanent=False)
        return (
            await get_latest_posts(channel_username, last_scraped_at_str, session=web_session),
            "web",
        )

    async def list_personal_channels(
        self,
        limit: int = 20,
        *,
        now: datetime | None = None,
        exclude_usernames: set[str] | list[str] | None = None,
    ) -> list[dict]:
        """List owner's personal broadcast channels with public username.

        Filters:
        - Broadcast channels only (broadcast, not groups/megagroups)
        - Public username
        - Last message not older than 30 days
        - Not in exclude_usernames
        Sorted by unread_count DESC, then recent activity DESC.
        Max `limit` channels (default 20).
        On FloodWait or error: returns [] and logs WARNING.
        DO NOT log dialog list or titles for privacy.
        """
        if self._client is None or not hasattr(self._client, "iter_dialogs"):
            return []

        from data.database import normalize_channel_username

        now_dt = now or datetime.now(timezone.utc)
        if now_dt.tzinfo is None:
            now_dt = now_dt.replace(tzinfo=timezone.utc)
        cutoff = now_dt - timedelta(days=30)

        excluded = {normalize_channel_username(u) for u in (exclude_usernames or []) if u}

        candidates = []
        try:
            async for dialog in self._client.iter_dialogs(limit=300):
                # Check broadcast channel (not group)
                is_channel = getattr(dialog, "is_channel", False)
                is_group = getattr(dialog, "is_group", False)
                entity = getattr(dialog, "entity", None)
                is_megagroup = getattr(entity, "megagroup", False) if entity else False
                is_broadcast = getattr(entity, "broadcast", False) if entity else False

                if not is_broadcast and (not is_channel or is_group or is_megagroup):
                    continue

                raw_username = getattr(entity, "username", None) or getattr(dialog, "username", None)
                if not raw_username:
                    continue

                clean = normalize_channel_username(raw_username)
                if not clean or clean in excluded:
                    continue

                dialog_date = getattr(dialog, "date", None)
                if dialog_date is None:
                    continue
                if dialog_date.tzinfo is None:
                    dialog_date = dialog_date.replace(tzinfo=timezone.utc)
                else:
                    dialog_date = dialog_date.astimezone(timezone.utc)

                if dialog_date < cutoff:
                    continue

                unread_count = getattr(dialog, "unread_count", 0) or 0
                title = getattr(dialog, "title", None) or getattr(dialog, "name", None) or clean

                candidates.append(
                    {
                        "username": clean,
                        "title": title,
                        "unread_count": int(unread_count),
                        "date": dialog_date,
                    }
                )
        except Exception as exc:
            wait_s = getattr(exc, "seconds", None)
            if wait_s is not None:
                logger.warning(
                    "Failed to list personal channels: %s (wait %ss)",
                    type(exc).__name__,
                    wait_s,
                )
            else:
                logger.warning(
                    "Failed to list personal channels: %s",
                    type(exc).__name__,
                )
            return []

        # Sort: unread_count DESC, then date DESC
        candidates.sort(key=lambda c: (-c["unread_count"], -c["date"].timestamp()))

        result = [
            {
                "username": c["username"],
                "title": c["title"],
                "unread_count": c["unread_count"],
            }
            for c in candidates[:limit]
        ]
        # Privacy invariant: do NOT log channel names or titles
        logger.debug("Discovered %d personal broadcast channels", len(result))
        return result
