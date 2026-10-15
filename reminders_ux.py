"""UX helpers and handlers for reminders (notes, snooze, follow-ups, list & card rendering)."""

import html
import logging
import os
import re
from datetime import datetime
from typing import Any

from aiogram import F, Router, types
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from data.database import (
    get_recent_delivered_reminder,
    get_reminder_by_id,
    replace_sent_reminder_with_pending,
    serialize_datetime,
    undo_followup_reschedule,
    update_reminder_note,
)
from time_parser import parse_reminder_time
from time_utils import (
    build_standard_message_keyboard,
    describe_quick_times,
    display_db_datetime,
    format_when,
)
from user_prefs import day_part_hours, user_now

logger = logging.getLogger(__name__)

router = Router(name="reminders_ux")

_bot: Any = None
_render_detail: Any = None


def configure(*, bot: Any = None, render_detail: Any = None) -> None:
    global _bot, _render_detail
    if bot is not None:
        _bot = bot
    if render_detail is not None:
        _render_detail = render_detail

# Pattern for logs copied from Telegram Desktop: [09.10.2026 17:57] Name: text...
LOG_PREFIX_PATTERN = re.compile(
    r"^\[\d{1,2}\.\d{1,2}\.\d{4}\s+\d{1,2}:\d{2}\]\s+([^:\n]{1,60}):\s*(.*)$",
    re.DOTALL,
)

RU_WEEKDAYS_SHORT = {0: "Пн", 1: "Вт", 2: "Ср", 3: "Чт", 4: "Пт", 5: "Сб", 6: "Вс"}
RU_WEEKDAYS_FULL = {
    0: "понедельник",
    1: "вторник",
    2: "среда",
    3: "четверг",
    4: "пятница",
    5: "суббота",
    6: "воскресенье",
}


class ReminderNoteState(StatesGroup):
    waiting_for_note = State()


def parse_copied_log_prefix(text: str | None) -> tuple[str | None, str]:
    """If text begins with `[дд.мм.гггг чч:мм] Имя: ...` (name <= 60 chars),
    returns (name, stripped_text). Otherwise returns (None, text).
    """
    if not text:
        return None, text or ""
    stripped = text.strip()
    m = LOG_PREFIX_PATTERN.match(stripped)
    if m:
        name = m.group(1).strip()
        body = m.group(2)
        return name, body
    return None, text


def extract_source_url(msg: types.Message) -> str | None:
    """Extract source URL from message forward origin:
    - channel with username -> https://t.me/<username>/<message_id>
    - user with id -> tg://user?id=<id>
    - otherwise -> None
    """
    origin = getattr(msg, "forward_origin", None)
    if not origin:
        return None
    origin_type = getattr(origin, "type", None)
    if origin_type == "channel":
        chat = getattr(origin, "chat", None)
        username = getattr(chat, "username", None) if chat else None
        msg_id = getattr(origin, "message_id", None)
        if username and msg_id:
            return f"https://t.me/{username}/{msg_id}"
    elif origin_type == "user":
        user = getattr(origin, "sender_user", None)
        user_id = getattr(user, "id", None) if user else None
        if user_id:
            return f"tg://user?id={user_id}"
    return None


def is_safe_source_url(url: str | None) -> bool:
    """Whitelists only https://t.me/... and tg://user?id=<digits>."""
    if not url or not isinstance(url, str):
        return False
    u = url.strip()
    if u.startswith("https://t.me/"):
        return True
    if re.match(r"^tg://user\?id=\d+$", u):
        return True
    return False


def resolve_reminder_display_data(
    source: str | None,
    full_text: str | None,
    text_preview: str | None,
    source_url: str | None = None,
) -> tuple[str, str, str | None]:
    """Resolves (effective_source, display_text, source_url).
    If source is 'Твой текст' (or empty) and text has a copied log prefix,
    effective_source becomes the log name, and display_text has the prefix stripped.
    """
    raw_text = full_text or text_preview or ""
    effective_source = source or "Твой текст"
    display_text = raw_text

    if effective_source in ("Твой текст", "Неизвестно", ""):
        log_name, stripped = parse_copied_log_prefix(raw_text)
        if log_name:
            effective_source = log_name
            display_text = stripped

    safe_url = source_url if is_safe_source_url(source_url) else None
    return effective_source, display_text, safe_url


