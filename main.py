import asyncio
import calendar
import os
import html
import logging
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
    get_user_subscriptions, delete_subscription,
    add_saved_message, get_user_tags, get_saved_messages, get_saved_message_by_id, delete_saved_message # <-- Добавлены импорты
)
from scraper import get_latest_posts

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

# 👇👇👇 НОВЫЙ КОД: Состояние для ввода тега 👇👇👇
class SaveState(StatesGroup):
    waiting_for_tag = State()
# 👆👆👆 КОНЕЦ НОВОГО КОДА 👆👆👆

def chunk_html_text(lines, max_length=4000):
    chunks, current = [], ""
    for line in lines:
        if len(current) + len(line) > max_length:
            chunks.append(current)
            current = line + "\n"
        else:
            current += line + "\n"
    if current:
        chunks.append(current)
    return chunks

def get_next_digest_time(period: str, now: datetime | None = None) -> datetime:
    now = now or datetime.now(TZ)
    base_time = now.replace(hour=7, minute=0, second=0, microsecond=0)

    if period == "daily":
        return base_time + timedelta(days=1)

    if period == "weekly":
        return base_time + timedelta(days=7)

    if period == "monthly":
        year = base_time.year + (1 if base_time.month == 12 else 0)
        month = 1 if base_time.month == 12 else base_time.month + 1
        day = min(base_time.day, calendar.monthrange(year, month)[1])
        return base_time.replace(year=year, month=month, day=day)

    raise ValueError(f"Unsupported digest period: {period}")

def get_message_preview(msg: types.Message):
    text = msg.text or msg.caption or "🖼 Медиафайл"
    preview = text.replace('\n', ' ')[:40] + "..." if len(text) > 40 else text.replace('\n', ' ')
    
    source = "Твой текст"
    if msg.forward_origin:
        if msg.forward_origin.type == "channel":
            source = getattr(msg.forward_origin.chat, 'title', 'Канал')
        elif msg.forward_origin.type == "user":
            source = getattr(msg.forward_origin.sender_user, 'first_name', 'Пользователь')
        elif msg.forward_origin.type == "hidden_user":
            source = getattr(msg.forward_origin, 'sender_user_name', 'Скрытый пользователь')
        elif msg.forward_origin.type == "chat":
            source = getattr(msg.forward_origin.chat, 'title', 'Группа')
            
    return preview, source

@dp.message(CommandStart())
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Привет! Я готов.\n\n"
        "1️⃣ Перешли мне любое сообщение, чтобы отложить его или сохранить в базу знаний.\n"
        "2️⃣ Перешли пост из открытого канала, чтобы подписаться на его дайджест.\n"
        "3️⃣ Напиши /list для задач или /saved для Избранного."
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
        text_lines = ["⏳ <b>Твои разовые напоминания:</b>\n"]
        buttons = []
        for idx, msg in enumerate(user_msgs, 1):
            db_id, send_at, preview, source = msg
            dt_obj = datetime.strptime(send_at, '%Y-%m-%d %H:%M:%S')
            source_safe = html.escape(source or "Неизвестно")
            preview_safe = html.escape(preview or "Без текста")
            
            text_lines.append(f"{idx}. 📌 <b>{dt_obj.strftime('%d.%m в %H:%M')}</b> | От: {source_safe}\n<i>{preview_safe}</i>\n")
            buttons.append(InlineKeyboardButton(text=f"❌ {idx}", callback_data=f"cancel_{db_id}"))
            
        kb_rows = [buttons[i:i + 5] for i in range(0, len(buttons), 5)]
        await message.answer("\n".join(text_lines), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))

    if user_subs:
        text_lines = ["📡 <b>Твои подписки на дайджесты:</b>\n"]
        buttons = []
        period_ru = {"daily": "каждый день", "weekly": "раз в неделю", "monthly": "раз в месяц"}
        for idx, sub in enumerate(user_subs, 1):
            sub_id, username, title, period, next_send_at = sub
            dt_obj = datetime.strptime(next_send_at, '%Y-%m-%d %H:%M:%S')
            
            text_lines.append(f"{idx}. 📰 <b>{html.escape(title or 'Канал')}</b> ({period_ru.get(period, period)})\nСлед: {dt_obj.strftime('%d.%m в %H:%M')}\n")
            buttons.append(InlineKeyboardButton(text=f"❌ {idx}", callback_data=f"unsub_{sub_id}"))
            
        kb_rows = [buttons[i:i + 5] for i in range(0, len(buttons), 5)]
        await message.answer("\n".join(text_lines), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))

