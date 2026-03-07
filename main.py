import asyncio
import os
from datetime import datetime, timedelta
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

# ЕДИНЫЙ ИМПОРТ ИЗ БАЗЫ:
from database import add_message, get_pending_messages, mark_as_sent, init_db

# Загрузка переменных окружения
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise ValueError("❌ Токен бота не найден! Проверьте файл .env")

# Инициализация бота и диспетчера
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Класс состояний (FSM) для ожидания текстового ввода даты
class ScheduleState(StatesGroup):
    waiting_for_datetime = State()

# 1. ОБРАБОТЧИК: Команда /start
@dp.message(CommandStart())
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear() # На всякий случай очищаем память состояний
    await message.answer("Привет! Я готов. Перешли мне любое сообщение, и я предложу время для отложенной отправки.")

# 2. ОБРАБОТЧИК: Ловец точной даты (СПЕЦИФИЧНЫЙ ФИЛЬТР - СТРОГО ВЫШЕ ОБЩЕГО)
@dp.message(ScheduleState.waiting_for_datetime)
async def process_custom_datetime(message: types.Message, state: FSMContext):
    try:
        # Пытаемся расшифровать текст пользователя в дату
        scheduled_time = datetime.strptime(message.text, "%d.%m.%Y %H:%M")
        
        # Проверяем, не ввел ли он дату из прошлого
        if scheduled_time < datetime.now():
            await message.answer("Эта дата уже в прошлом! Попробуй еще раз (ДД.ММ.ГГГГ ЧЧ:ММ):")
            return
            
        # Достаем ID сообщения из временной памяти
        data = await state.get_data()
        msg_id = data.get('message_id')
        chat_id = message.chat.id
        
        # Защита: если ID сообщения потерялся
        if not msg_id:
            await message.answer("Ошибка: не найден ID сообщения. Попробуй переслать его заново.")
            await state.clear()
            return
            
        # Сохраняем в базу
        add_message(user_id=chat_id, message_id=msg_id, send_at=scheduled_time.strftime('%Y-%m-%d %H:%M:%S'))
        
        await message.answer(f"✅ Принято! Запланировано на {scheduled_time.strftime('%d.%m.%Y в %H:%M')}.")
        
        # Очищаем состояние (выходим из режима ожидания)
        await state.clear()
        
    except ValueError:
        await message.answer("❌ Неверный формат. Напиши точно как в примере: ДД.ММ.ГГГГ ЧЧ:ММ (например, 15.03.2026 14:30)")

# 3. ОБРАБОТЧИК: Общий перехватчик сообщений (БЕЗ ФИЛЬТРОВ - СТРОГО ВНИЗУ)
@dp.message()
async def catch_message(message: types.Message, state: FSMContext):
    # Если пользователь прислал новое сообщение, отменяем ожидание даты для старого
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


# 4. ОБРАБОТЧИК: Нажатия на кнопки (Callback)
@dp.callback_query(F.data.startswith("time_"))
async def handle_time_selection(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    
    now = datetime.now()
    scheduled_time = None
    label = ""

    # ЛОГИКА ДЛЯ КНОПКИ ТОЧНОГО ВРЕМЕНИ
    if callback.data == "time_custom":
        msg_id = callback.message.reply_to_message.message_id
        # Сохраняем ID сообщения во временную память машины состояний
        await state.update_data(message_id=msg_id)
        # Включаем состояние ожидания
        await state.set_state(ScheduleState.waiting_for_datetime)
        
        await callback.message.edit_text(
            "Напиши точную дату и время в формате ДД.ММ.ГГГГ ЧЧ:ММ\n"
            "Например: `15.03.2026 14:30`", parse_mode="Markdown"
        )
        return # Выходим из функции, дальше бот будет ждать текстовое сообщение

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
        chat_id = callback.message.chat.id
        msg_id = callback.message.reply_to_message.message_id
        add_message(user_id=chat_id, message_id=msg_id, send_at=scheduled_time.strftime('%Y-%m-%d %H:%M:%S'))
        await callback.message.edit_text(f"✅ Принято! Отправлю это сообщение тебе {label}.")

# 5. ФОНОВАЯ ЗАДАЧА: Проверка базы данных
async def check_messages():
    while True:
        pending = get_pending_messages()
        for msg in pending:
            db_id, chat_id, message_id = msg
            try:
                chat_id = int(chat_id)
                print(f"⏳ Пробую отправить: чат={chat_id}, сообщение={message_id}")
                
                await bot.forward_message(chat_id=chat_id, from_chat_id=chat_id, message_id=message_id)
                mark_as_sent(db_id)
                print(f"✅ Сообщение {message_id} успешно отправлено!")
                
            except Exception as e:
                print(f"❌ Ошибка при отправке сообщения {message_id}: {e}")
                mark_as_sent(db_id)
        
        # Проверяем базу каждые 30 секунд
        await asyncio.sleep(30)

# 6. ГЛАВНАЯ ФУНКЦИЯ ЗАПУСКА
async def main():
    # 1. Создаем таблицы в базе данных (если их еще нет на новом диске)
    init_db()

    # 2. Удаляем старые вебхуки Telegram, чтобы избежать конфликта getUpdates
    await bot.delete_webhook(drop_pending_updates=True)

    # 3. Запускаем фоновый "будильник"
    asyncio.create_task(check_messages())

    print("Бот успешно запущен и ждет сообщений...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())