def extract_first_line(text: str, max_len: int = 100) -> str:
    """Extracts first non-empty line of text, truncating to max_len."""
    if not text:
        return ""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    first = lines[0] if lines else text.strip()
    if len(first) > max_len:
        return first[: max_len - 3] + "..."
    return first


def format_button_label(dt: datetime, source_or_text: str, max_len: int = 40) -> str:
    """Formats button label: `ЧЧ:ММ · <источник или первые слова>` (<= 40 chars)."""
    time_str = dt.strftime("%H:%M")
    clean = " ".join((source_or_text or "").split()).strip()
    prefix = f"{time_str} · "
    budget = max_len - len(prefix)
    if budget <= 0:
        return time_str[:max_len]
    if len(clean) > budget:
        clean = clean[: budget - 1] + "…"
    return f"{prefix}{clean}"


def format_day_group_header(moment: datetime, now: datetime) -> str:
    """Header for day group in reminders list: «Сегодня», «Завтра», «Пн 13.10»."""
    days_diff = (moment.date() - now.date()).days
    if days_diff == 0:
        return "Сегодня"
    if days_diff == 1:
        return "Завтра"
    if days_diff == 2:
        return "Послезавтра"
    wd = RU_WEEKDAYS_SHORT.get(moment.weekday(), "")
    if moment.year != now.year:
        return f"{wd} {moment.strftime('%d.%m.%Y')}"
    return f"{wd} {moment.strftime('%d.%m')}"


def format_reminder_relative(dt_obj: datetime, now: datetime) -> str:
    """Short human-friendly relative time for list item, e.g. 'через 3 ч'."""
    secs = int((dt_obj - now).total_seconds())
    if secs < 0:
        return "просрочено"
    mins = secs // 60
    if mins < 1:
        return "меньше минуты"
    if mins < 60:
        return f"через {mins} мин"
    hours = mins // 60
    if hours < 24:
        rem_min = mins % 60
        return f"через {hours} ч" + (f" {rem_min} мин" if rem_min else "")
    days = hours // 24
    rem_hours = hours % 24
    return f"через {days} дн" + (f" {rem_hours} ч" if rem_hours else "")


def group_reminders_by_day(items: list[Any], now: datetime, dt_getter=None) -> list[tuple[str, list[Any]]]:
    """Groups reminders by calendar date into [(header, items), ...], preserving order."""
    if dt_getter is None:
        def dt_getter(item):
            val = item["send_at"] if isinstance(item, dict) or hasattr(item, "__getitem__") else item.send_at
            if isinstance(val, str):
                return display_db_datetime(val, tz=now.tzinfo)
            return val

    groups: dict[Any, tuple[str, list[Any]]] = {}
    for item in items:
        dt = dt_getter(item)
        cal_date = dt.date()
        if cal_date not in groups:
            header = format_day_group_header(dt, now)
            groups[cal_date] = (header, [])
        groups[cal_date][1].append(item)
    return [entry for entry in groups.values()]


def render_reminder_list_text(
    header: str,
    page: int,
    total_pages: int,
    page_items: list[Any],
    now: datetime,
) -> str:
    """Builds the HTML text for the reminders list screen with day grouping."""
    text_lines = [f"<b>{header}</b> (стр. {page} из {total_pages})\n"]

    groups = group_reminders_by_day(page_items, now)
    for day_header, items in groups:
        text_lines.append(f"<b>{day_header}</b>")
        for item in items:
            send_at = item["send_at"]
            dt_obj = display_db_datetime(send_at, tz=now.tzinfo)
            when = format_reminder_relative(dt_obj, now)

            source = item["source_name"] if "source_name" in item.keys() else item[3]
            preview = item["text_preview"] if "text_preview" in item.keys() else item[2]
            full_text = item["full_text"] if "full_text" in item.keys() else None
            note = item["note"] if "note" in item.keys() else None
            source_url = item["source_url"] if "source_url" in item.keys() else None
            row_tag = item["tag"] if "tag" in item.keys() else item[4]

            effective_source, display_text, eff_url = resolve_reminder_display_data(
                source, full_text, preview, source_url
            )
            time_str = dt_obj.strftime("%H:%M")

            if eff_url:
                source_safe = f'<a href="{html.escape(eff_url)}"><b>{html.escape(effective_source)}</b></a>'
            else:
                source_safe = f"<b>{html.escape(effective_source)}</b>"

            tag_chip = f" · 🏷 #{html.escape(row_tag)}" if row_tag else ""
            note_chip = " 📌" if note else ""
            first_line = extract_first_line(display_text, 100)
            first_line_safe = html.escape(first_line) if first_line else "Без текста"

            text_lines.append(
                f"• <b>{time_str}</b> <i>({when})</i> · {source_safe}{tag_chip}{note_chip}\n"
                f"  <i>{first_line_safe}</i>"
            )
        text_lines.append("")

    return "\n".join(text_lines).strip()


