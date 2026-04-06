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
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, InaccessibleMessage, Message
from dotenv import load_dotenv

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
    replace_sent_reminder_with_pending,
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

USER_FACING_ERROR = "Что-то пошло не так. Попробуй ещё раз позже."

_QUICK_RESCHEDULE_ACTION: dict[str, str] = {
    "morning": "morning",
    "day": "day",
    "evening": "evening",
    "later": "now",
}


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


def get_callback_message(callback: CallbackQuery) -> Message | None:
    message = callback.message
    if message is None or isinstance(message, InaccessibleMessage):
        return None
    return message


def parse_callback_int_suffix(data: str | None, prefix: str) -> int | None:
    if not data or not data.startswith(prefix):
        return None
    rest = data[len(prefix) :]
    try:
        return int(rest)
    except ValueError:
        return None


def parse_callback_strip_prefix(data: str | None, prefix: str) -> str | None:
    if not data or not data.startswith(prefix):
        return None
    rest = data[len(prefix) :]
    return rest if rest else None


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


async def _reschedule_replied_reminder(
    message: types.Message,
    quick_action: str,
    success_answer: str,
) -> None:
    scheduled_msg = get_replied_sent_schedule(message)
    if not scheduled_msg:
        await message.answer("Ответь этой командой на доставленное напоминание от бота.")
        return

    db_id, source_message_id, _, _, preview, source, _ = scheduled_msg
    scheduled_time, label = get_quick_scheduled_time(quick_action, local_now())
    if scheduled_time is None:
        await message.answer("❌ Ошибка при планировании времени.")
        return
    try:
        replace_sent_reminder_with_pending(
            message.chat.id,
            db_id,
            source_message_id,
            serialize_datetime(scheduled_time),
            preview or "",
            source or "",
        )
    except Exception:
        logger.exception("replace_sent_reminder_with_pending failed")
        await message.answer(USER_FACING_ERROR)
        return
    try:
        if message.reply_to_message is not None:
            await message.reply_to_message.delete()
        await message.delete()
    except TelegramAPIError:
        pass
    await message.answer(success_answer.format(label=label))


@dp.message(Command("morning", "day", "evening", "later"))
async def cmd_quick_reschedule(message: types.Message, command: CommandObject):
    cmd = (command.command or "").lower()
    action = _QUICK_RESCHEDULE_ACTION.get(cmd)
    if action is None:
        return
    await _reschedule_replied_reminder(
        message,
        action,
        "✅ Отложил {label}.",
    )


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
    try:
        replace_sent_reminder_with_pending(
            message.chat.id,
            db_id,
            source_message_id,
            serialize_datetime(scheduled_time),
            preview or "",
            source or "",
        )
    except Exception:
        logger.exception("replace_sent_reminder_with_pending failed in cmd_at")
        await message.answer(USER_FACING_ERROR)
        return
    try:
        if message.reply_to_message is not None:
            await message.reply_to_message.delete()
        await message.delete()
    except TelegramAPIError:
        pass
    await message.answer(f"✅ Отложил до {scheduled_time.strftime('%d.%m.%Y %H:%M')}.")


@dp.message(Command("save"))
async def cmd_save_reply(message: types.Message, state: FSMContext):
    scheduled_msg = get_replied_sent_schedule(message)
    if not scheduled_msg:
        await message.answer("Ответь этой командой на доставленное напоминание от бота.")
        return

    db_id, _, delivered_message_id, _, _, source, _ = scheduled_msg
    if message.reply_to_message is None:
        await message.answer("Ответь этой командой на доставленное напоминание от бота.")
        return
    full_text = get_message_full_text(message.reply_to_message)
    tags = get_user_tags(message.chat.id)
    tags_text = ""
    if tags:
        tags_formatted = "  ".join([f"`{tag}`" for tag in tags])
        tags_text = f"\n\n📝 *Твои прошлые теги* (нажми, чтобы скопировать):\n{tags_formatted}"

    prompt = await message.answer(
        f"Напиши тег для этого сообщения (например: Идеи, Статьи, Важное).{tags_text}",
        parse_mode="Markdown",
    )
    await state.update_data(
        full_text=full_text,
        source=source or "Неизвестно",
        prompt_msg_id=prompt.message_id,
        target_message_id=delivered_message_id,
        scheduled_db_id=db_id,
        command_message_id=message.message_id,
    )
    await state.set_state(SaveState.waiting_for_tag)


