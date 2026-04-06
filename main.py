import asyncio
import calendar
import html
import logging
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import aiohttp
from aiogram import Bot, Dispatcher, F, types
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramNotFound,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions
from dotenv import load_dotenv
from aiogram.types import FSInputFile

from data.database import (
    add_message,
    add_saved_message,
    add_subscription,
    cleanup_old_records,
    delete_message,
    delete_saved_message,
    delete_subscription,
    get_due_subscriptions,
    get_pending_messages,
    get_saved_message_by_id,
    get_scheduled_message_by_delivered_message_id,
    get_saved_messages,
    get_user_messages,
    get_user_subscriptions,
    get_user_tags,
    init_db,
    mark_as_sent,
    mark_message_delivery_error,
    mark_subscription_delivery_error,
    normalize_channel_username,
    parse_db_datetime,
    serialize_datetime,
    update_subscription_time,
    utc_now,
)
from scraper import ChannelFetchError, REQUEST_TIMEOUT, get_latest_posts

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise ValueError("❌ Токен бота не найден! Проверьте файл .env")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
TZ = ZoneInfo(os.getenv("APP_TIMEZONE", "Europe/Moscow"))
MAX_MESSAGE_RETRIES = 5
MAX_DIGEST_RETRIES = 5
DIGEST_FETCH_CONCURRENCY = 5
CLEANUP_INTERVAL_SECONDS = 6 * 60 * 60


class ScheduleState(StatesGroup):
    waiting_for_datetime = State()


class SaveState(StatesGroup):
    waiting_for_tag = State()


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


def local_now() -> datetime:
    return datetime.now(TZ)


def display_db_datetime(value: str) -> datetime:
    return parse_db_datetime(value).astimezone(TZ)


def get_next_digest_time(period: str, now: datetime | None = None) -> datetime:
    now = now or local_now()
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


def get_message_full_text(msg: types.Message) -> str:
    return msg.text or msg.caption or "🖼 Медиафайл (без текста)"


def get_replied_sent_schedule(message: types.Message):
    replied = message.reply_to_message
    if not replied:
        return None
    return get_scheduled_message_by_delivered_message_id(message.chat.id, replied.message_id)


def build_time_selection_keyboard(prefix: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🌅 Утро", callback_data=f"{prefix}_morning"),
                InlineKeyboardButton(text="☀️ День", callback_data=f"{prefix}_day"),
                InlineKeyboardButton(text="🌙 Вечер", callback_data=f"{prefix}_evening"),
            ],
            [
                InlineKeyboardButton(text="⏱ На 3 часа", callback_data=f"{prefix}_now"),
                InlineKeyboardButton(text="✍️ Вручную", callback_data=f"{prefix}_custom"),
            ],
        ]
    )


def get_quick_scheduled_time(action: str, now: datetime) -> tuple[datetime | None, str]:
    if action == "morning":
        return (now + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0), "завтра на 09:00"

    if action == "day":
        scheduled_time = now.replace(hour=14, minute=0, second=0, microsecond=0)
        if now.hour >= 14:
            scheduled_time += timedelta(days=1)
        return scheduled_time, "на 14:00"

    if action == "evening":
        scheduled_time = now.replace(hour=20, minute=0, second=0, microsecond=0)
        if now.hour >= 20:
            scheduled_time += timedelta(days=1)
        return scheduled_time, "на 20:00"

    if action == "now":
        return now + timedelta(hours=3), "через 3 часа"

    return None, ""


def get_suggested_manual_time(now: datetime) -> datetime:
    if now.hour < 8:
        return now.replace(hour=9, minute=0, second=0, microsecond=0)
    if now.hour < 13:
        return now.replace(hour=14, minute=0, second=0, microsecond=0)
    if now.hour < 20:
        return now.replace(hour=20, minute=0, second=0, microsecond=0)
    return (now + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)


def classify_telegram_send_error(error: TelegramAPIError) -> tuple[bool, str]:
    if isinstance(error, TelegramRetryAfter):
        return False, f"Retry after {error.retry_after} seconds"
    if isinstance(error, (TelegramNetworkError, TelegramServerError)):
        return False, str(error)
    if isinstance(error, (TelegramForbiddenError, TelegramNotFound)):
        return True, str(error)
    if isinstance(error, TelegramBadRequest):
        message = str(error)
        temporary_markers = ("timeout", "retry", "flood")
        return (not any(marker in message.lower() for marker in temporary_markers), message)
    return False, str(error)


async def send_digest_chunks(user_id: int, digest_lines: list[str]):
    for chunk in chunk_html_text(digest_lines):
        await bot.send_message(
            user_id,
            chunk,
            parse_mode="HTML",
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )


async def fetch_subscription_posts(
    sub: tuple,
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore,
    now_str: str,
):
    sub_id, _, username, title, period, last_scraped, failure_count = sub
    title_safe = html.escape(title) if title else "Канал"
    next_send_str = serialize_datetime(get_next_digest_time(period, local_now()))

    async with semaphore:
        try:
            posts = await get_latest_posts(username, last_scraped, session=session)
            return {
                "status": "ok",
                "sub_id": sub_id,
                "username": username,
                "title_safe": title_safe,
                "posts": posts,
                "next_send_str": next_send_str,
            }
        except ChannelFetchError as exc:
            new_failure_count = failure_count + 1
            is_permanent = exc.permanent or new_failure_count >= MAX_DIGEST_RETRIES
            mark_subscription_delivery_error(
                sub_id,
                str(exc),
                now_str,
                new_failure_count,
                is_permanent,
            )
            log_level = logging.ERROR if is_permanent else logging.WARNING
            logger.log(
                log_level,
                "Не удалось получить канал %s для подписки %s. Статус: %s. Ошибка: %s",
                username,
                sub_id,
                "permanent" if is_permanent else "temporary",
                exc,
            )
            return {
                "status": "error",
                "sub_id": sub_id,
                "is_permanent": is_permanent,
            }