def build_reminder_list_keyboard(
    page_items: list[Any],
    page: int,
    total_pages: int,
    tag: str | None,
    now: datetime,
) -> InlineKeyboardMarkup:
    """Builds inline keyboard with 1 button per reminder with ЧЧ:ММ · <источник/текст>."""
    keyboard: list[list[InlineKeyboardButton]] = []

    for item in page_items:
        msg_id = item["id"] if "id" in item.keys() else item[0]
        send_at = item["send_at"] if "send_at" in item.keys() else item[1]
        dt_obj = display_db_datetime(send_at, tz=now.tzinfo)

        source = item["source_name"] if "source_name" in item.keys() else item[3]
        preview = item["text_preview"] if "text_preview" in item.keys() else item[2]
        full_text = item["full_text"] if "full_text" in item.keys() else None
        effective_source, display_text, _ = resolve_reminder_display_data(source, full_text, preview)

        first_line = extract_first_line(display_text, 30)
        label_text = effective_source if effective_source and effective_source != "Твой текст" else (first_line or "Напоминание")
        btn_label = format_button_label(dt_obj, label_text, max_len=40)

        keyboard.append([
            InlineKeyboardButton(text=btn_label, callback_data=f"rem_sel_{msg_id}")
        ])

    nav_row: list[InlineKeyboardButton] = []
    if page > 1:
        nav_row.append(InlineKeyboardButton(text="⬅️ Назад", callback_data="rem_page_prev"))
    else:
        nav_row.append(InlineKeyboardButton(text=" ", callback_data="rem_ignore"))
    nav_row.append(InlineKeyboardButton(text=f"Стр {page}/{total_pages}", callback_data="rem_ignore"))
    if page < total_pages:
        nav_row.append(InlineKeyboardButton(text="Вперёд ➡️", callback_data="rem_page_next"))
    else:
        nav_row.append(InlineKeyboardButton(text=" ", callback_data="rem_ignore"))
    keyboard.append(nav_row)

    filter_row = [InlineKeyboardButton(text="🏷 По тегам", callback_data="rem_view_tags")]
    if tag:
        filter_row.append(InlineKeyboardButton(text="📅 Все", callback_data="rem_tag_all"))
    filter_row.append(InlineKeyboardButton(text="🏠 В начало", callback_data="flow_home"))
    keyboard.append(filter_row)

    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def render_reminder_detail_text(row: Any, now: datetime) -> str:
    """Builds the HTML text for the reminder detail card."""
    send_at = row["send_at"] if "send_at" in row.keys() else row[1]
    dt_obj = display_db_datetime(send_at, tz=now.tzinfo)
    when = format_reminder_relative(dt_obj, now)
    weekday_name = RU_WEEKDAYS_FULL.get(dt_obj.weekday(), "")

    source = row["source_name"] if "source_name" in row.keys() else row[3]
    preview = row["text_preview"] if "text_preview" in row.keys() else row[2]
    full_text = row["full_text"] if "full_text" in row.keys() else None
    note = row["note"] if "note" in row.keys() else None
    source_url = row["source_url"] if "source_url" in row.keys() else None
    row_tag = row["tag"] if "tag" in row.keys() else row[4]
    message_id = row["message_id"] if "message_id" in row.keys() else row[5]

    effective_source, display_text, eff_url = resolve_reminder_display_data(
        source, full_text, preview, source_url
    )

    if eff_url:
        source_safe = f'<a href="{html.escape(eff_url)}">{html.escape(effective_source)}</a>'
    else:
        source_safe = html.escape(effective_source)

    tag_safe = html.escape(row_tag) if row_tag else "Без тега"
    when_str = f"⏰ <b>Когда:</b> {dt_obj.strftime('%d.%m.%Y')} ({weekday_name}) в {dt_obj.strftime('%H:%M')} <i>({when})</i>"

    if full_text:
        max_len = 3500
        if len(display_text) > max_len:
            full_text_safe = html.escape(display_text[:max_len]) + "\n\n⚠️ <i>(Текст усечён до 3500 символов)</i>"
        else:
            full_text_safe = html.escape(display_text)
    else:
        full_text_safe = html.escape(display_text or "Без текста")
        if message_id:
            full_text_safe += "\n\nℹ️ <i>(Полный текст — по кнопке «💬 К сообщению»)</i>"

    note_section = f"\n📌 <b>Примечание:</b> {html.escape(note)}\n" if note else ""

    return (
        f"{when_str}\n"
        f"🏷 <b>Тег:</b> #{tag_safe}\n"
        f"👤 <b>Источник:</b> {source_safe}\n"
        f"{note_section}\n"
        f"📝 <b>Текст:</b>\n{full_text_safe}"
    )