@dp.message(Command("delete"))
async def cmd_delete_reply(message: types.Message):
    scheduled_msg = get_replied_sent_schedule(message)
    if not scheduled_msg:
        await message.answer("Ответь этой командой на доставленное напоминание от бота.")
        return

    db_id = scheduled_msg[0]
    delete_message(message.chat.id, db_id)
    try:
        if message.reply_to_message is not None:
            await message.reply_to_message.delete()
        await message.delete()
    except TelegramAPIError:
        pass


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
            dt_obj = display_db_datetime(send_at)
            source_safe = html.escape(source or "Неизвестно")
            preview_safe = html.escape(preview or "Без текста")

            text_lines.append(
                f"{idx}. 📌 <b>{dt_obj.strftime('%d.%m в %H:%M')}</b> | От: {source_safe}\n"
                f"<i>{preview_safe}</i>\n"
            )
            buttons.append(InlineKeyboardButton(text=f"❌ {idx}", callback_data=f"cancel_{db_id}"))

        kb_rows = [buttons[i:i + 5] for i in range(0, len(buttons), 5)]
        await message.answer(
            "\n".join(text_lines),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
        )

    if user_subs:
        text_lines = ["📡 <b>Твои подписки на дайджесты:</b>\n"]
        buttons = []
        period_ru = {"daily": "каждый день", "weekly": "раз в неделю", "monthly": "раз в месяц"}
        for idx, sub in enumerate(user_subs, 1):
            sub_id, _, title, period, next_send_at = sub
            dt_obj = display_db_datetime(next_send_at)

            text_lines.append(
                f"{idx}. 📰 <b>{html.escape(title or 'Канал')}</b> ({period_ru.get(period, period)})\n"
                f"След: {dt_obj.strftime('%d.%m в %H:%M')}\n"
            )
            buttons.append(InlineKeyboardButton(text=f"❌ {idx}", callback_data=f"unsub_{sub_id}"))

        kb_rows = [buttons[i:i + 5] for i in range(0, len(buttons), 5)]
        await message.answer(
            "\n".join(text_lines),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
        )


@dp.callback_query(F.data.startswith("cancel_"))
async def handle_cancel(callback: CallbackQuery):
    callback_message = get_callback_message(callback)
    if callback_message is None:
        await callback.answer("❌ Сообщение недоступно.")
        return

    db_id = parse_callback_int_suffix(callback.data, "cancel_")
    if db_id is None:
        await callback.answer("❌ Некорректные данные.")
        return

    delete_message(callback_message.chat.id, db_id)

    user_msgs = get_user_messages(callback_message.chat.id)
    if not user_msgs:
        await callback_message.edit_text("📭 Все разовые напоминания отменены.")
        return

    text_lines = ["⏳ <b>Твои разовые напоминания:</b>\n"]
    buttons = []
    for idx, msg in enumerate(user_msgs, 1):
        item_id, send_at, preview, source = msg
        dt_obj = display_db_datetime(send_at)
        text_lines.append(
            f"{idx}. 📌 <b>{dt_obj.strftime('%d.%m в %H:%M')}</b> | От: {html.escape(source or 'Неизвестно')}\n"
            f"<i>{html.escape(preview or 'Без текста')}</i>\n"
        )
        buttons.append(InlineKeyboardButton(text=f"❌ {idx}", callback_data=f"cancel_{item_id}"))

    kb_rows = [buttons[i:i + 5] for i in range(0, len(buttons), 5)]
    try:
        await callback_message.edit_text(
            "\n".join(text_lines),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
        )
    except TelegramAPIError:
        pass
    await callback.answer("Удалено!")


