import asyncio
import logging
import os
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

WEB_FETCH_MAX_PAGES = int(os.getenv("WEB_FETCH_MAX_PAGES", "15"))
WEB_FETCH_PAGE_DELAY = float(os.getenv("WEB_FETCH_PAGE_DELAY", "0.5"))


class ScrapedPosts(list):
    def __init__(self, iterable=(), cursor_reached: bool = True):
        super().__init__(iterable)
        self.cursor_reached = cursor_reached
        self.is_partial = not cursor_reached


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


def _parse_channel_html_page(
    html_text: str,
    clean_username: str,
    last_scraped_at: datetime,
) -> tuple[list[dict], int | None, bool]:
    """Parse one HTML page of t.me/s/<channel>.

    Returns (new_posts, min_post_id_on_page, reached_cursor).
    """
    soup = BeautifulSoup(html_text, "html.parser")
    messages = soup.find_all("div", class_="tgme_widget_message")
    logger.info("Channel @%s contains %s message blocks on the page", clean_username, len(messages))

    if not messages:
        page_text = soup.get_text(" ", strip=True).lower()
        if "channel has no messages" in page_text or "канал пока пуст" in page_text:
            return [], None, True
        raise ChannelFetchError(
            f"Telegram Web вернул страницу без сообщений для @{clean_username}",
            permanent=False,
        )

    new_posts = []
    parsed_timestamps = 0
    reached_cursor = False
    all_page_post_ids: list[int] = []

    for msg in messages:
        link = f"https://t.me/{clean_username}"
        link_elem = msg.find("a", class_="tgme_widget_message_date")
        if link_elem and link_elem.has_attr("href"):
            link = link_elem["href"]

        post_id = _extract_post_id(link)
        if post_id is not None:
            all_page_post_ids.append(post_id)

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
            reached_cursor = True
            continue

        text_elem = msg.find("div", class_="tgme_widget_message_text")
        text = text_elem.get_text(separator=" ", strip=True) if text_elem else ""
        if not text:
            continue

        new_posts.append(
            {
                "id": post_id,
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

    min_post_id = min(all_page_post_ids) if all_page_post_ids else None
    return new_posts, min_post_id, reached_cursor


def _parse_channel_html(html_text: str, clean_username: str, last_scraped_at: datetime) -> list[dict]:
    posts, _, _ = _parse_channel_html_page(html_text, clean_username, last_scraped_at)
    return posts


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


async def get_latest_posts(
    channel_username: str,
    last_scraped_at_str: str | None,
    session: aiohttp.ClientSession | None = None,
    max_pages: int = WEB_FETCH_MAX_PAGES,
    page_delay: float = WEB_FETCH_PAGE_DELAY,
) -> ScrapedPosts:
    clean_username = channel_username.replace("@", "")
    last_scraped_at = _parse_last_scraped_at(last_scraped_at_str)

    logger.info("Scanning channel @%s for posts newer than %s", clean_username, last_scraped_at)

    own_session = session is None
    if own_session:
        session = aiohttp.ClientSession(timeout=REQUEST_TIMEOUT)

    all_posts: list[dict] = []
    seen_ids: set[int] = set()
    reached_cursor = False
    pages_fetched = 0
    current_url = f"https://t.me/s/{clean_username}"

    try:
        while pages_fetched < max_pages:
            html = await _load_channel_html(session, current_url, clean_username)
            pages_fetched += 1

            page_posts, min_id, page_reached = _parse_channel_html_page(
                html, clean_username, last_scraped_at
            )

            for post in page_posts:
                pid = post.get("id")
                if pid is not None:
                    if pid not in seen_ids:
                        seen_ids.add(pid)
                        all_posts.append(post)
                else:
                    all_posts.append(post)

            if page_reached:
                reached_cursor = True
                break

            if not min_id or min_id <= 1:
                reached_cursor = True
                break

            if pages_fetched >= max_pages:
                break

            current_url = f"https://t.me/s/{clean_username}?before={min_id}"
            if page_delay > 0:
                await asyncio.sleep(page_delay)

    finally:
        if own_session and session is not None:
            await session.close()

    all_posts.sort(key=lambda p: (p.get("time") or datetime.min.replace(tzinfo=timezone.utc), p.get("id") or 0))
    logger.info(
        "Channel @%s produced %s new posts (pages: %s, cursor_reached: %s)",
        clean_username,
        len(all_posts),
        pages_fetched,
        reached_cursor,
    )
    return ScrapedPosts(all_posts, cursor_reached=reached_cursor)
