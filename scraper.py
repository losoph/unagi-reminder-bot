import asyncio
import logging
from datetime import datetime, timedelta

import aiohttp
from bs4 import BeautifulSoup

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
    return datetime.strptime(last_scraped_at_str, '%Y-%m-%d %H:%M:%S') if last_scraped_at_str else datetime.min


async def _load_channel_html(session: aiohttp.ClientSession, url: str, clean_username: str) -> str:
    try:
        async with session.get(url, headers=REQUEST_HEADERS) as response:
            logger.info("Fetching Telegram channel %s returned HTTP %s", clean_username, response.status)
            if response.status != 200:
                raise ChannelFetchError(
                    f"Не удалось получить канал @{clean_username}: HTTP {response.status}",
                    permanent=response.status in {403, 404},
                )
            return await response.text()
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

    soup = BeautifulSoup(html, 'html.parser')
    messages = soup.find_all('div', class_='tgme_widget_message')
    logger.info("Channel @%s contains %s message blocks on the page", clean_username, len(messages))

    new_posts = []
    for msg in messages:
        date_elem = msg.find('time', class_='time')
        if not date_elem or not date_elem.has_attr('datetime'):
            continue

        try:
            post_time_str = date_elem['datetime'][:19]
            post_time = datetime.strptime(post_time_str, '%Y-%m-%dT%H:%M:%S') + timedelta(hours=3)
        except ValueError:
            continue

        if post_time <= last_scraped_at:
            continue

        text_elem = msg.find('div', class_='tgme_widget_message_text')
        text = text_elem.get_text(separator=' ', strip=True) if text_elem else "Медиа-файл или сообщение без текста"

        link = f"https://t.me/{clean_username}"
        link_elem = msg.find('a', class_='tgme_widget_message_date')
        if link_elem and link_elem.has_attr('href'):
            link = link_elem['href']

        new_posts.append(
            {
                'time': post_time,
                'text': text[:300] + "..." if len(text) > 300 else text,
                'link': link,
            }
        )

    logger.info("Channel @%s produced %s new posts for the digest", clean_username, len(new_posts))
    return new_posts