@dp.callback_query(F.data.startswith("unsub_"))
async def handle_unsub(callback: CallbackQuery):
    callback_message = get_callback_message(callback)
    if callback_message is None:
        await callback.answer("❌ Сообщение недоступно.")
        return

    sub_id = parse_callback_int_suffix(callback.data, "unsub_")
    if sub_id is None:
        await callback.answer("❌ Некорректные данные.")
        return

    delete_subscription(callback_message.chat.id, sub_id)

    user_subs = get_user_subscriptions(callback_message.chat.id)
    if not user_subs:
        await callback_message.edit_text("📭 Все подписки на дайджесты отменены.")
        return

    text_lines = ["📡 <b>Твои подписки на дайджесты:</b>\n"]
    buttons = []
    period_ru = {"daily": "каждый день", "weekly": "раз в неделю", "monthly": "раз в месяц"}
    for idx, sub in enumerate(user_subs, 1):
        item_id, _, title, period, next_send_at = sub
        dt_obj = display_db_datetime(next_send_at)
        text_lines.append(
            f"{idx}. 📰 <b>{html.escape(title or 'Канал')}</b> ({period_ru.get(period, period)})\n"
            f"След: {dt_obj.strftime('%d.%m в %H:%M')}\n"
        )
        buttons.append(InlineKeyboardButton(text=f"❌ {idx}", callback_data=f"unsub_{item_id}"))

    kb_rows = [buttons[i:i + 5] for i in range(0, len(buttons), 5)]
    try:
        await callback_message.edit_text(
            "\n".join(text_lines),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
        )
    except TelegramAPIError:
        pass
    await callback.answer("Удалено!")