@dp.message(CommandStart())
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Привет! Я готов.\n\n"
        "1️⃣ Перешли мне любое сообщение, чтобы отложить его или сохранить в базу знаний.\n"
        "2️⃣ Перешли пост из открытого канала, чтобы подписаться на его дайджест.\n"
        "3️⃣ Напиши /list для задач или /saved для Избранного.\n"
        "4️⃣ Для уже доставленного напоминания ответь на него командой: /morning, /day, /evening, /later, /at ДД.ММ.ГГГГ ЧЧ:ММ, /save, /delete."
    )


@dp.message(Command("morning"))
async def cmd_morning(message: types.Message):
    scheduled_msg = get_replied_sent_schedule(message)
    if not scheduled_msg:
        await message.answer("Ответь этой командой на доставленное напоминание от бота.")
        return

    db_id, source_message_id, _, _, preview, source, _ = scheduled_msg
    scheduled_time, label = get_quick_scheduled_time("morning", local_now())
    if scheduled_time is None:
        await message.answer("❌ Ошибка при планировании времени.")
        return
    add_message(message.chat.id, source_message_id, serialize_datetime(scheduled_time), preview or "", source or "")
    delete_message(message.chat.id, db_id)
    try:
        if message.reply_to_message is not None:
            await message.reply_to_message.delete()
        await message.delete()
    except TelegramAPIError:
        pass
    await message.answer(f"✅ Отложил {label}.")


@dp.message(Command("day"))
async def cmd_day(message: types.Message):
    scheduled_msg = get_replied_sent_schedule(message)
    if not scheduled_msg:
        await message.answer("Ответь этой командой на доставленное напоминание от бота.")
        return

    db_id, source_message_id, _, _, preview, source, _ = scheduled_msg
    scheduled_time, label = get_quick_scheduled_time("day", local_now())
    if scheduled_time is None:
        await message.answer("❌ Ошибка при планировании времени.")
        return
    add_message(message.chat.id, source_message_id, serialize_datetime(scheduled_time), preview or "", source or "")
    delete_message(message.chat.id, db_id)
    try:
        if message.reply_to_message is not None:
            await message.reply_to_message.delete()
        await message.delete()
    except TelegramAPIError:
        pass
    await message.answer(f"✅ Отложил {label}.")


@dp.message(Command("evening"))
async def cmd_evening(message: types.Message):
    scheduled_msg = get_replied_sent_schedule(message)
    if not scheduled_msg:
        await message.answer("Ответь этой командой на доставленное напоминание от бота.")
        return

    db_id, source_message_id, _, _, preview, source, _ = scheduled_msg
    scheduled_time, label = get_quick_scheduled_time("evening", local_now())
    if scheduled_time is None:
        await message.answer("❌ Ошибка при планировании времени.")
        return
    add_message(message.chat.id, source_message_id, serialize_datetime(scheduled_time), preview or "", source or "")
    delete_message(message.chat.id, db_id)
    try:
        if message.reply_to_message is not None:
            await message.reply_to_message.delete()
        await message.delete()
    except TelegramAPIError:
        pass
    await message.answer(f"✅ Отложил {label}.")


@dp.message(Command("at"))
async def cmd_at(message: types.Message):
    scheduled_msg = get_replied_sent_schedule(message)
    if not scheduled_msg:
        await message.answer("Ответь этой командой на доставленное напоминание от бота.")
        return

    command_text = (message.text or "").strip()
    parts = command_text.split(maxsplit=1)
    arg = parts[1].strip() if len(parts) > 1 else ""
    if not arg:
        await message.answer("Формат: /at ДД.ММ.ГГГГ ЧЧ:ММ")
        return

    try:
        scheduled_time = datetime.strptime(arg, "%d.%m.%Y %H:%M").replace(tzinfo=TZ)
    except ValueError:
        await message.answer("❌ Неверный формат. Пример: /at 06.04.2026 18:30")
        return

    if scheduled_time < local_now():
        await message.answer("❌ Эта дата уже в прошлом.")
        return

    db_id, source_message_id, _, _, preview, source, _ = scheduled_msg
    add_message(message.chat.id, source_message_id, serialize_datetime(scheduled_time), preview or "", source or "")
    delete_message(message.chat.id, db_id)
    try:
        if message.reply_to_message is not None:
            await message.reply_to_message.delete()
        await message.delete()
    except TelegramAPIError:
        pass
    await message.answer(f"✅ Отложил до {scheduled_time.strftime('%d.%m.%Y %H:%M')}.")

@dp.message(Command("export"))
async def cmd_export(message: types.Message):
    # Бот сам возьмет правильный путь из переменных Railway
    db_path = os.getenv("DB_PATH", "data/bot_data.db")
    try:
        document = FSInputFile(db_path)
        await message.answer_document(document, caption="📦 Моя база данных с Railway")
    except Exception as e:
        await message.answer(f"Ошибка выгрузки: {e}")