@dp.callback_query(F.data.startswith("cancel_"))
async def handle_cancel(callback: CallbackQuery):
    db_id = int(callback.data.split("_")[1])
    delete_message(db_id)
    
    user_msgs = get_user_messages(callback.message.chat.id)
    if not user_msgs:
        await callback.message.edit_text("📭 Все разовые напоминания отменены.")
        return
        
    text_lines = ["⏳ <b>Твои разовые напоминания:</b>\n"]
    buttons = []
    for idx, msg in enumerate(user_msgs, 1):
        db_id, send_at, preview, source = msg
        dt_obj = datetime.strptime(send_at, '%Y-%m-%d %H:%M:%S')
        text_lines.append(f"{idx}. 📌 <b>{dt_obj.strftime('%d.%m в %H:%M')}</b> | От: {html.escape(source or 'Неизвестно')}\n<i>{html.escape(preview or 'Без текста')}</i>\n")
        buttons.append(InlineKeyboardButton(text=f"❌ {idx}", callback_data=f"cancel_{db_id}"))
        
    kb_rows = [buttons[i:i + 5] for i in range(0, len(buttons), 5)]
    try:
        await callback.message.edit_text("\n".join(text_lines), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))
    except TelegramAPIError:
        pass
    await callback.answer("Удалено!")

@dp.callback_query(F.data.startswith("unsub_"))
async def handle_unsub(callback: CallbackQuery):
    sub_id = int(callback.data.split("_")[1])
    delete_subscription(sub_id)
    
    user_subs = get_user_subscriptions(callback.message.chat.id)
    if not user_subs:
        await callback.message.edit_text("📭 Все подписки на дайджесты отменены.")
        return
        
    text_lines = ["📡 <b>Твои подписки на дайджесты:</b>\n"]
    buttons = []
    period_ru = {"daily": "каждый день", "weekly": "раз в неделю", "monthly": "раз в месяц"}
    for idx, sub in enumerate(user_subs, 1):
        sub_id, username, title, period, next_send_at = sub
        dt_obj = datetime.strptime(next_send_at, '%Y-%m-%d %H:%M:%S')
        text_lines.append(f"{idx}. 📰 <b>{html.escape(title or 'Канал')}</b> ({period_ru.get(period, period)})\nСлед: {dt_obj.strftime('%d.%m в %H:%M')}\n")
        buttons.append(InlineKeyboardButton(text=f"❌ {idx}", callback_data=f"unsub_{sub_id}"))
        
    kb_rows = [buttons[i:i + 5] for i in range(0, len(buttons), 5)]
    try:
        await callback.message.edit_text("\n".join(text_lines), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))
    except TelegramAPIError:
        pass
    await callback.answer("Удалено!")