def build_reminder_detail_keyboard(
    msg_id: int,
    has_message_id: bool,
    source_url: str | None = None,
) -> InlineKeyboardMarkup:
    """Builds inline keyboard for reminder detail screen with:
    «💬 К сообщению» (if message_id), «↗️ Открыть в канале» (if https://t.me/...),
    «⏰ Перенести», «📌 Примечание», «🏷 Тег», «📁 В закладки», «🗑 Удалить», «🔙 К списку».
    """
    buttons: list[list[InlineKeyboardButton]] = []
    nav_row: list[InlineKeyboardButton] = []
    if has_message_id:
        nav_row.append(InlineKeyboardButton(text="💬 К сообщению", callback_data=f"rem_goto_{msg_id}"))
    if source_url and source_url.startswith("https://t.me/"):
        nav_row.append(InlineKeyboardButton(text="↗️ Открыть в канале", url=source_url))
    if nav_row:
        buttons.append(nav_row)
    buttons.append([
        InlineKeyboardButton(text="⏰ Перенести", callback_data=f"rem_resched_init_{msg_id}"),
        InlineKeyboardButton(text="📌 Примечание", callback_data=f"rem_note:{msg_id}:detail"),
    ])
    buttons.append([
        InlineKeyboardButton(text="🏷 Тег", callback_data=f"rem_tag_init_{msg_id}"),
        InlineKeyboardButton(text="📁 В закладки", callback_data=f"rem_tobk_{msg_id}"),
    ])
    buttons.append([
        InlineKeyboardButton(text="🗑 Удалить", callback_data=f"rem_del_{msg_id}"),
    ])
    buttons.append([
        InlineKeyboardButton(text="🔙 К списку", callback_data="flow_back"),
        InlineKeyboardButton(text="🏠 В начало", callback_data="flow_home"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def build_reschedule_note_keyboard(reminder_id: int) -> InlineKeyboardMarkup:
    """Button attached to confirmation message allowing the user to attach a note."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📝 Примечание", callback_data=f"rem_note:{reminder_id}")]
        ]
    )


@router.callback_query(F.data.startswith("rem_goto_"))
async def handle_rem_goto(callback: types.CallbackQuery):
    """Jumps to the original message by sending a short reply to message_id."""
    data = callback.data or ""
    try:
        rem_id = int(data.split("_")[-1])
    except ValueError:
        await callback.answer("❌ Некорректный ID.")
        return

    if not callback.message:
        await callback.answer()
        return

    row = get_reminder_by_id(callback.message.chat.id, rem_id)
    if not row:
        await callback.answer("❌ Напоминание не найдено.", show_alert=True)
        return

    message_id = row["message_id"] if "message_id" in row.keys() else row[5]
    if not message_id:
        await callback.answer("Оригинал отсутствует.", show_alert=True)
        return

    bot = callback.bot or _bot
    if not bot:
        await callback.answer("Бот временно недоступен.", show_alert=True)
        return
    try:
        await bot.send_message(
            chat_id=callback.message.chat.id,
            text="👆 Оригинал сообщения",
            reply_to_message_id=message_id,
        )
        await callback.answer()
    except TelegramBadRequest:
        await callback.answer("Оригинал удалён, текст — в карточке.", show_alert=True)
    except TelegramAPIError as exc:
        logger.warning("rem_goto error: %s", exc)
        await callback.answer("Не удалось перейти к сообщению.", show_alert=True)


@router.callback_query(F.data.startswith("rem_note:"))
async def cb_rem_note(callback: types.CallbackQuery, state: FSMContext):
    data = callback.data or ""
    parts = data.split(":")
    if len(parts) < 2:
        await callback.answer("❌ Ошибка.")
        return
    try:
        rem_id = int(parts[1])
    except ValueError:
        await callback.answer("❌ Некорректный ID.")
        return

    is_detail = len(parts) > 2 and parts[2] == "detail"
    await state.set_state(ReminderNoteState.waiting_for_note)
    await state.update_data(note_rem_id=rem_id, note_from_detail=is_detail)
    await callback.answer()
    if callback.message:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await callback.message.reply(
            "📝 Введи примечание к напоминанию (до 200 символов):",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отмена", callback_data="rem_note_cancel")]]
            ),
        )


@router.callback_query(F.data == "rem_note_cancel")
async def cb_rem_note_cancel(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        if callback.message:
            await callback.message.delete()
    except Exception:
        pass
    await callback.answer("Ввод примечания отменён.")


@router.message(ReminderNoteState.waiting_for_note)
async def process_reminder_note(message: types.Message, state: FSMContext):
    text = (message.text or "").strip()
    if not text:
        await message.answer("❌ Примечание не должно быть пустым. Введи текст (до 200 символов):")
        return
    if len(text) > 200:
        await message.answer(f"❌ Примечание слишком длинное ({len(text)}/200 симв.). Попробуй покороче:")
        return

    data = await state.get_data()
    rem_id = data.get("note_rem_id")
    from_detail = data.get("note_from_detail", False)
    if not rem_id:
        await message.answer("❌ Ошибка: не найден ID напоминания.")
        await state.clear()
        return

    updated = update_reminder_note(message.chat.id, int(rem_id), text)
    await state.clear()
    if updated:
        await message.answer("✅ Примечание сохранено!")
        if from_detail and _render_detail:
            await _render_detail(message.chat.id, int(rem_id), message, state)
    else:
        await message.answer("❌ Не удалось сохранить примечание (напоминание не найдено или уже отправлено).")


def build_sent_reminder_actions_keyboard(
    now: datetime,
    evening_hour: int = 20,
) -> InlineKeyboardMarkup:
    """Builds action keyboard for a delivered reminder (T16).

    The quick snooze is +3 h: an hour turned out too short. Old messages still carry
    ``snooze_1h`` and keep working.
    """
    row1: list[InlineKeyboardButton] = [
        InlineKeyboardButton(text="+3 ч", callback_data="snooze_3h"),
    ]
    if now.hour < evening_hour:
        row1.append(InlineKeyboardButton(text="Вечером", callback_data="snooze_evening"))
    row1.append(InlineKeyboardButton(text="Завтра утром", callback_data="snooze_tomorrow_morning"))

    return InlineKeyboardMarkup(
        inline_keyboard=[
            row1,
            [
                InlineKeyboardButton(text="⏰ Другое время", callback_data="sent_later"),
                InlineKeyboardButton(text="✅ Готово", callback_data="sent_done"),
            ],
            [
                InlineKeyboardButton(text="📁 Сохранить", callback_data="sent_save"),
                InlineKeyboardButton(text="🗑 Удалить", callback_data="sent_delete"),
            ],
        ]
    )


RESCHEDULE_FOLLOWUP_WINDOW_MINUTES = int(os.getenv("RESCHEDULE_FOLLOWUP_WINDOW_MINUTES", "10"))


def build_followup_reschedule_keyboard(new_db_id: int) -> InlineKeyboardMarkup:
    """Keyboard attached to followup reschedule confirmation with undo option (T17)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="↩️ Нет, это новое", callback_data=f"undo_resched_{new_db_id}")],
        ]
    )