@dp.message(Command("saved"))
async def cmd_saved(message: types.Message, state: FSMContext):
    await state.clear()
    user_id = message.chat.id
    saved_msgs = get_saved_messages(user_id)

    if not saved_msgs:
        await message.answer("📭 Твоя база знаний пока пуста.")
        return

    grouped = {}
    for msg in saved_msgs:
        tag = msg[1]
        grouped.setdefault(tag, []).append(msg)

    text_lines = ["📁 <b>Твоя база знаний:</b>\n"]
    buttons = []

    idx = 1
    for tag, msgs in grouped.items():
        text_lines.append(f"🏷 <b>{html.escape(tag)}</b>")
        for item in msgs:
            db_id, _, full_text, source, _ = item
            preview = full_text.replace('\n', ' ')[:40] + "..." if len(full_text) > 40 else full_text.replace('\n', ' ')
            source_safe = html.escape(source or "Неизвестно")
            preview_safe = html.escape(preview or "Без текста")

            text_lines.append(f"{idx}. От: {source_safe} | <i>{preview_safe}</i>")
            buttons.append(
                [
                    InlineKeyboardButton(text=f"📖 Читать {idx}", callback_data=f"read_saved_{db_id}"),
                    InlineKeyboardButton(text=f"❌ {idx}", callback_data=f"del_saved_{db_id}"),
                ]
            )
            idx += 1
        text_lines.append("")

    await message.answer(
        "\n".join(text_lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@dp.callback_query(F.data.startswith("read_saved_"))
async def read_saved(callback: CallbackQuery):
    callback_message = get_callback_message(callback)
    if callback_message is None:
        await callback.answer("❌ Сообщение недоступно.")
        return

    db_id = parse_callback_int_suffix(callback.data, "read_saved_")
    if db_id is None:
        await callback.answer("❌ Некорректные данные.")
        return

    msg_data = get_saved_message_by_id(callback_message.chat.id, db_id)
    if not msg_data:
        await callback.answer("❌ Сообщение не найдено", show_alert=True)
        return

    full_text, source, tag, saved_at = msg_data
    dt_obj = display_db_datetime(saved_at)
    text = (
        f"🏷 <b>Тег:</b> {html.escape(tag or 'Без тега')}\n"
        f"👤 <b>Источник:</b> {html.escape(source or 'Неизвестно')}\n"
        f"📅 <b>Сохранено:</b> {dt_obj.strftime('%d.%m.%Y в %H:%M')}\n\n"
        f"📝 <b>Текст:</b>\n{html.escape(full_text or 'Без текста')}"
    )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🔙 Закрыть", callback_data="close_msg")]]
    )

    chunks = chunk_html_text(text.split('\n'))
    for index, chunk in enumerate(chunks):
        if index == len(chunks) - 1:
            await callback_message.answer(chunk, parse_mode="HTML", reply_markup=kb)
        else:
            await callback_message.answer(chunk, parse_mode="HTML")
    await callback.answer()


@dp.callback_query(F.data == "close_msg")
async def close_msg(callback: CallbackQuery):
    callback_message = get_callback_message(callback)
    if callback_message is None:
        await callback.answer("❌ Сообщение недоступно.")
        return

    try:
        await callback_message.delete()
    except TelegramAPIError:
        pass
    await callback.answer()


@dp.callback_query(F.data.startswith("del_saved_"))
async def handle_del_saved(callback: CallbackQuery, state: FSMContext):
    callback_message = get_callback_message(callback)
    if callback_message is None:
        await callback.answer("❌ Сообщение недоступно.")
        return

    db_id = parse_callback_int_suffix(callback.data, "del_saved_")
    if db_id is None:
        await callback.answer("❌ Некорректные данные.")
        return

    delete_saved_message(callback_message.chat.id, db_id)
    await callback.answer("✅ Закладка удалена!")
    await cmd_saved(callback_message, state)


@dp.message(ScheduleState.waiting_for_datetime)
async def process_custom_datetime(message: types.Message, state: FSMContext):
    text = (message.text or "").strip()
    if not text:
        await message.answer("❌ Неверный формат. Пример: 15.03.2026 14:30")
        return

    try:
        scheduled_time = datetime.strptime(text, "%d.%m.%Y %H:%M").replace(tzinfo=TZ)
        if scheduled_time < local_now():
            await message.answer("Эта дата уже в прошлом! Попробуй еще раз (ДД.ММ.ГГГГ ЧЧ:ММ):")
            return

        data = await state.get_data()
        msg_id = data.get("message_id")
        preview = data.get("preview", "")
        source = data.get("source", "")
        prompt_msg_id = data.get("prompt_msg_id")

        if not msg_id:
            await message.answer("Ошибка: не найден ID сообщения. Попробуй переслать его заново.")
            await state.clear()
            return

        add_message(message.chat.id, msg_id, serialize_datetime(scheduled_time), preview, source)

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


@dp.message(SaveState.waiting_for_tag)
async def process_tag(message: types.Message, state: FSMContext):
    tag = (message.text or "").strip()
    if not tag:
        await message.answer("❌ Тег не должен быть пустым. Попробуй ещё раз.")
        return

    data = await state.get_data()
    orig_msg_id = data.get("orig_msg_id")
    full_text = data.get("full_text")
    source = data.get("source")
    prompt_msg_id = data.get("prompt_msg_id")
    target_message_id = data.get("target_message_id")
    scheduled_db_id = data.get("scheduled_db_id")
    command_message_id = data.get("command_message_id")

    saved_at = serialize_datetime(utc_now())
    add_saved_message(message.chat.id, full_text, source, tag, saved_at)

    try:
        await message.delete()
        if prompt_msg_id:
            await bot.delete_message(message.chat.id, prompt_msg_id)
        if orig_msg_id:
            await bot.delete_message(message.chat.id, orig_msg_id)
        if target_message_id:
            await bot.delete_message(message.chat.id, target_message_id)
        if command_message_id:
            await bot.delete_message(message.chat.id, command_message_id)
    except TelegramAPIError:
        pass

    if scheduled_db_id:
        delete_message(message.chat.id, scheduled_db_id)

    confirm_msg = await message.answer(
        f"✅ Сохранено в базу знаний под тегом <b>{html.escape(tag)}</b>",
        parse_mode="HTML",
    )
    await state.clear()

    await asyncio.sleep(3)
    try:
        await confirm_msg.delete()
    except TelegramAPIError:
        pass


@dp.message(Command("test_digest"))
async def cmd_test_digest(message: types.Message, state: FSMContext):
    await state.clear()
    user_id = message.chat.id
    user_subs = get_user_subscriptions(user_id)

    if not user_subs:
        await message.answer("📭 У тебя нет активных подписок для парсинга.")
        return

    await message.answer("⏳ Собираю единый дайджест за последние 24 часа...")
    now_tz = local_now()
    last_24h_str = serialize_datetime(now_tz - timedelta(days=1))
    digest_lines = ["📰 <b>Твоя тестовая утренняя газета</b> ☕️\n\n"]
    has_news = False
    semaphore = asyncio.Semaphore(DIGEST_FETCH_CONCURRENCY)

    async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
        async def load_posts(subscription) -> tuple[str | None, str, list[dict]]:
            _, username, title, _, _ = subscription
            async with semaphore:
                return title, username, await get_latest_posts(username, last_24h_str, session=session)

        results: list[tuple[str | None, str, list[dict]] | BaseException] = await asyncio.gather(
            *(load_posts(sub) for sub in user_subs),
            return_exceptions=True,
        )

    for result in results:
        if isinstance(result, BaseException):
            logger.error(
                "test_digest channel fetch failed",
                exc_info=(type(result), result, result.__traceback__),
            )
            has_news = True
            digest_lines.append("❌ Не удалось получить данные одного из каналов.\n")
            continue

        title, _, posts = result
        title_safe = html.escape(title) if title else "Без названия"
        if posts:
            has_news = True
            digest_lines.append(f"📌 <b>{title_safe}</b>")
            for post in posts:
                text_safe = html.escape(post["text"])
                digest_lines.append(f"🔹 <i>{text_safe}</i> <a href='{post['link']}'>[Читать]</a>\n")
            digest_lines.append("")

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
        if getattr(message.forward_origin.chat, "username", None):
            is_public_channel = True
            channel_username = message.forward_origin.chat.username
            channel_title = message.forward_origin.chat.title

    kb = build_time_selection_keyboard("time").inline_keyboard
    kb.append([InlineKeyboardButton(text="📁 В закладки (База знаний)", callback_data="bookmark_setup")])

    if is_public_channel:
        kb.append([InlineKeyboardButton(text="📡 Собирать дайджест", callback_data="digest_setup")])
        await state.update_data(channel_username=channel_username, channel_title=channel_title)

    await message.reply("Что мне сделать с этим сообщением?", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))


