import logging
import os
from datetime import timezone

import aiohttp

from scraper import ChannelFetchError, get_latest_posts

logger = logging.getLogger(__name__)


class HybridChannelSource:
    """Read channel history through MTProto, with an optional Telegram Web fallback."""

    def __init__(self):
        self.mode = os.getenv("CHANNEL_SOURCE", "auto").strip().lower()
        self.api_id = os.getenv("TELEGRAM_API_ID")
        self.api_hash = os.getenv("TELEGRAM_API_HASH")
        self.session_string = os.getenv("TELEGRAM_USER_SESSION")
        self.web_fallback = os.getenv("MTPROTO_WEB_FALLBACK", "true").lower() in {"1", "true", "yes"}
        self.fetch_limit = max(20, int(os.getenv("MTPROTO_FETCH_LIMIT", "1000")))
        self._client = None

        if self.mode not in {"auto", "mtproto", "web"}:
            raise ValueError("CHANNEL_SOURCE must be one of: auto, mtproto, web")

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

    async def _fetch_mtproto(self, channel_username: str, last_scraped_at_str: str | None) -> list[dict]:
        if self._client is None:
            raise ChannelFetchError("MTProto client is not connected", permanent=False)

        from data.database import parse_db_datetime

        marker = parse_db_datetime(last_scraped_at_str)
        posts: list[dict] = []
        inspected = 0
        reached_marker = False
        clean_username = channel_username.lstrip("@")

        try:
            async for message in self._client.iter_messages(clean_username, limit=self.fetch_limit):
                inspected += 1
                post_time = message.date
                if post_time.tzinfo is None:
                    post_time = post_time.replace(tzinfo=timezone.utc)
                else:
                    post_time = post_time.astimezone(timezone.utc)
                if post_time <= marker:
                    reached_marker = True
                    break
                text = (message.message or "Медиа-файл или сообщение без текста").strip()
                if len(text) > 300:
                    text = text[:297] + "..."
                posts.append(
                    {
                        "id": message.id,
                        "time": post_time,
                        "text": text,
                        "link": f"https://t.me/{clean_username}/{message.id}",
                    }
                )
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
