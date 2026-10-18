"""Модуль экспорта и импорта пользовательских данных (/export, /import, FSM импорта).

Содержит FSM-состояния, генерацию и валидацию payload экспорта/импорта,
а также обработчики команд и callback-запросов для переноса пользовательских данных.
"""

from __future__ import annotations

import io
import json
import logging
import os
from typing import Any

from aiogram import F, Router, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from data.database import (
    add_feed_subscription,
    add_message,
    add_saved_message,
    add_subscription,
    export_user_data,
    get_feed_subscription_by_username,
    is_valid_channel_username,
    normalize_channel_username,
    parse_db_datetime,
    serialize_datetime,
    set_feed_paused,
    set_subscription_paused,
    upsert_digest_settings,
    utc_now,
)
from digest_settings import (
    edit_or_answer,
    get_next_digest_time,
    resolve_digest_settings,
)
from time_utils import local_now

logger = logging.getLogger(__name__)

router = Router(name="data_transfer")

_bot: Any = None

MAX_IMPORT_SUBSCRIPTIONS = int(os.getenv("MAX_IMPORT_SUBSCRIPTIONS", "300"))
MAX_IMPORT_FEED = int(os.getenv("MAX_IMPORT_FEED", "200"))
MAX_IMPORT_BOOKMARKS = int(os.getenv("MAX_IMPORT_BOOKMARKS", "1000"))
MAX_IMPORT_REMINDERS = int(os.getenv("MAX_IMPORT_REMINDERS", "500"))
MAX_IMPORT_FILE_BYTES = int(os.getenv("MAX_IMPORT_FILE_BYTES", str(2 * 1024 * 1024)))
EXPORT_VERSION = 2


def configure(*, bot: Any = None) -> None:
    """Внедрение бота для отправки документов экспорта и промпта импорта."""
    global _bot
    if bot is not None:
        _bot = bot


def _get_bot(bot: Any = None, event: Any = None) -> Any:
    if bot is not None:
        return bot
    if event is not None and getattr(event, "bot", None) is not None:
        return event.bot
    if _bot is not None:
        return _bot
    raise RuntimeError("Bot instance is not configured in data_transfer")


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


class ImportState(StatesGroup):
    waiting_for_file = State()


async def _send_export(user_id: int, bot: Any = None):
    target_bot = _get_bot(bot)
    data = export_user_data(user_id)
    payload = {
        "app": "unagi-reminder-bot",
        "version": EXPORT_VERSION,
        "exported_at": serialize_datetime(utc_now()),
        **data,
    }
    raw = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    filename = f"unagi_export_{user_id}_{local_now().strftime('%Y%m%d_%H%M')}.json"
    feed_count = len(data.get("feed") or [])
    caption = (
        "📤 <b>Экспорт данных Unagi</b>\n"
        f"• Подписки: {len(data['subscriptions'])}\n"
        f"• Лента: {feed_count}\n"
        f"• Закладки: {len(data['bookmarks'])}\n"
        f"• Напоминания: {len(data['reminders'])}\n\n"
        "Чтобы восстановить — отправь команду /import и пришли этот файл."
    )
    await target_bot.send_document(
        user_id,
        BufferedInputFile(raw, filename=filename),
        caption=caption,
        parse_mode="HTML",
    )


@router.message(Command("export"))
async def cmd_export(message: types.Message, state: FSMContext, bot: Any = None):
    await state.clear()
    await _send_export(user_id_from_message(message), bot=bot or message.bot)


@router.callback_query(F.data == "data_export")
async def cb_export(callback: CallbackQuery, bot: Any = None):
    await callback.answer("📤 Готовлю файл...")
    await _send_export(user_id_from_callback(callback), bot=bot or callback.bot)


async def _prompt_import(user_id: int, state: FSMContext, bot: Any = None):
    target_bot = _get_bot(bot)
    await state.set_state(ImportState.waiting_for_file)
    await target_bot.send_message(
        user_id,
        "📥 Пришли JSON-файл, который ты ранее получил через /export.\n\n"
        "Я добавлю подписки, закладки и будущие напоминания к текущим "
        "(дубликаты подписок пропущу).",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отмена", callback_data="import_cancel")]]
        ),
    )


@router.message(Command("import"))
async def cmd_import(message: types.Message, state: FSMContext, bot: Any = None):
    await state.clear()
    await _prompt_import(user_id_from_message(message), state, bot=bot or message.bot)


@router.callback_query(F.data == "data_import")
async def cb_import(callback: CallbackQuery, state: FSMContext, bot: Any = None):
    await _prompt_import(user_id_from_callback(callback), state, bot=bot or callback.bot)
    await callback.answer()


@router.callback_query(F.data == "import_cancel")
async def cb_import_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await edit_or_answer(callback, "Импорт отменён.")
    await callback.answer()


