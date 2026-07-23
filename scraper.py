import asyncio
import logging
import re
from datetime import datetime, timezone
from urllib.parse import urlparse

import aiohttp
from bs4 import BeautifulSoup

from data.database import parse_db_datetime

logger = logging.getLogger(__name__)
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=10)
REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/91.0.4472.124 Safari/537.36"
    )
}


class ChannelFetchError(Exception):
    def __init__(self, message, *, permanent=False):
        super().__init__(message)
        self.permanent = permanent


def _parse_last_scraped_at(last_scraped_at_str: str | None) -> datetime:
    return parse_db_datetime(last_scraped_at_str)


def _extract_post_id(link: str | None) -> int | None:
    if not link:
        return None
    match = re.search(r"/(\d+)(?:\?.*)?$", urlparse(link).path)
    return int(match.group(1)) if match else None


def _parse_channel_html(html_text: str, clean_username: str, last_scraped_at: datetime) -> list[dict]:
    soup = BeautifulSoup(html_text, "html.parser")
    messages = soup.find_all("div", class_="tgme_widget_message")
    logger.info("Channel @%s contains %s message blocks on the page", clean_username, len(messages))

    # t.me may return a login/interstitial/limited page with HTTP 200. Treating it
    # as an empty channel would advance the delivery cursor and silently lose posts.
    if not messages:
        page_text = soup.get_text(" ", strip=True).lower()
        if "channel has no messages" in page_text or "канал пока пуст" in page_text:
            return []
        raise ChannelFetchError(
            f"Telegram Web вернул страницу без сообщений для @{clean_username}",
            permanent=False,
        )

    new_posts = []
    parsed_timestamps = 0
    for msg in messages:
        date_elem = msg.find("time", class_="time")
        if not date_elem or not date_elem.has_attr("datetime"):
            continue

        try:
            datetime_str = date_elem["datetime"]
            post_time = datetime.fromisoformat(datetime_str.replace("Z", "+00:00"))
            if post_time.tzinfo is None:
                post_time = post_time.replace(tzinfo=timezone.utc)
            else:
                post_time = post_time.astimezone(timezone.utc)
            parsed_timestamps += 1
        except (ValueError, KeyError):
            continue

        if post_time <= last_scraped_at:
            continue

        text_elem = msg.find("div", class_="tgme_widget_message_text")
        text = text_elem.get_text(separator=" ", strip=True) if text_elem else "Медиа-файл или сообщение без текста"
        if len(text) > 300:
            text = text[:297] + "..."

        link = f"https://t.me/{clean_username}"
        link_elem = msg.find("a", class_="tgme_widget_message_date")
        if link_elem and link_elem.has_attr("href"):
            link = link_elem["href"]

        new_posts.append(
            {
                "id": _extract_post_id(link),
                "time": post_time,
                "text": text,
                "link": link,
            }
        )

    if parsed_timestamps == 0:
        raise ChannelFetchError(
            f"Не удалось распознать даты сообщений @{clean_username}",
            permanent=False,
        )

    logger.info("Channel @%s produced %s new posts for the digest", clean_username, len(new_posts))
    return new_posts


async def _load_channel_html(session: aiohttp.ClientSession, url: str, clean_username: str) -> str:
    try:
        async with session.get(url, headers=REQUEST_HEADERS) as response:
            logger.info("Fetching Telegram channel %s returned HTTP %s", clean_username, response.status)
            if response.status != 200:
                raise ChannelFetchError(
                    f"Не удалось получить канал @{clean_username}: HTTP {response.status}",
                    # Web preview restrictions are not authoritative evidence that
                    # a Telegram channel is permanently unavailable.
                    permanent=False,
                )
            html_text = await response.text()
            if not html_text.strip():
                raise ChannelFetchError(
                    f"Telegram Web вернул пустой ответ для @{clean_username}",
                    permanent=False,
                )
            return html_text
    except aiohttp.ClientError as exc:
        raise ChannelFetchError(
            f"Сетевая ошибка при чтении канала @{clean_username}: {exc}",
            permanent=False,
        ) from exc
    except asyncio.TimeoutError as exc:
        raise ChannelFetchError(
            f"Таймаут при чтении канала @{clean_username}",
            permanent=False,
        ) from exc


async def get_latest_posts(channel_username, last_scraped_at_str, session: aiohttp.ClientSession | None = None):
    clean_username = channel_username.replace("@", "")
    url = f"https://t.me/s/{clean_username}"
    last_scraped_at = _parse_last_scraped_at(last_scraped_at_str)

    logger.info("Scanning channel @%s for posts newer than %s", clean_username, last_scraped_at)

    if session is None:
        async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as owned_session:
            html = await _load_channel_html(owned_session, url, clean_username)
    else:
        html = await _load_channel_html(session, url, clean_username)

    return _parse_channel_html(html, clean_username, last_scraped_at)