async def send_standard_message_actions(
    bot: Any,
    chat_id: int,
    user_id: int,
    reply_to_message_id: int | None = None,
) -> Any:
    """Sends the standard «Что мне сделать с этим сообщением?» prompt with quick time buttons and bookmarking."""
    now = user_now(user_id)
    hours = day_part_hours(user_id)
    kb = build_standard_message_keyboard()
    prompt = "Что мне сделать с этим сообщением?"
    text = f"{prompt}\n\n⏰ {describe_quick_times(now, hours)}"
    if reply_to_message_id:
        try:
            return await bot.send_message(
                chat_id,
                text,
                parse_mode="HTML",
                reply_markup=kb,
                reply_to_message_id=reply_to_message_id,
            )
        except TelegramAPIError:
            pass
    return await bot.send_message(
        chat_id,
        text,
        parse_mode="HTML",
        reply_markup=kb,
    )


@router.callback_query(F.data.startswith("undo_resched_"))
async def handle_undo_followup_reschedule(callback: types.CallbackQuery):
    """Handles «↩️ Нет, это новое» callback: restores delivered reminder, removes pending, sends standard menu."""
    raw_id = callback.data[len("undo_resched_") :]
    try:
        new_db_id = int(raw_id)
    except ValueError:
        await callback.answer("❌ Некорректный ID.")
        return

    user_id = callback.from_user.id
    success, reason, snapshot = undo_followup_reschedule(user_id, new_db_id)
    if not success:
        if reason == "already_undone":
            await callback.answer("Уже отменено.")
            return
        await callback.answer("Уже нельзя отменить.", show_alert=True)
        return

    await callback.answer("Перенос отменён.")
    if callback.message:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass

    chat_id = callback.message.chat.id if callback.message else user_id
    orig_msg_id = snapshot.get("original_message_id") if snapshot else None
    bot_inst = callback.bot or _bot
    if bot_inst:
        await send_standard_message_actions(
            bot=bot_inst,
            chat_id=chat_id,
            user_id=user_id,
            reply_to_message_id=orig_msg_id,
        )