# 👇👇👇 НОВЫЙ КОД: Логика команды /saved и просмотра сохраненных текстов 👇👇👇
@dp.message(Command("saved"))
async def cmd_saved(message: types.Message, state: FSMContext):
    await state.clear()
    user_id = message.chat.id
    saved_msgs = get_saved_messages(user_id)
    
    if not saved_msgs:
        await message.answer("📭 Твоя база знаний пока пуста.")
        return
        
    # Группируем сообщения по тегам
    grouped = {}
    for msg in saved_msgs:
        tag = msg[1]
        if tag not in grouped:
            grouped[tag] = []
        grouped[tag].append(msg)
        
    text_lines = ["📁 <b>Твоя база знаний:</b>\n"]
    buttons = []
    
    idx = 1
    for tag, msgs in grouped.items():
        text_lines.append(f"🏷 <b>{html.escape(tag)}</b>")
        for m in msgs:
            db_id, _, full_text, source, saved_at = m
            preview = full_text.replace('\n', ' ')[:40] + "..." if len(full_text) > 40 else full_text.replace('\n', ' ')
            source_safe = html.escape(source or "Неизвестно")
            preview_safe = html.escape(preview or "Без текста")
            
            text_lines.append(f"{idx}. От: {source_safe} | <i>{preview_safe}</i>")
            # Кнопка чтения и удаления для каждой закладки
            buttons.append([
                InlineKeyboardButton(text=f"📖 Читать {idx}", callback_data=f"read_saved_{db_id}"),
                InlineKeyboardButton(text=f"❌ {idx}", callback_data=f"del_saved_{db_id}")
            ])
            idx += 1
        text_lines.append("") 
        
    await message.answer("\n".join(text_lines), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@dp.callback_query(F.data.startswith("read_saved_"))
async def read_saved(callback: CallbackQuery):
    db_id = int(callback.data.split("_")[2])
    msg_data = get_saved_message_by_id(db_id)
    if not msg_data:
        await callback.answer("❌ Сообщение не найдено", show_alert=True)
        return
        
    full_text, source, tag, saved_at = msg_data
    dt_obj = datetime.strptime(saved_at, '%Y-%m-%d %H:%M:%S')
    
    # Формируем красивую карточку полного поста
    text = f"🏷 <b>Тег:</b> {html.escape(tag)}\n" \
           f"👤 <b>Источник:</b> {html.escape(source)}\n" \
           f"📅 <b>Сохранено:</b> {dt_obj.strftime('%d.%m.%Y в %H:%M')}\n\n" \
           f"📝 <b>Текст:</b>\n{html.escape(full_text)}"
           
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Закрыть", callback_data="close_msg")]])
    
    chunks = chunk_html_text(text.split('\n'))
    for i, chunk in enumerate(chunks):
        if i == len(chunks) - 1:
            await callback.message.answer(chunk, parse_mode="HTML", reply_markup=kb)
        else:
            await callback.message.answer(chunk, parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "close_msg")
async def close_msg(callback: CallbackQuery):
    try:
        await callback.message.delete()
    except TelegramAPIError:
        pass

@dp.callback_query(F.data.startswith("del_saved_"))
async def handle_del_saved(callback: CallbackQuery, state: FSMContext): # <-- Добавили state сюда
    db_id = int(callback.data.split("_")[2])
    delete_saved_message(db_id)
    await callback.answer("✅ Закладка удалена!")
    # Передаем state напрямую
    await cmd_saved(callback.message, state)
    
# 👆👆👆 КОНЕЦ НОВОГО КОДА 👆👆👆

@dp.message(ScheduleState.waiting_for_datetime)
async def process_custom_datetime(message: types.Message, state: FSMContext):
    try:
        scheduled_time = datetime.strptime(message.text, "%d.%m.%Y %H:%M").replace(tzinfo=TZ)
        if scheduled_time < datetime.now(TZ):
            await message.answer("Эта дата уже в прошлом! Попробуй еще раз (ДД.ММ.ГГГГ ЧЧ:ММ):")
            return
            
        data = await state.get_data()
        msg_id = data.get('message_id')
        preview = data.get('preview', '')
        source = data.get('source', '')
        prompt_msg_id = data.get('prompt_msg_id') 
        
        if not msg_id:
            await message.answer("Ошибка: не найден ID сообщения. Попробуй переслать его заново.")
            await state.clear()
            return
            
        add_message(message.chat.id, msg_id, scheduled_time.strftime('%Y-%m-%d %H:%M:%S'), preview, source)
        
        try:
            await message.delete() 
            if prompt_msg_id:
                await bot.delete_message(message.chat.id, prompt_msg_id)
        except TelegramAPIError:
            pass
            
        confirm_msg = await message.answer(f"✅ Принято! Запланировано на {scheduled_time.strftime('%d.%m.%Y в %H:%M')}.")
        await state.clear()
        
        await asyncio.sleep(3)
        try:
            await confirm_msg.delete()
        except TelegramAPIError:
            pass
            
    except ValueError:
        await message.answer("❌ Неверный формат. Пример: 15.03.2026 14:30")

# 👇👇👇 НОВЫЙ КОД: Сохранение тега (Ввод текста) 👇👇👇
@dp.message(SaveState.waiting_for_tag)
async def process_tag(message: types.Message, state: FSMContext):
    tag = message.text.strip()
    data = await state.get_data()
    
    orig_msg_id = data.get('orig_msg_id')
    full_text = data.get('full_text')
    source = data.get('source')
    prompt_msg_id = data.get('prompt_msg_id')
    
    saved_at = datetime.now(TZ).strftime('%Y-%m-%d %H:%M:%S')
    add_saved_message(message.chat.id, full_text, source, tag, saved_at)
    
    try:
        await message.delete() 
        if prompt_msg_id:
            await bot.delete_message(message.chat.id, prompt_msg_id) 
        if orig_msg_id:
            await bot.delete_message(message.chat.id, orig_msg_id) 
    except TelegramAPIError:
        pass
        
    confirm_msg = await message.answer(f"✅ Сохранено в базу знаний под тегом <b>{html.escape(tag)}</b>", parse_mode="HTML")
    await state.clear()
    
    await asyncio.sleep(3)
    try:
        await confirm_msg.delete()
    except TelegramAPIError:
        pass
# 👆👆👆 КОНЕЦ НОВОГО КОДА 👆👆👆

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
                digest_lines.append("") 
        except Exception as e:
            has_news = True  
            digest_lines.append(f"❌ Ошибка парсинга канала <b>{title_safe}</b>: {e}\n")
            
    if not has_news:
        await message.answer("📭 За последние 24 часа новых постов ни в одном из каналов не было.")
        return
        
    for chunk in chunk_html_text(digest_lines):
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
    
    # 👇👇👇 НОВЫЙ КОД: Добавили кнопку "📁 В закладки" 👇👇👇
    kb = [
        [
            InlineKeyboardButton(text="🌅 Утром в 9", callback_data="time_morning"),
            InlineKeyboardButton(text="☀️ Днем в 14", callback_data="time_day"),
            InlineKeyboardButton(text="🌙 Вечером в 20", callback_data="time_evening")
        ],
        [
            InlineKeyboardButton(text="⏱ Через 3 часа", callback_data="time_now"),
            InlineKeyboardButton(text="📅 Точная дата", callback_data="time_custom")
        ],
        [InlineKeyboardButton(text="📁 В закладки (База знаний)", callback_data="bookmark_setup")]
    ]
    # 👆👆👆 КОНЕЦ НОВОГО КОДА 👆👆👆
    
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
    next_send = get_next_digest_time(period, now)

    last_scraped = now.strftime('%Y-%m-%d %H:%M:%S')
    add_subscription(callback.message.chat.id, username, title, period, last_scraped, next_send.strftime('%Y-%m-%d %H:%M:%S'))
    
    period_ru = {"daily": "каждый день", "weekly": "раз в неделю", "monthly": "раз в месяц"}
    
    await callback.answer(f"✅ Дайджест {title} оформлен!\nБудет приходить {period_ru[period]} в 07:00.", show_alert=True)
    
    try:
        if callback.message.reply_to_message:
            await bot.delete_message(callback.message.chat.id, callback.message.reply_to_message.message_id)
        await callback.message.delete()
    except TelegramAPIError:
        pass
        
    await state.clear()

# 👇👇👇 НОВЫЙ КОД: Обработчик нажатия на "В закладки" (Показывает теги) 👇👇👇
@dp.callback_query(F.data == "bookmark_setup")
async def setup_bookmark(callback: CallbackQuery, state: FSMContext):
    orig_msg = callback.message.reply_to_message
    if not orig_msg:
        await callback.message.edit_text("❌ Ошибка: не могу найти оригинальное сообщение.")
        return
        
    # Сохраняем полный текст, чтобы не зависеть от источника
    full_text = orig_msg.text or orig_msg.caption or "🖼 Медиафайл (без текста)"
    _, source = get_message_preview(orig_msg)
    
    await state.update_data(
        orig_msg_id=orig_msg.message_id, 
        full_text=full_text, 
        source=source,
        prompt_msg_id=callback.message.message_id
    )
    await state.set_state(SaveState.waiting_for_tag)
    
    # Достаем уже существующие теги пользователя
    tags = get_user_tags(callback.message.chat.id)
    tags_text = ""
    if tags:
        tags_formatted = "  ".join([f"`{t}`" for t in tags])
        tags_text = f"\n\n📝 *Твои прошлые теги* (нажми, чтобы скопировать):\n{tags_formatted}"
        
    await callback.message.edit_text(
        f"Напиши тег для этого сообщения (например: Идеи, Статьи, Важное).{tags_text}",
        parse_mode="Markdown"
    )
# 👆👆👆 КОНЕЦ НОВОГО КОДА 👆👆👆

@dp.callback_query(F.data.startswith("time_"))
async def handle_time_selection(callback: CallbackQuery, state: FSMContext):
    now = datetime.now(TZ)
    scheduled_time = None
    label = ""

    if callback.message.reply_to_message is None:
        await callback.message.edit_text("❌ Ошибка: не могу найти оригинальное сообщение.")
        return
        
    orig_msg = callback.message.reply_to_message
    preview, source = get_message_preview(orig_msg)

    if callback.data == "time_custom":
        await state.update_data(message_id=orig_msg.message_id, preview=preview, source=source, prompt_msg_id=callback.message.message_id)
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
        scheduled_time = now + timedelta(hours=3)
        label = "через 3 часа"

    if scheduled_time:
        try:
            await callback.message.edit_text(f"✅ Принято! Отправлю это сообщение тебе {label}.")
            add_message(callback.message.chat.id, orig_msg.message_id, scheduled_time.strftime('%Y-%m-%d %H:%M:%S'), preview, source)
            
            await asyncio.sleep(3)
            await callback.message.delete()
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
        
        users_subs = {}
        for sub in due_subs:
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
                        digest_lines.append("") 
                        
                    now = datetime.now(TZ)
                    next_send_str = get_next_digest_time(period, now).strftime('%Y-%m-%d %H:%M:%S')
                    update_subscription_time(sub_id, now_str, next_send_str)
                    
                except Exception as e:
                    print(f"Ошибка дайджеста для {username}: {e}")
            
            if has_news:
                for chunk in chunk_html_text(digest_lines):
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