@dp.callback_query(F.data == "digest_setup")
async def setup_digest(callback: CallbackQuery, state: FSMContext):
    callback_message = get_callback_message(callback)
    if callback_message is None:
        await callback.answer("❌ Сообщение недоступно.")
        return

    kb = [
        [InlineKeyboardButton(text="📅 Раз в день", callback_data="period_daily")],
        [InlineKeyboardButton(text="🗓 Раз в неделю", callback_data="period_weekly")],
        [InlineKeyboardButton(text="📊 Раз в месяц", callback_data="period_monthly")],
    ]
    await callback_message.edit_text(
        "Как часто присылать новые посты из этого канала?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),
    )


@dp.callback_query(F.data.startswith("period_"))
async def save_subscription(callback: CallbackQuery, state: FSMContext):
    callback_message = get_callback_message(callback)
    if callback_message is None:
        await callback.answer("❌ Сообщение недоступно.")
        return

    period = parse_callback_strip_prefix(callback.data, "period_")
    if period is None:
        await callback.answer("❌ Некорректные данные.")
        return
    data = await state.get_data()
    username = data.get("channel_username")
    title = data.get("channel_title")

    if not username:
        await callback_message.edit_text("❌ Ошибка: данные канала утеряны.")
        return

    username = normalize_channel_username(username)

    now = local_now()
    next_send = get_next_digest_time(period, now)
    last_scraped = serialize_datetime(utc_now())
    add_subscription(
        callback_message.chat.id,
        username,
        title,
        period,
        last_scraped,
        serialize_datetime(next_send),
    )

    period_ru = {"daily": "каждый день", "weekly": "раз в неделю", "monthly": "раз в месяц"}
    await callback.answer(
        f"✅ Дайджест {title} оформлен!\nБудет приходить {period_ru[period]} в 07:00.",
        show_alert=True,
    )

    try:
        if callback_message.reply_to_message:
            await bot.delete_message(callback_message.chat.id, callback_message.reply_to_message.message_id)
        await callback_message.delete()
    except TelegramAPIError:
        pass

    await state.clear()