async def handle_followup_text_reschedule(
    message: types.Message,
    now: datetime | None = None,
    window_minutes: int | None = None,
) -> bool:
    """Attempts to reschedule the most recently delivered reminder if message text parses as a valid reminder time.
    Returns True if rescheduled, False otherwise.
    """
    user_id = message.chat.id
    current_now = now or user_now(user_id)
    hours = day_part_hours(user_id)
    text = (message.text or "").strip()
    scheduled_time = parse_reminder_time(text, current_now, hours=hours)
    if scheduled_time is None or scheduled_time <= current_now:
        return False

    win = window_minutes if window_minutes is not None else RESCHEDULE_FOLLOWUP_WINDOW_MINUTES
    candidate = get_recent_delivered_reminder(
        user_id,
        window_minutes=win,
        now=current_now,
    )
    if not candidate:
        return False

    preview = candidate.get("text_preview") or candidate.get("full_text") or ""
    try:
        new_db_id = replace_sent_reminder_with_pending(
            user_id,
            candidate["id"],
            candidate.get("message_id"),
            serialize_datetime(scheduled_time),
            preview,
            candidate.get("source_name") or "",
            undo={
                "original_text": text,
                "original_message_id": message.message_id,
            },
        )
    except Exception:
        logger.exception("replace_sent_reminder_with_pending failed in followup reschedule")
        return False

    confirmation = (
        f"✅ Отложил «{html.escape(preview)}» на {format_when(scheduled_time, current_now)}."
        if preview
        else f"✅ Отложил на {format_when(scheduled_time, current_now)}."
    )
    kb = build_followup_reschedule_keyboard(new_db_id)
    await message.reply(confirmation, parse_mode="HTML", reply_markup=kb)
    return True

