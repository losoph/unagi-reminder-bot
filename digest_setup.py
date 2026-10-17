"""Модуль мастера оформления подписки на дайджест (setup_digest).

Содержит FSM-состояния, клавиатуры и обработчики callback-запросов
для выбора периодичности и сохранения новой подписки на канал.
"""

from __future__ import annotations

import html
import logging
from typing import Any

from aiogram import F, Router, types
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InaccessibleMessage,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from data.database import (
    add_subscription,
    is_valid_channel_username,
    normalize_channel_username,
    serialize_datetime,
    utc_now,
)
from digest_settings import (
    build_post_subscribe_keyboard,
    format_digest_schedule,
    get_next_digest_time,
    resolve_digest_settings,
)
from time_utils import local_now

logger = logging.getLogger(__name__)

router = Router(name="digest_setup")

_bot: Any = None


def configure(*, bot: Any = None) -> None:
    """Внедрение бота для удаления служебных сообщений."""
    global _bot
    if bot is not None:
        _bot = bot


def _get_bot(bot: Any = None, event: Any = None) -> Any:
    if bot is not None:
        return bot
    if event is not None and getattr(event, "bot", None) is not None:
        return event.bot
    return _bot


def user_id_from_message(message: types.Message) -> int:
    """SEC1 (п.5): идентичность пользователя берём из from_user, а не из chat.id."""
    if message.from_user is None:
        raise ValueError("Message without from_user is not supported")
    return message.from_user.id


def user_id_from_callback(callback: CallbackQuery) -> int:
    """SEC1 (п.5): идентичность пользователя берём из from_user, а не из chat.id."""
    if callback.from_user is None:
        raise ValueError("CallbackQuery without from_user is not supported")
    return callback.from_user.id


def get_callback_message(callback: CallbackQuery) -> Message | None:
    message = callback.message
    if message is None or isinstance(message, InaccessibleMessage):
        return None
    return message


def parse_callback_strip_prefix(data: str | None, prefix: str) -> str | None:
    if not data or not data.startswith(prefix):
        return None
    rest = data[len(prefix) :]
    return rest if rest else None


class DigestSetupState(StatesGroup):
    """FSM-состояния мастера оформления подписки."""

    waiting_for_channel = State()
    waiting_for_period = State()


def build_setup_digest_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура выбора периодичности дайджеста или добавления в Ленту."""
    kb = [
        [InlineKeyboardButton(text="📅 Раз в день", callback_data="period_daily")],
        [InlineKeyboardButton(text="🗓 Раз в неделю", callback_data="period_weekly")],
        [InlineKeyboardButton(text="📊 Раз в месяц", callback_data="period_monthly")],
        [InlineKeyboardButton(text="📰 В Ленту (без расписания)", callback_data="feed_add")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)


@router.callback_query(F.data == "digest_setup")
async def setup_digest(callback: CallbackQuery, state: FSMContext):
    callback_message = get_callback_message(callback)
    if callback_message is None:
        await callback.answer("❌ Сообщение недоступно.")
        return

    await callback_message.edit_text(
        "Как часто присылать новые посты из этого канала?",
        reply_markup=build_setup_digest_keyboard(),
    )


@router.callback_query(F.data.startswith("period_"))
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
    # SEC1 (п.10): имя канала идёт в href дайджеста — валидируем при сохранении.
    if not is_valid_channel_username(username):
        await callback.answer("❌ Некорректное имя канала.", show_alert=True)
        return

    now = local_now()
    settings = resolve_digest_settings(user_id_from_callback(callback), period, now)
    next_send = get_next_digest_time(period, now, settings)
    last_scraped = serialize_datetime(utc_now())
    try:
        sub_id = add_subscription(
            user_id_from_callback(callback),
            username,
            title,
            period,
            last_scraped,
            serialize_datetime(next_send),
        )
    except ValueError as exc:
        await callback.answer(f"🚫 {exc}", show_alert=True)
        return

    await callback.answer("✅ Подписка оформлена!")

    active_bot = _get_bot(None, callback)
    if active_bot is not None and callback_message.reply_to_message:
        try:
            await active_bot.delete_message(
                user_id_from_callback(callback),
                callback_message.reply_to_message.message_id,
            )
        except TelegramAPIError:
            pass

    title_safe = html.escape(title) if title else "Канал"
    confirmation = (
        f"✅ <b>Дайджест оформлен:</b> {title_safe}\n"
        f"Будет приходить {format_digest_schedule(period, settings)}.\n\n"
        f"Можно сразу изменить периодичность или время:"
    )
    keyboard = build_post_subscribe_keyboard(sub_id, period)
    try:
        await callback_message.edit_text(confirmation, parse_mode="HTML", reply_markup=keyboard)
    except TelegramAPIError:
        await callback_message.answer(confirmation, parse_mode="HTML", reply_markup=keyboard)

    await state.clear()
