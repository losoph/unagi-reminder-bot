import asyncio
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo # Встроенная библиотека для часовых поясов
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.exceptions import TelegramAPIError

from database import add_message, get_pending_messages, mark_as_sent, init_db, get_user_messages, delete_message

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise ValueError("❌ Токен бота не найден! Проверьте файл .env")

bot = Bot(token=BOT_TOKEN)

# TODO: Для больших проектов память (MemoryStorage) заменяют на Redis, 
# чтобы при перезагрузке сервера Railway пользователи не теряли свои текущие "состояния" ввода.
dp = Dispatcher()

# Фиксируем часовой пояс
TZ = ZoneInfo("Europe/Moscow")

class ScheduleState(StatesGroup):
    waiting_for_datetime = State()

@dp.message(CommandStart())
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Привет! Я готов.\n\n"
        "1️⃣ Перешли мне любое сообщение, и я предложу время для отложенной отправки.\n"
        "2️⃣ Напиши /list, чтобы посмотреть или отменить запланированные задачи."
    )

# НОВАЯ ФУНКЦИЯ: Управление списком задач
@dp.message(Command("list"))
async def cmd_list(message: types.Message, state: FSMContext):
    await state.clear()
    
    # TODO: В будущем, при переходе на асинхронную БД (aiosqlite), эта функция не будет 
    # блокировать event-loop при высоких нагрузках. Для MVP синхронного sqlite3 более чем достаточно.
    user_msgs = get_user_messages(message.chat.id)
    
    if not user_msgs:
        await message.answer("📭 У тебя нет активных напоминаний.")
        return
        
    await message.answer("⏳ Твои запланированные напоминания:")
    
    # Выдаем каждое напоминание отдельным сообщением с кнопкой отмены
    for msg in user_msgs:
        db_id, send_at = msg
        dt_obj = datetime.strptime(send_at, '%Y-%m-%d %H:%M:%S')
        pretty_time = dt_obj.strftime('%d.%m.%Y в %H:%M')
        
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отменить", callback_data=f"cancel_{db_id}")]
        ])
        await message.answer(f"📌 Напоминание на: {pretty_time}", reply_markup=kb)

# НОВАЯ ФУНКЦИЯ: Обработка кнопки "Отменить"
@dp.callback_query(F.data.startswith("cancel_"))
async def handle_cancel(callback: CallbackQuery):
    db_id = int(callback.data.split("_")[1])
    delete_message(db_id)
    
    try:
        await callback.message.edit_text("🚫 Напоминание отменено.")
    except TelegramAPIError:
        pass
    await callback.answer("Удалено!")

@dp.message(ScheduleState.waiting_for_datetime)
async def process_custom_datetime(message: types.Message, state: FSMContext):
    try:
        # Привязываем введенную дату к нашему часовому поясу
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
        await message.answer("❌ Неверный формат. Напиши точно как в примере: ДД.ММ.ГГГГ ЧЧ:ММ")

@dp.message()
async def catch_message(message: types.Message, state: FSMContext):
    await state.clear() 
    
    kb = [
        [
            InlineKeyboardButton(text="🌅 Утро", callback_data="time_morning"),
            InlineKeyboardButton(text="☀️ День", callback_data="time_day"),
            InlineKeyboardButton(text="🌙 Вечер", callback_data="time_evening")
        ],
        [
            InlineKeyboardButton(text="⏱ Через минуту (тест)", callback_data="time_now")
        ],
        [
            InlineKeyboardButton(text="📅 Точная дата и время", callback_data="time_custom")
        ]
    ]
    keyboard = InlineKeyboardMarkup(inline_keyboard=kb)
    await message.reply("Когда мне напомнить об этом сообщении?", reply_markup=keyboard)


@dp.callback_query(F.data.startswith("time_"))
async def handle_time_selection(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    
    # Все расчеты теперь строго в нашем часовом поясе
    now = datetime.now(TZ)
    scheduled_time = None
    label = ""

    if callback.data == "time_custom":
        # ЗАЩИТА №1: Проверяем, существует ли пересланное сообщение
        if callback.message.reply_to_message is None:
            await callback.message.edit_text("❌ Ошибка: не могу найти оригинальное сообщение. Попробуй переслать его заново.")
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
            await callback.message.edit_text("❌ Ошибка: не могу найти оригинальное сообщение. Попробуй переслать его заново.")
            return

        chat_id = callback.message.chat.id
        msg_id = callback.message.reply_to_message.message_id
        
        try:
            await callback.message.edit_text(f"✅ Принято! Отправлю это сообщение тебе {label}.")
            add_message(user_id=chat_id, message_id=msg_id, send_at=scheduled_time.strftime('%Y-%m-%d %H:%M:%S'))
        except TelegramAPIError:
            pass

async def check_messages():
    # TODO: В будущем бесконечный цикл `while True` с `sleep()` лучше заменить на 
    # профессиональный планировщик задач, например APScheduler или Celery.
    while True:
        # Передаем точное время в нашем поясе
        now_str = datetime.now(TZ).strftime('%Y-%m-%d %H:%M:%S')
        pending = get_pending_messages(now_str)
        
        for msg in pending:
            db_id, chat_id, message_id = msg
            try:
                chat_id = int(chat_id)
                await bot.forward_message(chat_id=chat_id, from_chat_id=chat_id, message_id=message_id)
                mark_as_sent(db_id)
                
                # TODO: Если бот будет массово рассылать сотни сообщений в одну секунду, 
                # Telegram выдаст бан за спам. Сюда стоит добавить `await asyncio.sleep(0.05)`, 
                # чтобы искусственно тормозить отправку.
                
            except Exception as e:
                print(f"❌ Ошибка при отправке сообщения {message_id}: {e}")
                mark_as_sent(db_id)
        
        await asyncio.sleep(30)

async def main():
    init_db()
    await bot.delete_webhook(drop_pending_updates=True)
    asyncio.create_task(check_messages())
    print("Бот успешно запущен и ждет сообщений...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())