@dp.callback_query(F.data == "bookmark_setup")
async def setup_bookmark(callback: CallbackQuery, state: FSMContext):
    callback_message = get_callback_message(callback)
    if callback_message is None:
        await callback.answer("❌ Сообщение недоступно.")
        return

    orig_msg = callback_message.reply_to_message
    if not orig_msg:
        await callback_message.edit_text("❌ Ошибка: не могу найти оригинальное сообщение.")
        return

    full_text = get_message_full_text(orig_msg)
    _, source = get_message_preview(orig_msg)

    await state.update_data(
        orig_msg_id=orig_msg.message_id,
        full_text=full_text,
        source=source,
        prompt_msg_id=callback_message.message_id,
    )
    await state.set_state(SaveState.waiting_for_tag)

    tags = get_user_tags(callback_message.chat.id)
    tags_text = ""
    if tags:
        tags_formatted = "  ".join([f"`{tag}`" for tag in tags])
        tags_text = f"\n\n📝 *Твои прошлые теги* (нажми, чтобы скопировать):\n{tags_formatted}"

    await callback_message.edit_text(
        f"Напиши тег для этого сообщения (например: Идеи, Статьи, Важное).{tags_text}",
        parse_mode="Markdown",
    )


@dp.callback_query(F.data.startswith("time_"))
async def handle_time_selection(callback: CallbackQuery, state: FSMContext):
    callback_message = get_callback_message(callback)
    if callback_message is None:
        await callback.answer("❌ Сообщение недоступно.")
        return

    now = local_now()
    if callback_message.reply_to_message is None:
        await callback_message.edit_text("❌ Ошибка: не могу найти оригинальное сообщение.")
        return

    orig_msg = callback_message.reply_to_message
    preview, source = get_message_preview(orig_msg)
    action = parse_callback_strip_prefix(callback.data, "time_")
    if action is None:
        await callback_message.edit_text("❌ Некорректные данные.")
        return

    if action == "custom":
        await state.update_data(
            message_id=orig_msg.message_id,
            preview=preview,
            source=source,
            prompt_msg_id=callback_message.message_id,
        )
        await state.set_state(ScheduleState.waiting_for_datetime)
        suggested_time = get_suggested_manual_time(now)
        suggested_str = suggested_time.strftime("%d.%m.%Y %H:%M")
        await callback_message.edit_text(
            "Напиши точную дату и время в формате ДД.ММ.ГГГГ ЧЧ:ММ\n\n"
            "💡 *Лайфхак:* нажми на время ниже, чтобы скопировать его:\n\n"
            f"`{suggested_str}`",
            parse_mode="Markdown",
        )
        return

    scheduled_time, label = get_quick_scheduled_time(action, now)

    if scheduled_time:
        try:
            await callback_message.edit_text(f"✅ Принято! Отправлю это сообщение тебе {label}.")
            add_message(
                callback_message.chat.id,
                orig_msg.message_id,
                serialize_datetime(scheduled_time),
                preview,
                source,
            )
            await asyncio.sleep(3)
            await callback_message.delete()
        except TelegramAPIError:
            pass


async def check_messages():
    while True:
        now_str = serialize_datetime(utc_now())
        pending = get_pending_messages(now_str)

        for db_id, chat_id, message_id, retry_count in pending:
            attempt_number = retry_count + 1
            try:
                sent_message = await bot.copy_message(
                    chat_id=int(chat_id),
                    from_chat_id=int(chat_id),
                    message_id=message_id,
                )
                mark_as_sent(db_id, sent_message.message_id)
            except TelegramAPIError as exc:
                is_permanent, error_text = classify_telegram_send_error(exc)
                retries_exhausted = attempt_number >= MAX_MESSAGE_RETRIES
                mark_message_delivery_error(
                    db_id,
                    error_text,
                    now_str,
                    attempt_number,
                    is_permanent or retries_exhausted,
                )
                logger.log(
                    logging.ERROR if (is_permanent or retries_exhausted) else logging.WARNING,
                    "Не удалось отправить сообщение %s пользователю %s. Попытка %s/%s. Статус: %s. Ошибка: %s",
                    message_id,
                    chat_id,
                    attempt_number,
                    MAX_MESSAGE_RETRIES,
                    "permanent" if (is_permanent or retries_exhausted) else "temporary",
                    error_text,
                )

        await asyncio.sleep(30)


