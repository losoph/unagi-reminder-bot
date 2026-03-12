import asyncio
import os
import html
import logging # <-- Включаем систему логов
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, LinkPreviewOptions
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.exceptions import TelegramAPIError

from database import (
    add_message, get_pending_messages, mark_as_sent, init_db, 
    get_user_messages, delete_message,
    add_subscription, get_due_subscriptions, update_subscription_time, 
    get_user_subscriptions, delete_subscription
)
from scraper import get_latest_posts

# ЗАСТАВЛЯЕМ БОТА ВЫВОДИТЬ ВСЕ ОШИБКИ В КОНСОЛЬ RAILWAY
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise ValueError("❌ Токен бота не найден! Проверьте файл .env")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
TZ = ZoneInfo("Europe/Moscow")

class ScheduleState(StatesGroup):
    waiting_for_datetime = State()

@dp.message(CommandStart())
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Привет! Я готов.\n\n"
        "1️⃣ Перешли мне любое сообщение, чтобы отложить его.\n"
        "2️⃣ Перешли пост из открытого канала, чтобы подписаться на его дайджест.\n"
        "3️⃣ Напиши /list для управления задачами."
    )

@dp.message(Command("list"))
async def cmd_list(message: types.Message, state: FSMContext):
    await state.clear()
    user_id = message.chat.id
    
    user_msgs = get_user_messages(user_id)
    user_subs = get_user_subscriptions(user_id)
    
    if not user_msgs and not user_subs:
        await message.answer("📭 У тебя нет активных напоминаний или подписок.")
        return
        
    if user_msgs:
        await message.answer("⏳ **Твои разовые напоминания:**", parse_mode="Markdown")
        for msg in user_msgs:
            db_id, send_at = msg
            dt_obj = datetime.strptime(send_at, '%Y-%m-%d %H:%M:%S')
            pretty_time = dt_obj.strftime('%d.%m.%Y в %H:%M')
            
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отменить", callback_data=f"cancel_{db_id}")]])
            await message.answer(f"📌 Напоминание на: {pretty_time}", reply_markup=kb)

    if user_subs:
        await message.answer("📡 **Твои подписки на дайджесты:**", parse_mode="Markdown")
        period_ru = {"daily": "каждый день", "weekly": "раз в неделю", "monthly": "раз в месяц"}
        for sub in user_subs:
            sub_id, username, title, period, next_send_at = sub
            dt_obj = datetime.strptime(next_send_at, '%Y-%m-%d %H:%M:%S')
            pretty_time = dt_obj.strftime('%d.%m.%Y в %H:%M')
            
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отменить сбор", callback_data=f"unsub_{sub_id}")]])
            await message.answer(f"📰 Канал: *{title}*\n🔄 Частота: {period_ru.get(period, period)}\nСлед. отчет: {pretty_time}", reply_markup=kb, parse_mode="Markdown")

@dp.callback_query(F.data.startswith("cancel_"))
async def handle_cancel(callback: CallbackQuery):
    db_id = int(callback.data.split("_")[1])
    delete_message(db_id)
    try:
        await callback.message.edit_text("🚫 Напоминание отменено.")
    except TelegramAPIError:
        pass

@dp.callback_query(F.data.startswith("unsub_"))
async def handle_unsub(callback: CallbackQuery):
    sub_id = int(callback.data.split("_")[1])
    delete_subscription(sub_id)
    try:
        await callback.message.edit_text("🚫 Подписка на дайджест отменена.")
    except TelegramAPIError:
        pass

