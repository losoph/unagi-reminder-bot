import aiohttp
from bs4 import BeautifulSoup
from datetime import datetime, timedelta

async def get_latest_posts(channel_username, last_scraped_at_str):
    url = f"https://t.me/s/{channel_username}"
    # Притворяемся обычным браузером, чтобы Telegram нас не заблокировал
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    }
    
    last_scraped_at = datetime.strptime(last_scraped_at_str, '%Y-%m-%d %H:%M:%S') if last_scraped_at_str else datetime.min
    
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, headers=headers, timeout=10) as response:
                if response.status != 200:
                    return []
                html = await response.text()
        except Exception as e:
            print(f"Ошибка парсинга канала {channel_username}: {e}")
            return []
            
    soup = BeautifulSoup(html, 'html.parser')
    messages = soup.find_all('div', class_='tgme_widget_message')
    
    new_posts = []
    for msg in messages:
        # Ищем дату поста
        date_elem = msg.find('time', class_='time')
        if not date_elem or not date_elem.has_attr('datetime'):
            continue
        
        # Telegram отдает время в формате UTC (например, 2026-03-07T14:30:00+00:00). 
        # Переводим его в наше Московское время (+3 часа) для корректного сравнения с базой
        post_time_str = date_elem['datetime'][:19]
        post_time = datetime.strptime(post_time_str, '%Y-%m-%dT%H:%M:%S') + timedelta(hours=3)
        
        if post_time > last_scraped_at:
            # Достаем текст
            text_elem = msg.find('div', class_='tgme_widget_message_text')
            text = text_elem.get_text(separator=' ', strip=True) if text_elem else "Медиа-файл или сообщение без текста"
            
            # Достаем прямую ссылку на пост
            link = f"https://t.me/{channel_username}"
            link_elem = msg.find('a', class_='tgme_widget_message_date')
            if link_elem and link_elem.has_attr('href'):
                link = link_elem['href']
                
            new_posts.append({
                'time': post_time,
                'text': text[:300] + "..." if len(text) > 300 else text, # Обрезаем длинный текст для превью
                'link': link
            })
            
    return new_posts