def _import_user_payload(user_id: int, payload) -> str:
    if not isinstance(payload, dict):
        return "❌ Неверный формат файла."

    subscriptions = payload.get("subscriptions") or []
    feed = payload.get("feed") or []
    bookmarks = payload.get("bookmarks") or []
    reminders = payload.get("reminders") or []
    digest_settings = payload.get("digest_settings") or []
    now = local_now()
    added_subs = added_feed = added_bms = added_rems = 0
    already_in_feed = 0

    for setting in digest_settings[:10]:
        period = setting.get("period")
        if period not in ("daily", "weekly", "monthly"):
            continue
        try:
            upsert_digest_settings(
                user_id,
                period,
                send_hour=setting.get("send_hour"),
                send_minute=setting.get("send_minute"),
                weekday=setting.get("weekday"),
                month_day=setting.get("month_day"),
                monthly_mode=setting.get("monthly_mode"),
            )
        except Exception:
            logger.exception("Import: failed to apply digest setting")

    for sub in subscriptions[:MAX_IMPORT_SUBSCRIPTIONS]:
        username = normalize_channel_username(sub.get("channel_username") or "")
        if not username or not is_valid_channel_username(username):
            # SEC1 (п.10): имя канала должно быть безопасным для href.
            continue
        period = sub.get("period") if sub.get("period") in ("daily", "weekly", "monthly") else "daily"
        settings = resolve_digest_settings(user_id, period, now)
        next_send = serialize_datetime(get_next_digest_time(period, now, settings))
        try:
            sub_id = add_subscription(
                user_id,
                username,
                sub.get("channel_title") or username,
                period,
                serialize_datetime(utc_now()),
                next_send,
                tag=sub.get("tag"),
            )
            if sub.get("is_paused"):
                set_subscription_paused(user_id, sub_id, True)
            added_subs += 1
        except Exception:
            logger.exception("Import: failed to add subscription")

    for item in feed[:MAX_IMPORT_FEED]:
        username = normalize_channel_username(item.get("channel_username") or "")
        if not username or not is_valid_channel_username(username):
            continue
        try:
            if get_feed_subscription_by_username(user_id, username):
                already_in_feed += 1
                continue
            sub_id = add_feed_subscription(
                user_id,
                username,
                channel_title=item.get("channel_title") or username,
                tag=item.get("tag"),
            )
            if item.get("is_paused") and sub_id:
                set_feed_paused(user_id, sub_id, True)
            added_feed += 1
        except Exception:
            logger.exception("Import: failed to add feed subscription")

    for bookmark in bookmarks[:MAX_IMPORT_BOOKMARKS]:
        text = bookmark.get("full_text")
        if not text:
            continue
        # SEC1 (п.11): произвольный saved_at ломал карточку закладки при открытии.
        saved_at_raw = bookmark.get("saved_at")
        try:
            saved_at = parse_db_datetime(saved_at_raw) if saved_at_raw else utc_now()
        except (ValueError, TypeError):
            saved_at = utc_now()
        try:
            add_saved_message(
                user_id,
                text,
                bookmark.get("source_name") or "Импорт",
                bookmark.get("tag") or "Импорт",
                serialize_datetime(saved_at),
            )
            added_bms += 1
        except Exception:
            logger.exception("Import: failed to add bookmark")

    for reminder in reminders[:MAX_IMPORT_REMINDERS]:
        send_at = reminder.get("send_at")
        if not send_at:
            continue
        try:
            if parse_db_datetime(send_at) <= utc_now():
                continue  # пропускаем напоминания из прошлого
            text_preview = reminder.get("text_preview") or ""
            full_text = reminder.get("full_text") or text_preview
            note = reminder.get("note")
            source_url = reminder.get("source_url")
            add_message(
                user_id,
                None,
                send_at,
                text_preview=text_preview,
                source_name=reminder.get("source_name") or "Импорт",
                full_text=full_text,
                note=note,
                source_url=source_url,
            )
            added_rems += 1
        except Exception:
            logger.exception("Import: failed to add reminder")

    feed_line = f"• Лента: +{added_feed}"
    if already_in_feed:
        feed_line += f" (уже были в Ленте: {already_in_feed})"

    return (
        "✅ <b>Импорт завершён</b>\n"
        f"• Подписки: +{added_subs}\n"
        f"{feed_line}\n"
        f"• Закладки: +{added_bms}\n"
        f"• Напоминания: +{added_rems}\n\n"
        "Открой /list_digest, /saved или /list, чтобы проверить."
    )


@router.message(ImportState.waiting_for_file, F.document)
async def process_import_file(message: types.Message, state: FSMContext, bot: Any = None):
    document = message.document
    filename = (document.file_name or "").lower()
    is_jsonish = filename.endswith(".json") or document.mime_type in (
        "application/json",
        "text/json",
        "text/plain",
    )
    if not is_jsonish:
        await message.answer("❌ Это не похоже на JSON-файл. Пришли .json из /export или нажми «Отмена».")
        return
    if document.file_size and document.file_size > MAX_IMPORT_FILE_BYTES:
        await message.answer("❌ Файл слишком большой.")
        return

    await state.set_state(None)
    buffer = io.BytesIO()
    try:
        target_bot = _get_bot(bot, event=message)
        await target_bot.download(document, destination=buffer)
        payload = json.loads(buffer.getvalue().decode("utf-8"))
    except Exception:
        logger.exception("Import: failed to read file")
        await message.answer("❌ Не удалось прочитать файл как JSON. Проверь, что это экспорт из /export.")
        return

    report = _import_user_payload(user_id_from_message(message), payload)
    await message.answer(report, parse_mode="HTML")


@router.message(ImportState.waiting_for_file)
async def import_wrong_type(message: types.Message):
    await message.answer("📎 Пришли именно файл .json (как документ), либо нажми «Отмена».")