@dp.message(ScheduleState.waiting_for_datetime)
async def process_custom_datetime(message: types.Message, state: FSMContext):
    try:
        scheduled_time = datetime.strptime(message.text, "%d.%m.%Y %H:%M").replace(tzinfo=TZ)
        if scheduled_time < datetime.now(TZ):
            await message.answer("Эта дата уже в прошлом! Попробуй еще раз (ДД.ММ.ГГГГ ЧЧ:ММ):")
            return
            
        data = await state.get_data()
        msg_id = data.get('message_id')
        chat_id = message.chat.id
        
        if not msg_id:
            await message.answer("Ошибка: не найден ID сообщения. Попробуй переслать его заново.")
            await state.clear()
            return
            
        add_message(user_id=chat_id, message_id=msg_id, send_at=scheduled_time.strftime('%Y-%m-%d %H:%M:%S'))
        await message.answer(f"✅ Принято! Запланировано на {scheduled_time.strftime('%d.%m.%Y в %H:%M')}.")
        await state.clear()
    except ValueError:
        await message.answer("❌ Неверный формат. Пример: 15.03.2026 14:30")

# --- КОМАНДА ДЛЯ СРОЧНОГО СБОРА (С МАЯЧКАМИ DEBUG) ---
@dp.message(Command("test_digest"))
async def cmd_test_digest(message: types.Message, state: FSMContext):
    await state.clear()
    user_id = message.chat.id
    user_subs = get_user_subscriptions(user_id)
    
    if not user_subs:
        await message.answer("📭 У тебя нет активных подписок для парсинга.")
        return
        
    await message.answer("⏳ Собираю единый дайджест за последние 24 часа...")
    
    now_tz = datetime.now(TZ)
    last_24h_str = (now_tz - timedelta(days=1)).strftime('%Y-%m-%d %H:%M:%S')
    
    digest_lines = [f"📰 <b>Твоя тестовая утренняя газета</b> ☕️\n\n"]
    has_news = False
    
    for sub in user_subs:
        sub_id, username, title, period, next_send_at = sub
        title_safe = html.escape(title) if title else "Без названия"
        
        try:
            posts = await get_latest_posts(username, last_24h_str)
            if posts:
                has_news = True
                digest_lines.append(f"📌 <b>{title_safe}</b>")
                for p in posts:
                    text_safe = html.escape(p['text'])
                    digest_lines.append(f"🔹 <i>{text_safe}</i> <a href='{p['link']}'>[Читать]</a>\n")
                digest_lines.append("") # Пустая строка между каналами
        except Exception as e:
            digest_lines.append(f"❌ Ошибка парсинга канала <b>{title_safe}</b>: {e}\n")
            
    if not has_news:
        await message.answer("📭 За последние 24 часа новых постов ни в одном из каналов не было.")
        return
        
    # Умная разбивка текста: не режем HTML-теги пополам
    chunks = []
    current_chunk = ""
    for line in digest_lines:
        if len(current_chunk) + len(line) > 4000:
            chunks.append(current_chunk)
            current_chunk = line + "\n"
        else:
            current_chunk += line + "\n"
    if current_chunk:
        chunks.append(current_chunk)
        
    for chunk in chunks:
        await message.answer(chunk, parse_mode="HTML", link_preview_options=LinkPreviewOptions(is_disabled=True))