async def check_digests():
    semaphore = asyncio.Semaphore(DIGEST_FETCH_CONCURRENCY)

    async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
        while True:
            now_str = serialize_datetime(utc_now())
            due_subs = get_due_subscriptions(now_str)

            users_subs = {}
            for sub in due_subs:
                users_subs.setdefault(sub[1], []).append(sub)

            for user_id, subs in users_subs.items():
                digest_lines = ["📰 <b>Твоя утренняя газета</b> ☕️\n\n"]
                has_news = False
                user_has_temporary_failures = False
                successful_subscriptions = []

                results = await asyncio.gather(
                    *(fetch_subscription_posts(sub, session, semaphore, now_str) for sub in subs),
                    return_exceptions=False,
                )

                for result in results:
                    if result["status"] == "error":
                        if not result["is_permanent"]:
                            user_has_temporary_failures = True
                        continue

                    successful_subscriptions.append((result["sub_id"], result["next_send_str"]))
                    if result["posts"]:
                        has_news = True
                        digest_lines.append(f"📌 <b>{result['title_safe']}</b>")
                        for post in result["posts"]:
                            text_safe = html.escape(post["text"])
                            digest_lines.append(f"🔹 <i>{text_safe}</i> <a href='{post['link']}'>[Читать]</a>\n")
                        digest_lines.append("")

                if has_news:
                    try:
                        await send_digest_chunks(user_id, digest_lines)
                        for sub_id, next_send_str in successful_subscriptions:
                            update_subscription_time(sub_id, now_str, next_send_str)
                    except TelegramAPIError as exc:
                        is_permanent, error_text = classify_telegram_send_error(exc)
                        for sub_id, _ in successful_subscriptions:
                            original = next((item for item in subs if item[0] == sub_id), None)
                            failure_count = original[6] if original else 0
                            new_failure_count = failure_count + 1
                            mark_subscription_delivery_error(
                                sub_id,
                                error_text,
                                now_str,
                                new_failure_count,
                                is_permanent or new_failure_count >= MAX_DIGEST_RETRIES,
                            )
                        retries_exhausted = any(
                            (next((item[6] for item in subs if item[0] == sub_id), 0) + 1) >= MAX_DIGEST_RETRIES
                            for sub_id, _ in successful_subscriptions
                        )
                        logger.log(
                            logging.ERROR if (is_permanent or retries_exhausted) else logging.WARNING,
                            "Не удалось отправить дайджест пользователю %s. Статус: %s. Ошибка: %s",
                            user_id,
                            "permanent" if (is_permanent or retries_exhausted) else "temporary",
                            error_text,
                        )
                else:
                    for sub_id, next_send_str in successful_subscriptions:
                        update_subscription_time(sub_id, now_str, next_send_str)

                if user_has_temporary_failures:
                    logger.info(
                        "Для пользователя %s дайджест будет частично повторён позже из-за временных ошибок чтения каналов.",
                        user_id,
                    )

            await asyncio.sleep(60)


async def cleanup_database():
    while True:
        now_str = serialize_datetime(utc_now())
        cleanup_stats = cleanup_old_records(now_str)
        if any(cleanup_stats.values()):
            logger.info("Очистка SQLite завершена: %s", cleanup_stats)
        await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)


async def main():
    init_db()
    await bot.delete_webhook(drop_pending_updates=True)
    asyncio.create_task(check_messages())
    asyncio.create_task(check_digests())
    asyncio.create_task(cleanup_database())
    logger.info("Бот успешно запущен и ждет сообщений...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
