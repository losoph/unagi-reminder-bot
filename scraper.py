import aiohttp
from bs4 import BeautifulSoup

from config import APP_TIMEZONE
from datetime_utils import parse_db_datetime, parse_telegram_datetime


async def get_latest_posts(channel_username, last_scraped_at_str):
    clean_username = channel_username.replace("@", "")
    url = f"https://t.me/s/{clean_username}"

    print(f"\n[SCRAPER] ------------------------------------")
    print(f"[SCRAPER] 🌍 Захожу на: {url}")

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    }

    last_scraped_at = parse_db_datetime(last_scraped_at_str)
    print(
        f"[SCRAPER] ⏳ Ищу посты строго новее чем: {last_scraped_at.astimezone(APP_TIMEZONE).isoformat()} ({APP_TIMEZONE.key})"
    )

    timeout = aiohttp.ClientTimeout(total=10)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        try:
            async with session.get(url, headers=headers) as response:
                print(f"[SCRAPER] 📡 Статус ответа сервера Телеграм: {response.status}")
                if response.status != 200:
                    print(f"[SCRAPER] ❌ Ошибка: Сервер вернул код {response.status}")
                    return []
                html = await response.text()
        except Exception as e:
            print(f"[SCRAPER] ❌ Ошибка сети: {e}")
            return []

    soup = BeautifulSoup(html, 'html.parser')
    messages = soup.find_all('div', class_='tgme_widget_message')
    print(f"[SCRAPER] 🧩 На странице найдено {len(messages)} блоков сообщений")

    new_posts = []
    for msg in messages:
        date_elem = msg.find('time', class_='time')
        if not date_elem or not date_elem.has_attr('datetime'):
            continue

        try:
            post_time = parse_telegram_datetime(date_elem['datetime'], target_tz=APP_TIMEZONE)
        except Exception:
            continue

        if post_time.astimezone(last_scraped_at.tzinfo) > last_scraped_at:
            text_elem = msg.find('div', class_='tgme_widget_message_text')
            text = text_elem.get_text(separator=' ', strip=True) if text_elem else "Медиа-файл или сообщение без текста"

            link = f"https://t.me/{clean_username}"
            link_elem = msg.find('a', class_='tgme_widget_message_date')
            if link_elem and link_elem.has_attr('href'):
                link = link_elem['href']

            new_posts.append({
                'time': post_time,
                'text': text[:300] + "..." if len(text) > 300 else text,
                'link': link
            })

    print(f"[SCRAPER] ✅ Из них {len(new_posts)} прошли фильтр времени и добавлены в дайджест")
    print(f"[SCRAPER] ------------------------------------\n")
    return new_posts