@dp.message()
async def catch_message(message: types.Message, state: FSMContext):
    await state.clear() 
    
    is_public_channel = False
    channel_username = None
    channel_title = None

    if message.forward_origin and message.forward_origin.type == "channel":
        if getattr(message.forward_origin.chat, 'username', None):
            is_public_channel = True
            channel_username = message.forward_origin.chat.username
            channel_title = message.forward_origin.chat.title
    
    kb = [
        [
            InlineKeyboardButton(text="🌅 Утро", callback_data="time_morning"),
            InlineKeyboardButton(text="☀️ День", callback_data="time_day"),
            InlineKeyboardButton(text="🌙 Вечер", callback_data="time_evening")
        ],
        [InlineKeyboardButton(text="⏱ Через минуту (тест)", callback_data="time_now")],
        [InlineKeyboardButton(text="📅 Точная дата и время", callback_data="time_custom")]
    ]
    
    if is_public_channel:
        kb.append([InlineKeyboardButton(text="📡 Собирать дайджест", callback_data="digest_setup")])
        await state.update_data(channel_username=channel_username, channel_title=channel_title)
        
    await message.reply("Что мне сделать с этим сообщением?", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@dp.callback_query(F.data == "digest_setup")
async def setup_digest(callback: CallbackQuery, state: FSMContext):
    kb = [
        [InlineKeyboardButton(text="📅 Раз в день", callback_data="period_daily")],
        [InlineKeyboardButton(text="🗓 Раз в неделю", callback_data="period_weekly")],
        [InlineKeyboardButton(text="📊 Раз в месяц", callback_data="period_monthly")]
    ]
    await callback.message.edit_text("Как часто присылать новые посты из этого канала?", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@dp.callback_query(F.data.startswith("period_"))
async def save_subscription(callback: CallbackQuery, state: FSMContext):
    period = callback.data.split("_")[1]
    data = await state.get_data()
    username = data.get("channel_username")
    title = data.get("channel_title")
    
    if not username:
        await callback.message.edit_text("❌ Ошибка: данные канала утеряны.")
        return
        
    now = datetime.now(TZ)
    next_send = now.replace(hour=7, minute=0, second=0, microsecond=0)
    if next_send <= now:
        next_send += timedelta(days=1)
        
    last_scraped = now.strftime('%Y-%m-%d %H:%M:%S')
    
    add_subscription(
        user_id=callback.message.chat.id,
        channel_username=username,
        channel_title=title,
        period=period,
        last_scraped_at=last_scraped,
        next_send_at=next_send.strftime('%Y-%m-%d %H:%M:%S')
    )
    
    period_ru = {"daily": "каждый день", "weekly": "раз в неделю", "monthly": "раз в месяц"}
    try:
        await callback.message.edit_text(f"✅ Подписка оформлена!\nДайджест канала *{title}* будет приходить {period_ru[period]} в 07:00.", parse_mode="Markdown")
    except TelegramAPIError:
        pass
    await state.clear()

@dp.callback_query(F.data.startswith("time_"))
async def handle_time_selection(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    now = datetime.now(TZ)
    scheduled_time = None
    label = ""

    if callback.data == "time_custom":
        if callback.message.reply_to_message is None:
            await callback.message.edit_text("❌ Ошибка: не могу найти оригинальное сообщение.")
            return

        msg_id = callback.message.reply_to_message.message_id
        await state.update_data(message_id=msg_id)
        await state.set_state(ScheduleState.waiting_for_datetime)
        
        if now.hour < 8:
            suggested_time = now.replace(hour=9, minute=0, second=0, microsecond=0)
        elif now.hour < 13:
            suggested_time = now.replace(hour=14, minute=0, second=0, microsecond=0)
        elif now.hour < 20:
            suggested_time = now.replace(hour=20, minute=0, second=0, microsecond=0)
        else:
            suggested_time = (now + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
            
        suggested_str = suggested_time.strftime('%d.%m.%Y %H:%M')
        await callback.message.edit_text(
            "Напиши точную дату и время в формате ДД.ММ.ГГГГ ЧЧ:ММ\n\n"
            "💡 *Лайфхак:* нажми на время ниже, чтобы скопировать его:\n\n"
            f"`{suggested_str}`", 
            parse_mode="Markdown"
        )
        return

    elif callback.data == "time_morning":
        scheduled_time = (now + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
        label = "завтра на 09:00"
    elif callback.data == "time_day":
        scheduled_time = now.replace(hour=14, minute=0, second=0, microsecond=0)
        if now.hour >= 14:
            scheduled_time += timedelta(days=1)
        label = "на 14:00"
    elif callback.data == "time_evening":
        scheduled_time = now.replace(hour=20, minute=0, second=0, microsecond=0)
        if now.hour >= 20:
            scheduled_time += timedelta(days=1)
        label = "на 20:00"
    elif callback.data == "time_now":
        scheduled_time = now + timedelta(minutes=1)
        label = "через 1 минуту (тест)"

    if scheduled_time:
        if callback.message.reply_to_message is None:
            await callback.message.edit_text("❌ Ошибка: не могу найти оригинальное сообщение.")
            return

        chat_id = callback.message.chat.id
        msg_id = callback.message.reply_to_message.message_id
        
        try:
            await callback.message.edit_text(f"✅ Принято! Отправлю это сообщение тебе {label}.")
            add_message(user_id=chat_id, message_id=msg_id, send_at=scheduled_time.strftime('%Y-%m-%d %H:%M:%S'))
        except TelegramAPIError:
            pass

async def check_messages():
    while True:
        now_str = datetime.now(TZ).strftime('%Y-%m-%d %H:%M:%S')
        pending = get_pending_messages(now_str)
        
        for msg in pending:
            db_id, chat_id, message_id = msg
            try:
                chat_id = int(chat_id)
                await bot.forward_message(chat_id=chat_id, from_chat_id=chat_id, message_id=message_id)
                mark_as_sent(db_id)
            except Exception as e:
                print(f"❌ Ошибка отправки {message_id}: {e}")
                mark_as_sent(db_id)
        
        await asyncio.sleep(30)

async def check_digests():
    while True:
        now_str = datetime.now(TZ).strftime('%Y-%m-%d %H:%M:%S')
        due_subs = get_due_subscriptions(now_str)
        
        # Группируем задачи по пользователям
        users_subs = {}
        for sub in due_subs:
            # sub = (id, user_id, channel_username, channel_title, period, last_scraped_at)
            user_id = sub[1]
            if user_id not in users_subs:
                users_subs[user_id] = []
            users_subs[user_id].append(sub)
            
        for user_id, subs in users_subs.items():
            digest_lines = [f"📰 <b>Твоя утренняя газета</b> ☕️\n\n"]
            has_news = False
            
            for sub in subs:
                sub_id, uid, username, title, period, last_scraped = sub
                title_safe = html.escape(title) if title else "Канал"
                
                try:
                    posts = await get_latest_posts(username, last_scraped)
                    if posts:
                        has_news = True
                        digest_lines.append(f"📌 <b>{title_safe}</b>")
                        for p in posts:
                            text_safe = html.escape(p['text'])
                            digest_lines.append(f"🔹 <i>{text_safe}</i> <a href='{p['link']}'>[Читать]</a>\n")
                        digest_lines.append("") # Пустая строка
                        
                    # Обновляем таймер в базе в любом случае
                    now = datetime.now(TZ)
                    if period == "daily":
                        next_time = now + timedelta(days=1)
                    elif period == "weekly":
                        next_time = now + timedelta(days=7)
                    else:
                        next_time = now + timedelta(days=30)
                        
                    next_send_str = next_time.replace(hour=7, minute=0, second=0, microsecond=0).strftime('%Y-%m-%d %H:%M:%S')
                    update_subscription_time(sub_id, now_str, next_send_str)
                    
                except Exception as e:
                    print(f"Ошибка дайджеста для {username}: {e}")
            
            # Отправляем газету ТОЛЬКО если есть новости
            if has_news:
                chunks = []
                current_chunk = ""
                for line in digest_lines:
                    if len(current_chunk) + len(line) > 4000:
                        chunks.append(current_chunk)
                        current_chunk = line + "\n"
                    else:
                        current_chunk += line + "\n"
                if current_chunk:
                    chunks.append(current_chunk)
                    
                for chunk in chunks:
                    try:
                        await bot.send_message(user_id, chunk, parse_mode="HTML", link_preview_options=LinkPreviewOptions(is_disabled=True))
                    except Exception as e:
                        print(f"Ошибка отправки лонгрида пользователю {user_id}: {e}")
        
        await asyncio.sleep(60)
        
async def main():
    init_db()
    await bot.delete_webhook(drop_pending_updates=True)
    asyncio.create_task(check_messages())
    asyncio.create_task(check_digests()) 
    print("Бот успешно запущен и ждет сообщений...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())