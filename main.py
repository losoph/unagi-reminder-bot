import asyncio
import html
import io
import json
import logging
import os
import re
import secrets
import time
from datetime import datetime, timedelta, tzinfo

import aiohttp
from aiogram import Bot, Dispatcher, F, Router, types
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ChatType
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramNotFound,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, InaccessibleMessage, Message
from aiohttp_socks import ProxyConnector # type: ignore
from dotenv import load_dotenv

from data.database import (
    add_digest_posts,
    add_feed_subscription,
    add_message,
    add_saved_message,
    add_subscription,
    add_telegraph_digest,
    cleanup_old_records,
    CHANNEL_BACKLOG_MAX,
    CHANNEL_DELIVERY_BATCH_SIZE,
    clear_email_periods,
    confirm_email_code,
    clear_subscription_failure,
    count_due_subscriptions,
    count_email_sends_for_address,
    count_email_sends_for_user,
    delete_message,
    delete_saved_message,
    delete_user_email,
    export_user_data,
    get_ai_usage_today,
    get_channel_scan_cursor,
    get_digest_execution_by_id,
    get_digest_posts,
    get_email_delivery,
    get_feed_subscription_by_username,
    get_user_email,
    delete_subscription,
    get_channel_posts_since,
    get_due_subscriptions,
    get_user_subscriptions_due_until,
    defer_subscriptions,
    get_pending_messages,
    get_reminder_by_id,
    get_saved_message_by_id,
    get_scheduled_message_by_delivered_message_id,
    get_subscription_by_id,
    get_user_messages,
    get_user_subscriptions,
    get_user_tags,
    increment_ai_usage,
    init_db,
    is_channel_backlog_overloaded,
    is_valid_channel_username,
    mark_as_sent,
    mark_message_delivery_error,
    mark_reminder_as_done,
    mark_subscription_delivery_error,
    normalize_channel_username,
    parse_db_datetime,
    record_digest_cycle_heartbeat,
    record_digest_execution_finish,
    record_digest_execution_start,
    record_email_send,
    replace_sent_reminder_with_pending,
    reset_user_alert_consecutive_failures,
    retry_digest_execution,
    serialize_datetime,
    start_email_verification,
    set_email_period,
    set_feed_paused,
    set_subscription_paused,
    set_user_alerts_enabled,
    set_user_alerts_paused_until,
    update_channel_scan_cursor,
    update_subscription_time,
    update_subscription_schedule,
    update_reminder_tag,
    update_reminder_time,
    upsert_channel_posts,
    upsert_digest_settings,
    utc_now,
)
from scraper import ChannelFetchError, REQUEST_TIMEOUT, get_latest_posts
from channel_source import HybridChannelSource
import ai_assistant
import email_digest
from channel_links import find_channel_username, is_private_invite
from ranking import prioritize_channel_posts
from telegraph_publisher import describe_counts, publish_digest
from time_parser import parse_reminder_time
from digest_alerts import build_digest_alert_keyboard, process_pending_digest_alerts
from watchdog import WATCHDOG_CHECK_INTERVAL_SECONDS, run_watchdog_loop
from bot_help import (
    BOT_COMMANDS,
    HELP_TEXT,
    SETTINGS_TEXT,
    WELCOME_TEXT,
    build_help_keyboard,
    build_main_menu_keyboard,
    build_settings_keyboard,
)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
BOT_USERNAME = os.getenv("BOT_USERNAME", "").lstrip("@")
TELEGRAM_PROXY = os.getenv("TELEGRAM_PROXY") or os.getenv("PROXY_URL")
channel_source = HybridChannelSource()

if not BOT_TOKEN:
    raise ValueError("❌ Токен бота не найден! Проверьте файл .env")

bot_session = AiohttpSession(proxy=TELEGRAM_PROXY) if TELEGRAM_PROXY else None
bot = Bot(token=BOT_TOKEN, session=bot_session)
dp = Dispatcher()
import reminders_ux
import user_prefs
import feed
import bookmarks
from bookmarks import (  # noqa: F401
    SaveState,
    callback_menu_bookmarks,
    cmd_save_reply,
    cmd_saved,
    handle_bkdel,
    handle_bkmv_init,
    handle_kb_ignore,
    handle_kb_mv_idx,
    handle_kb_page_next,
    handle_kb_page_prev,
    handle_kb_sel_idx,
    handle_kb_tag_all,
    handle_kb_tag_selection,
    handle_kb_view_tags,
    process_new_tag,
    process_tag,
    render_kb_detail_screen,
    render_kb_list_screen,
    render_kb_move_screen,
    render_kb_tags_screen,
    save_sent_reminder,
    setup_bookmark,
    start_save_flow,
)
import digest_settings
from digest_settings import (  # noqa: F401
    SubTagState,
    _PERIOD_PLURAL,
    _PERIOD_RU,
    _PERIOD_TITLES,
    _WEEKDAY_ACC_RU,
    _WEEKDAY_NOM_RU,
    _WEEKDAY_SHORT_RU,
    _build_digest_candidate,
    _nth_weekday_of_month,
    _refresh_digest_settings,
    _sort_subscriptions_for_display,
    _subscription_status_marker,
    build_digest_channel_actions,
    build_digest_monthday_keyboard,
    build_digest_monthly_mode_keyboard,
    build_digest_schedule_keyboard,
    build_digest_settings_keyboard,
    build_digest_time_hours_keyboard,
    build_digest_time_minutes_keyboard,
    build_digest_weekday_keyboard,
    build_post_subscribe_keyboard,
    build_subscription_folder_keyboard,
    clear_subscription_folder,
    confirm_unsubscribe_all,
    edit_or_answer,
    format_digest_schedule,
    get_default_digest_settings,
    get_email_delivery_periods,
    get_next_digest_time,
    move_digest_subscription,
    open_digest_minute_picker,
    open_digest_monthday_picker,
    open_digest_monthly_mode_picker,
    open_digest_schedule,
    open_digest_settings,
    open_digest_subscription_actions,
    open_digest_time_picker,
    open_digest_weekday_picker,
    pause_all_subscriptions,
    pause_subscription,
    process_new_subscription_folder,
    prompt_new_subscription_folder,
    render_digest_schedule_text,
    render_digest_settings_text,
    render_subscription_actions,
    reschedule_user_period,
    resolve_digest_settings,
    resume_all_subscriptions,
    resume_subscription,
    save_digest_monthday,
    save_digest_monthly_mode,
    save_digest_time,
    save_digest_weekday,
    set_subscription_folder,
    unsubscribe_all_confirmed,
)
dp.include_router(reminders_ux.router)
dp.include_router(user_prefs.router)
dp.include_router(feed.router)
dp.include_router(bookmarks.router)
dp.include_router(digest_settings.router)
# SEC1 (п.5): бот работает только в личных сообщениях. Корневые фильтры
# отсекают группы/каналы до маршрутизации во вложенные роутеры, поэтому ни один
# пользовательский обработчик (main/reminders_ux/user_prefs/feed/bookmarks/digest_settings) не увидит
# сообщение или callback из группового чата.
dp.message.filter(F.chat.type == ChatType.PRIVATE)
dp.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)
feed.configure(bot=bot, channel_source=channel_source)
from time_utils import (  # noqa: E402
    _DAY_PART_HOURS,
    TIME_TEXT_EXAMPLES,
    TZ,
    ai_today_key,
    build_standard_message_keyboard,
    build_time_selection_keyboard,
    describe_quick_times,
    format_when,
    get_quick_scheduled_time,
    local_now,
)
from user_prefs import day_part_hours, user_now, user_tz  # noqa: E402
import digest_edition  # noqa: E402
fallback_router = Router(name="fallback")
MAX_MESSAGE_RETRIES = 5
MAX_DIGEST_RETRIES = 5
DIGEST_FETCH_CONCURRENCY = 5
DIGEST_DUE_BATCH_SIZE = max(1, int(os.getenv("DIGEST_DUE_BATCH_SIZE", "100")))
DIGEST_BACKLOG_RETRY_SECONDS = max(1, int(os.getenv("DIGEST_BACKLOG_RETRY_SECONDS", "5")))
DIGEST_CHECK_INTERVAL_SECONDS = int(os.getenv("DIGEST_CHECK_INTERVAL_SECONDS", 30 * 60))  # Можно переопределить через .env
RESCHEDULE_FOLLOWUP_WINDOW_MINUTES = int(os.getenv("RESCHEDULE_FOLLOWUP_WINDOW_MINUTES", "10"))
CLEANUP_INTERVAL_SECONDS = 6 * 60 * 60
# Дайджесты с таким числом постов и более публикуются в Telegraph одной ссылкой.
DIGEST_TELEGRAPH_THRESHOLD = int(os.getenv("DIGEST_TELEGRAPH_THRESHOLD", "4"))
# Максимальная длина превью поста в Telegram-сообщении дайджеста.
TELEGRAM_PREVIEW_CHARS = int(os.getenv("TELEGRAM_PREVIEW_CHARS", "300"))
# Лимиты на импорт, чтобы один файл не положил бота.
MAX_IMPORT_SUBSCRIPTIONS = int(os.getenv("MAX_IMPORT_SUBSCRIPTIONS", "300"))
MAX_IMPORT_FEED = int(os.getenv("MAX_IMPORT_FEED", "200"))
MAX_IMPORT_BOOKMARKS = int(os.getenv("MAX_IMPORT_BOOKMARKS", "1000"))
MAX_IMPORT_REMINDERS = int(os.getenv("MAX_IMPORT_REMINDERS", "500"))
MAX_IMPORT_FILE_BYTES = int(os.getenv("MAX_IMPORT_FILE_BYTES", str(2 * 1024 * 1024)))
AI_DAILY_LIMIT = ai_assistant.AI_DAILY_LIMIT
# Средняя скорость чтения для оценки времени прочтения постов канала.
READING_WORDS_PER_MINUTE = 180
EXPORT_VERSION = 2

# SEC1 (п.3): общий per-user кулдаун для дорогих сетевых операций.
EXPENSIVE_COMMAND_COOLDOWN_SECONDS = int(os.getenv("EXPENSIVE_COMMAND_COOLDOWN_SECONDS", "60"))
# SEC1 (п.4): суточные лимиты писем с подтверждением.
EMAIL_DAILY_LIMIT_PER_USER = int(os.getenv("EMAIL_DAILY_LIMIT_PER_USER", "5"))
EMAIL_DAILY_LIMIT_PER_ADDRESS = int(os.getenv("EMAIL_DAILY_LIMIT_PER_ADDRESS", "5"))

_expensive_command_last: dict[int, float] = {}

USER_FACING_ERROR = "Что-то пошло не так. Попробуй ещё раз позже."


def check_expensive_command_cooldown(user_id: int) -> int:
    """SEC1 (п.3): 0 — можно выполнять (время отмечено), иначе сколько секунд ждать."""
    now = time.monotonic()
    last = _expensive_command_last.get(user_id, 0.0)
    elapsed = now - last
    if elapsed < EXPENSIVE_COMMAND_COOLDOWN_SECONDS:
        return int(EXPENSIVE_COMMAND_COOLDOWN_SECONDS - elapsed) + 1
    _expensive_command_last[user_id] = now
    return 0


def user_id_from_message(message: types.Message) -> int:
    """SEC1 (п.5): идентичность пользователя берём из from_user, а не из chat.id."""
    from_user = getattr(message, "from_user", None)
    uid = getattr(from_user, "id", None)
    if isinstance(uid, int):
        return uid
    return message.chat.id


def user_id_from_callback(callback: CallbackQuery) -> int:
    """SEC1 (п.5): владелец callback — from_user.id; chat.id только как запасной путь."""
    from_user = getattr(callback, "from_user", None)
    uid = getattr(from_user, "id", None)
    if isinstance(uid, int):
        return uid
    message = getattr(callback, "message", None)
    chat = getattr(message, "chat", None) if message is not None else None
    return getattr(chat, "id", 0)


def create_telegram_http_session() -> aiohttp.ClientSession:
    if TELEGRAM_PROXY:
        return aiohttp.ClientSession(
            timeout=REQUEST_TIMEOUT,
            connector=ProxyConnector.from_url(TELEGRAM_PROXY),
        )
    return aiohttp.ClientSession(timeout=REQUEST_TIMEOUT)


async def fetch_channel_posts(
    channel_username: str,
    last_scraped_at: str | None,
    session: aiohttp.ClientSession,
) -> list[dict]:
    posts, source_name = await channel_source.fetch(
        channel_username,
        last_scraped_at,
        web_session=session,
    )
    upsert_channel_posts(channel_username, posts, source_name)
    return posts


def advance_channel_scan_cursor(channel_username: str, posts: list[dict]) -> None:
    """Move the shared channel scan cursor past freshly fetched posts.

    A Telegram Web scan that hit the page limit before the marker returns only the
    newest posts; moving the cursor past them would leave the unfetched gap behind
    for every subscriber of the channel, so the cursor stays put.
    """
    if not posts:
        return
    if not getattr(posts, "cursor_reached", True):
        logger.warning(
            "Scan of @%s did not reach the marker; channel scan cursor not advanced",
            channel_username,
        )
        return
    max_time = max(p["time"] for p in posts)
    max_id = max((p["id"] for p in posts if p.get("id") is not None), default=None)
    update_channel_scan_cursor(channel_username, max_time, max_id)


_QUICK_RESCHEDULE_ACTION: dict[str, str] = {
    "morning": "morning",
    "day": "day",
    "evening": "evening",
    "later": "now",
    "l": "now",
}

class ScheduleState(StatesGroup):
    waiting_for_datetime = State()
    waiting_for_time = State()
    waiting_for_minutes = State()


class ImportState(StatesGroup):
    waiting_for_file = State()


class EmailState(StatesGroup):
    waiting_for_email = State()
    waiting_for_code = State()


def chunk_html_text(lines, max_length=4000):
    chunks, current_lines = [], []
    current_len = 0
    for line in lines:
        line_len = len(line) + 1
        if current_len + line_len > max_length and current_lines:
            chunks.append("\n".join(current_lines) + "\n")
            current_lines = [line]
            current_len = line_len
        else:
            current_lines.append(line)
            current_len += line_len
    if current_lines:
        chunks.append("\n".join(current_lines) + "\n")
    return chunks







feed.configure(
    get_next_digest_time=get_next_digest_time,
    format_digest_schedule=format_digest_schedule,
    resolve_digest_settings=resolve_digest_settings,
)


def get_message_preview(msg: types.Message):
    text = msg.text or msg.caption or "🖼 Медиафайл"
    normalized_text = text.replace('\n', ' ')
    preview = normalized_text[:37] + "..." if len(normalized_text) > 40 else normalized_text

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
    return get_scheduled_message_by_delivered_message_id(user_id_from_message(message), replied.message_id)


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


def build_manual_date_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Сегодня", callback_data="sdate_today"),
                InlineKeyboardButton(text="Завтра", callback_data="sdate_tomorrow"),
            ],
            [
                InlineKeyboardButton(text="Послезавтра", callback_data="sdate_after_tomorrow"),
            ],
            [
                InlineKeyboardButton(text="В субботу", callback_data="sdate_saturday"),
                InlineKeyboardButton(text="В понедельник", callback_data="sdate_monday"),
            ],
            [
                InlineKeyboardButton(text="✍️ Вручную", callback_data="sdate_manual"),
            ],
            [
                InlineKeyboardButton(text="🔙 Назад", callback_data="flow_back"),
                InlineKeyboardButton(text="🏠 В начало", callback_data="flow_home"),
            ],
        ]
    )


def build_manual_hour_keyboard() -> InlineKeyboardMarkup:
    hours = [8, 9, 10, 13, 14, 15, 17, 18, 19, 20]
    rows = [
        [
            InlineKeyboardButton(text=f"{hour:02d}", callback_data=f"shour_{hour}")
            for hour in hours[index : index + 3]
        ]
        for index in range(0, len(hours), 3)
    ]
    rows.append([InlineKeyboardButton(text="✍️ Вручную время", callback_data="shour_manual")])
    rows.append([
        InlineKeyboardButton(text="🔙 Назад", callback_data="flow_back"),
        InlineKeyboardButton(text="🏠 В начало", callback_data="flow_home"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_manual_minute_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="00", callback_data="smin_0"),
                InlineKeyboardButton(text="15", callback_data="smin_15"),
                InlineKeyboardButton(text="30", callback_data="smin_30"),
                InlineKeyboardButton(text="45", callback_data="smin_45"),
            ],
            [
                InlineKeyboardButton(text="✍️ Вручную минуты", callback_data="smin_manual"),
            ],
            [
                InlineKeyboardButton(text="🔙 Назад", callback_data="flow_back"),
                InlineKeyboardButton(text="🏠 В начало", callback_data="flow_home"),
            ],
        ]
    )


def build_back_home_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🔙 Назад", callback_data="flow_back"),
                InlineKeyboardButton(text="🏠 В начало", callback_data="flow_home"),
            ]
        ]
    )


def build_sent_reminder_actions_keyboard(
    user_id: int | None = None,
    now: datetime | None = None,
    hours: dict[str, int] | None = None,
) -> InlineKeyboardMarkup:
    if user_id is not None:
        user_time = now or user_now(user_id)
        user_h = hours or day_part_hours(user_id)
    else:
        user_time = now or local_now()
        user_h = hours or {"morning": 9, "day": 14, "evening": 20}
    return reminders_ux.build_sent_reminder_actions_keyboard(
        now=user_time,
        evening_hour=user_h.get("evening", 20),
    )


def get_suggested_manual_time(now: datetime, hours: dict[str, int] | None = None) -> datetime:
    day_hours = hours or _DAY_PART_HOURS
    m_h = day_hours.get("morning", 9)
    d_h = day_hours.get("day", 14)
    e_h = day_hours.get("evening", 20)
    if now.hour < m_h - 1:
        return now.replace(hour=m_h, minute=0, second=0, microsecond=0)
    if now.hour < d_h - 1:
        return now.replace(hour=d_h, minute=0, second=0, microsecond=0)
    if now.hour < e_h:
        return now.replace(hour=e_h, minute=0, second=0, microsecond=0)
    return (now + timedelta(days=1)).replace(hour=m_h, minute=0, second=0, microsecond=0)


def get_manual_date(action: str, now: datetime) -> datetime | None:
    if action == "today":
        return now
    if action == "tomorrow":
        return now + timedelta(days=1)
    if action == "after_tomorrow":
        return now + timedelta(days=2)
    if action in {"saturday", "monday"}:
        target_weekday = 5 if action == "saturday" else 0
        days_ahead = (target_weekday - now.weekday()) % 7
        if days_ahead == 0:
            days_ahead = 7
        return now + timedelta(days=days_ahead)
    return None


def build_scheduled_time_from_state(data: dict, *, hour: int, minute: int, tz: tzinfo | None = None) -> datetime | None:
    selected_date = data.get("selected_date")
    if not selected_date:
        return None
    try:
        scheduled_time = datetime.strptime(selected_date, "%Y-%m-%d").replace(
            hour=hour,
            minute=minute,
            second=0,
            microsecond=0,
            tzinfo=tz or TZ,
        )
    except (TypeError, ValueError):
        return None
    return scheduled_time


def has_schedule_context(data: dict) -> bool:
    # Delivered text reminders (from bookmarks) have no source_message_id, so the
    # scheduled row id alone is enough; reminders rescheduled from /list carry rem_reschedule_id.
    return bool(
        data.get("message_id")
        or data.get("scheduled_db_id")
        or data.get("is_bookmark_scheduling")
        or data.get("rem_reschedule_id")
    )


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


def build_bot_deep_link(payload: str) -> str | None:
    if not BOT_USERNAME:
        return None
    return f"https://t.me/{BOT_USERNAME}?start={payload}"


def build_digest_action_link(label: str, payload: str) -> str:
    deep_link = build_bot_deep_link(payload)
    if deep_link:
        return f"<a href='{deep_link}'>{label}</a>"
    fallback_text = "/list_digest" if payload == "ds" else "/start"
    return f"{label} ({fallback_text})"


def format_post_preview(text: str, max_chars: int = TELEGRAM_PREVIEW_CHARS) -> str:
    cleaned = (text or "").strip()
    if max_chars and len(cleaned) > max_chars:
        return cleaned[: max_chars - 3] + "..."
    return cleaned


def append_digest_channel_lines(
    lines: list[str],
    sub_id: int,
    period: str,
    channel_title: str | None,
    posts: list[dict],
    omitted_count: int = 0,
    channel_username: str | None = None,
):
    unsubscribe_link = build_digest_action_link("Отписаться", f"du_{sub_id}")
    lines.append(f"📌 <b>{html.escape(channel_title) if channel_title else 'Канал'}</b>  {unsubscribe_link}")
    for post in posts:
        text_safe = html.escape(format_post_preview(post.get("text") or ""))
        # SEC1 (п.10): ссылка тоже экранируется, иначе кавычка в href ломает разметку.
        link_safe = html.escape(str(post.get("link") or ""), quote=True)
        lines.append(f"🔹 <i>{text_safe}</i> <a href='{link_safe}'>[Читать]</a>\n")

    if omitted_count > 0 and channel_username:
        from telegraph_publisher import plural_ru

        clean_user = html.escape(channel_username.lstrip("@"), quote=True)
        post_str = plural_ru(omitted_count, "пост", "поста", "постов")
        lines.append(f"▫️ <a href='https://t.me/{clean_user}'>ещё {post_str} в канале</a>\n")

    move_links = []
    for target_period in ("daily", "weekly", "monthly"):
        if target_period == period:
            continue
        move_links.append(
            build_digest_action_link(
                f"В {_PERIOD_TITLES[target_period].lower()}",
                f"dm_{sub_id}_{target_period}",
            )
        )
    lines.append("Перенести: " + " | ".join(move_links))
    lines.append("")


def render_digest_lines(title_plain: str, sections: list[dict]) -> list[str]:
    lines = [f"📰 <b>{html.escape(title_plain)}</b> ☕️", ""]
    current_group = None
    for section in sections:
        group = section.get("group")
        if group and group != current_group:
            lines += [f"━━━━━━━━━━\n<b>{html.escape(group)}</b>", ""]
        current_group = group
        append_digest_channel_lines(
            lines,
            section["sub_id"],
            section["period"],
            section["title"],
            section["posts"],
            omitted_count=section.get("omitted_count", 0),
            channel_username=section.get("username") or section.get("channel_username"),
        )
    lines.append(build_digest_action_link("⚙️ Настройки дайджеста", "ds"))
    return lines





async def deliver_digest(
    user_id: int,
    title_plain: str | None,
    sections: list[dict],
    *,
    group_by_period: bool = True,
) -> bool:
    """Send a digest: each period goes either to email or to Telegram, never both.

    Returns True if delivered. A failed email falls back to Telegram, so posts are never lost.
    With ``group_by_period`` the sections are ordered juniors-first and, when one delivery
    mixes periods, split into groups «За сутки / За неделю / За месяц». Without a title,
    each delivery is named by the senior period inside it.
    """
    sections = [s for s in sections if s["posts"]]
    if not sections:
        return False

    def prepare(part: list[dict]) -> tuple[str, list[dict]]:
        if group_by_period:
            part = digest_edition.arrange_edition_sections(part)
        return title_plain or digest_edition.edition_title(part, local_now()), part

    address, email_periods = get_email_delivery_periods(user_id)
    email_sections = [s for s in sections if s.get("period") in email_periods]
    telegram_sections = [s for s in sections if s.get("period") not in email_periods]
    note = ""
    if email_sections and not await send_digest_email(address, *prepare(email_sections)):
        telegram_sections += email_sections
        note = "📧 Не удалось отправить дайджест на почту — присылаю сюда."
    if telegram_sections:
        await send_telegram_digest(user_id, *prepare(telegram_sections), note)
    return True


async def send_telegram_digest(user_id: int, title_plain: str, sections: list[dict], note: str = "") -> None:
    """Telegram delivery, switching to Telegraph for large digests."""
    total_posts = sum(len(s["posts"]) for s in sections)
    if note:
        await bot.send_message(user_id, note)

    if total_posts >= DIGEST_TELEGRAPH_THRESHOLD:
        url = await publish_digest(title_plain, sections)
        if url:
            add_telegraph_digest(user_id, url, title_plain, len(sections), total_posts)
            settings_link = build_digest_action_link("⚙️ Настройки дайджеста", "ds")
            text = (
                f"📰 <b>{html.escape(title_plain)}</b> ☕️\n"
                f"{describe_counts(len(sections), total_posts)}\n\n"
                f"📖 <a href='{url}'>Открыть дайджест целиком</a>\n\n"
                f"{settings_link}"
            )
            await bot.send_message(
                user_id,
                text,
                parse_mode="HTML",
                link_preview_options=LinkPreviewOptions(is_disabled=False),
            )
            return
        logger.warning("Telegraph publish failed for user %s, falling back to chunks", user_id)

    await send_digest_chunks(user_id, render_digest_lines(title_plain, sections))


def build_section_actions(section: dict) -> dict | None:
    """Deep links «Отписаться» and «Перенести в …» for one channel of an emailed digest.

    Same payloads as the links in a Telegram digest (du_/dm_), so a tap in the letter opens
    the bot and acts at once. None when the bot username is unknown.
    """
    sub_id = section.get("sub_id")
    if sub_id is None:
        return None
    unsubscribe_url = build_bot_deep_link(f"du_{sub_id}")
    if not unsubscribe_url:
        return None
    moves = [
        (f"в {_PERIOD_TITLES[period].lower()}", build_bot_deep_link(f"dm_{sub_id}_{period}"))
        for period in ("daily", "weekly", "monthly")
        if period != section.get("period")
    ]
    return {"unsubscribe": unsubscribe_url, "move": moves}


async def send_digest_email(address: str, title_plain: str, sections: list[dict]) -> bool:
    """Email delivery of a digest; never raises."""
    try:
        title = f"{title_plain} — {local_now().strftime('%d.%m.%Y')}"
        manage_url = build_bot_deep_link("em_off")
        email_sections = [{**section, "actions": build_section_actions(section)} for section in sections]
        html_body, text_body = email_digest.build_digest_email(
            title,
            email_sections,
            manage_url=manage_url,
            settings_url=build_bot_deep_link("ds"),
        )
        return await email_digest.send_email(address, title, text_body, html_body, unsubscribe_url=manage_url)
    except Exception:
        logger.exception("Email digest to %s failed", address.split("@", 1)[-1])
        return False


async def fetch_subscription_posts(
    sub: tuple,
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore,
    now_str: str,
    prefetched_channels: dict[str, dict] | None = None,
):
    sub_id, user_id, username, title, period, last_scraped, failure_count, last_post_id = sub[:8]
    scheduled_at = sub[8] if len(sub) > 8 else None
    title_safe = html.escape(title) if title else "Канал"

    async with semaphore:
        try:
            settings = resolve_digest_settings(user_id, period)
            next_send_base = digest_edition.next_send_base(local_now(), scheduled_at)
            next_send_str = serialize_datetime(get_next_digest_time(period, next_send_base, settings))
            prefetched = (prefetched_channels or {}).get(normalize_channel_username(username))
            if prefetched is not None:
                if prefetched.get("error") is not None:
                    raise prefetched["error"]
                fetched_posts = prefetched["posts"]
                source_name = prefetched["source"]
            else:
                channel_scanned_at, _ = get_channel_scan_cursor(username)
                if channel_scanned_at and last_scraped:
                    scan_marker = serialize_datetime(max(parse_db_datetime(last_scraped), parse_db_datetime(channel_scanned_at)))
                else:
                    scan_marker = channel_scanned_at or last_scraped
                fetched_posts, source_name = await channel_source.fetch(
                    username,
                    scan_marker,
                    web_session=session,
                )
                upsert_channel_posts(username, fetched_posts, source_name)
                advance_channel_scan_cursor(username, fetched_posts)

            # SEC1 (п.14): берём batch+1, чтобы отличить «ровно батч» от «есть backlog».
            available_posts = get_channel_posts_since(
                username, last_scraped, last_post_id, limit=CHANNEL_DELIVERY_BATCH_SIZE + 1
            )
            if fetched_posts and not available_posts:
                # Compatibility for a malformed legacy link without a numeric post id.
                available_posts = fetched_posts

            cursor_reached = getattr(fetched_posts, "cursor_reached", True)

            # Backlog batching: take up to CHANNEL_DELIVERY_BATCH_SIZE posts
            if len(available_posts) > CHANNEL_DELIVERY_BATCH_SIZE:
                posts_batch = available_posts[:CHANNEL_DELIVERY_BATCH_SIZE]
                has_remaining_backlog = True
            else:
                posts_batch = available_posts
                has_remaining_backlog = False

            if posts_batch:
                delivered_marker = max(
                    (post["time"] for post in posts_batch),
                    default=parse_db_datetime(last_scraped),
                )
                delivered_post_id = max(
                    (post["id"] for post in posts_batch if post.get("id") is not None),
                    default=last_post_id,
                )
            else:
                delivered_marker = parse_db_datetime(last_scraped)
                delivered_post_id = last_post_id

            is_partial = (not cursor_reached) or has_remaining_backlog
            if not cursor_reached:
                logger.warning(
                    "Cursor not reached for @%s (page limit reached); edition is partial, delivery cursor advanced to %s (%s)",
                    username,
                    delivered_marker,
                    delivered_post_id,
                )

            selected_posts, omitted_count = prioritize_channel_posts(username, posts_batch)
            return {
                "status": "ok",
                "sub_id": sub_id,
                "period": period,
                "username": username,
                "title": title,
                "title_safe": title_safe,
                "posts": selected_posts,
                "omitted_count": omitted_count,
                "is_partial": is_partial,
                "has_backlog": has_remaining_backlog,
                "next_send_str": next_send_str,
                "last_scraped_str": serialize_datetime(delivered_marker),
                "last_post_id": delivered_post_id,
                "source": source_name,
            }
        except ChannelFetchError as exc:
            new_failure_count = failure_count + 1
            is_permanent = exc.permanent
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
        except Exception as exc:
            new_failure_count = failure_count + 1
            mark_subscription_delivery_error(
                sub_id,
                f"Внутренняя ошибка чтения: {exc}",
                now_str,
                new_failure_count,
                False,
            )
            logger.exception(
                "Неожиданная ошибка подписки %s (@%s); остальные дайджесты продолжат работу",
                sub_id,
                username,
            )
            return {
                "status": "error",
                "sub_id": sub_id,
                "is_permanent": False,
            }


async def prefetch_due_channels(
    due_subs: list,
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore,
) -> dict[str, dict]:
    oldest_markers: dict[str, str | None] = {}
    for sub in due_subs:
        username = normalize_channel_username(sub[2])
        marker = sub[5]
        current = oldest_markers.get(username)
        if current is None or parse_db_datetime(marker) < parse_db_datetime(current):
            oldest_markers[username] = marker

    async def load(username: str, marker: str | None) -> tuple[str, dict]:
        async with semaphore:
            try:
                # Check for backpressure: if backlog exceeds limit, pause remote fetch to avoid overflow
                if is_channel_backlog_overloaded(username):
                    logger.warning(
                        "Backpressure triggered for channel @%s: backlog exceeds %d posts; skipping telegram fetch until backlog is drained",
                        username,
                        CHANNEL_BACKLOG_MAX,
                    )
                    return username, {"posts": [], "source": "backpressure", "error": None, "backpressure": True}

                # Scan cursor: check if channel has a more recent scan cursor
                channel_scanned_at, _ = get_channel_scan_cursor(username)
                if channel_scanned_at and marker:
                    scan_marker = serialize_datetime(max(parse_db_datetime(marker), parse_db_datetime(channel_scanned_at)))
                else:
                    scan_marker = channel_scanned_at or marker

                posts, source_name = await channel_source.fetch(
                    username,
                    scan_marker,
                    web_session=session,
                )
                upsert_channel_posts(username, posts, source_name)
                advance_channel_scan_cursor(username, posts)
                return username, {"posts": posts, "source": source_name, "error": None}
            except ChannelFetchError as exc:
                return username, {"posts": [], "source": None, "error": exc}
            except Exception as exc:
                logger.exception("Неожиданная ошибка общего ingestion канала @%s", username)
                return username, {
                    "posts": [],
                    "source": None,
                    "error": ChannelFetchError(f"Внутренняя ошибка ingestion @{username}: {exc}"),
                }

    results = await asyncio.gather(
        *(load(username, marker) for username, marker in oldest_markers.items())
    )
    return dict(results)


async def handle_digest_deep_link(message: types.Message, payload: str) -> bool:
    user_id = user_id_from_message(message)

    if payload == "ds":
        await message.answer(
            render_digest_settings_text(user_id),
            parse_mode="HTML",
            reply_markup=build_digest_settings_keyboard(get_user_subscriptions(user_id)),
        )
        return True

    if payload == "em_off":
        if clear_email_periods(user_id):
            await message.answer("📭 Письма с дайджестами отключены — все дайджесты снова приходят сюда, в Telegram. Вернуть почту — /email.")
        else:
            await message.answer("Письма с дайджестами и так не приходят. Настроить — /email.")
        return True

    if payload.startswith("du_"):
        sub_id = parse_callback_int_suffix(payload, "du_")
        if sub_id is None:
            return False
        sub = get_subscription_by_id(user_id, sub_id)
        if not sub:
            await message.answer("Подписка уже удалена или не найдена.")
            return True
        delete_subscription(user_id, sub_id)
        await message.answer(
            f"🚫 Подписка на <b>{html.escape(sub['channel_title']) if sub['channel_title'] else 'канал'}</b> удалена.",
            parse_mode="HTML",
        )
        await message.answer(
            render_digest_settings_text(user_id),
            parse_mode="HTML",
            reply_markup=build_digest_settings_keyboard(get_user_subscriptions(user_id)),
        )
        return True

    if payload.startswith("dm_"):
        payload_body = parse_callback_strip_prefix(payload, "dm_")
        if not payload_body or "_" not in payload_body:
            return False
        sub_id_str, new_period = payload_body.split("_", 1)
        try:
            sub_id = int(sub_id_str)
        except ValueError:
            return False
        sub = get_subscription_by_id(user_id, sub_id)
        if not sub:
            await message.answer("Подписка не найдена.")
            return True
        if new_period == sub["period"]:
            await message.answer("Этот период уже выбран.")
            return True
        settings = resolve_digest_settings(user_id, new_period)
        next_send_at = serialize_datetime(get_next_digest_time(new_period, local_now(), settings))
        try:
            add_subscription(
                user_id,
                sub["channel_username"],
                sub["channel_title"],
                new_period,
                sub["last_scraped_at"] or serialize_datetime(utc_now()),
                next_send_at,
            )
        except ValueError as exc:
            await message.answer(f"🚫 {exc}")
            return True
        delete_subscription(user_id, sub_id)
        await message.answer(
            f"↪️ <b>{html.escape(sub['channel_title']) if sub['channel_title'] else 'Канал'}</b> перенесён в {_PERIOD_TITLES[new_period].lower()} дайджест.",
            parse_mode="HTML",
        )
        await message.answer(
            render_digest_settings_text(user_id),
            parse_mode="HTML",
            reply_markup=build_digest_settings_keyboard(get_user_subscriptions(user_id)),
        )
        return True

    return False




async def push_screen(state: FSMContext, screen_name: str, **kwargs):
    data = await state.get_data()
    history = data.get("screen_history", [])
    history = list(history)
    # Don't push duplicate screens consecutively
    if not history or history[-1].get("screen") != screen_name or history[-1].get("args") != kwargs:
        history.append({"screen": screen_name, "args": kwargs})
        await state.update_data(screen_history=history)


async def pop_screen(state: FSMContext) -> dict | None:
    data = await state.get_data()
    history = data.get("screen_history", [])
    if not history:
        return None
    history = list(history)
    history.pop()  # Pop the current screen
    await state.update_data(screen_history=history)
    if history:
        return history[-1]
    return None


async def render_screen(chat_id: int, screen: dict, target: types.Message | types.CallbackQuery, state: FSMContext):
    name = screen["screen"]
    args = screen.get("args", {})
    
    if name == "kb_tags":
        await render_kb_tags_screen(chat_id, target, state)
    elif name == "kb_list":
        await render_kb_list_screen(chat_id, args.get("tag"), args.get("page", 1), target, state)
    elif name == "kb_detail":
        await render_kb_detail_screen(chat_id, args.get("msg_id"), target, state)
    elif name == "kb_move":
        await render_kb_move_screen(chat_id, args.get("msg_id"), target, state)
    elif name == "rem_tags":
        await render_rem_tags_screen(chat_id, target, state)
    elif name == "rem_list":
        await render_rem_list_screen(chat_id, args.get("tag"), args.get("page", 1), target, state)
    elif name == "rem_detail":
        await render_rem_detail_screen(chat_id, args.get("msg_id"), target, state)
    elif name == "rem_move":
        await render_rem_move_screen(chat_id, args.get("msg_id"), target, state)
    elif name == "manual_date":
        markup = build_manual_date_keyboard()
        if isinstance(target, types.CallbackQuery):
            if isinstance(target.message, types.Message):
                await target.message.edit_text("Выбери дату:", reply_markup=markup)
            else:
                await bot.send_message(chat_id, "Выбери дату:", reply_markup=markup)
            await target.answer()
        else:
            await target.answer("Выбери дату:", reply_markup=markup)
    elif name == "manual_hour":
        markup = build_manual_hour_keyboard()
        selected_date_str = args.get("selected_date")
        prompt_text = f"Дата: {selected_date_str}\nВыбери час:"
        if isinstance(target, types.CallbackQuery):
            if isinstance(target.message, types.Message):
                await target.message.edit_text(prompt_text, reply_markup=markup)
            else:
                await bot.send_message(chat_id, prompt_text, reply_markup=markup)
            await target.answer()
        else:
            await target.answer(prompt_text, reply_markup=markup)
    elif name == "manual_minute":
        markup = build_manual_minute_keyboard()
        hour = args.get("selected_hour", 0)
        prompt_text = f"Время: {hour:02d}:00\nВыбери минуты:"
        if isinstance(target, types.CallbackQuery):
            if isinstance(target.message, types.Message):
                await target.message.edit_text(prompt_text, reply_markup=markup)
            else:
                await bot.send_message(chat_id, prompt_text, reply_markup=markup)
            await target.answer()
        else:
            await target.answer(prompt_text, reply_markup=markup)
    else:
        await state.clear()
        if isinstance(target, types.CallbackQuery):
            await target.message.answer(WELCOME_TEXT, reply_markup=build_main_menu_keyboard())
            await target.answer()
        else:
            await target.answer(WELCOME_TEXT, reply_markup=build_main_menu_keyboard())


bookmarks.configure(render_screen=render_screen)


@dp.message(Command("home", "cancel"), StateFilter("*"))
async def cmd_home(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(WELCOME_TEXT, parse_mode="HTML", reply_markup=build_main_menu_keyboard())


@dp.message(Command("back"), StateFilter("*"))
async def cmd_back(message: types.Message, state: FSMContext):
    prev = await pop_screen(state)
    if prev:
        await render_screen(user_id_from_message(message), prev, message, state)
    else:
        await state.clear()
        await message.answer(WELCOME_TEXT, parse_mode="HTML", reply_markup=build_main_menu_keyboard())


@dp.callback_query(F.data == "flow_home")
async def callback_home(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await callback.message.edit_text(WELCOME_TEXT, parse_mode="HTML", reply_markup=build_main_menu_keyboard())
    except TelegramAPIError:
        await callback.message.answer(WELCOME_TEXT, parse_mode="HTML", reply_markup=build_main_menu_keyboard())
        try:
            await callback.message.delete()
        except TelegramAPIError:
            pass
    await callback.answer()


@dp.callback_query(F.data == "flow_back")
async def callback_back(callback: types.CallbackQuery, state: FSMContext):
    prev = await pop_screen(state)
    if prev:
        await render_screen(user_id_from_callback(callback), prev, callback, state)
    else:
        await state.clear()
        try:
            await callback.message.edit_text(WELCOME_TEXT, parse_mode="HTML", reply_markup=build_main_menu_keyboard())
        except TelegramAPIError:
            await callback.message.answer(WELCOME_TEXT, parse_mode="HTML", reply_markup=build_main_menu_keyboard())
            try:
                await callback.message.delete()
            except TelegramAPIError:
                pass
        await callback.answer()


@dp.message(CommandStart())
async def cmd_start(message: types.Message, state: FSMContext, command: CommandObject):
    await state.clear()
    if command.args and await handle_digest_deep_link(message, command.args.strip()):
        return
    await message.answer(WELCOME_TEXT, parse_mode="HTML", reply_markup=build_main_menu_keyboard())


@dp.message(Command("help"), StateFilter("*"))
async def cmd_help(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(HELP_TEXT, parse_mode="HTML", reply_markup=build_help_keyboard())


@dp.callback_query(F.data == "help_full")
async def callback_help(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await edit_or_answer(callback, HELP_TEXT, reply_markup=build_help_keyboard())
    await callback.answer()


@dp.message(Command("settings"), StateFilter("*"))
async def cmd_settings(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        SETTINGS_TEXT,
        parse_mode="HTML",
        reply_markup=build_settings_keyboard(email_digest.is_enabled()),
    )


@dp.callback_query(F.data == "settings_menu")
async def callback_settings(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await edit_or_answer(callback, SETTINGS_TEXT, reply_markup=build_settings_keyboard(email_digest.is_enabled()))
    await callback.answer()


@dp.callback_query(F.data == "menu_rem")
async def callback_menu_reminders(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    if not get_user_messages(user_id_from_callback(callback)):
        await callback.answer("📭 Напоминаний нет. Перешли сообщение или напиши текст — отложу его.", show_alert=True)
        return
    await push_screen(state, "rem_list", tag=None, page=1)
    await render_rem_list_screen(user_id_from_callback(callback), None, 1, callback, state)


async def set_bot_commands(bot: Bot):
    commands = [types.BotCommand(command=name, description=text) for name, text in BOT_COMMANDS]
    await bot.set_my_commands(commands)


async def _apply_replied_reschedule(
    message: types.Message,
    scheduled_msg,
    scheduled_time: datetime,
    confirmation: str,
) -> None:
    db_id, source_message_id, _, _, preview, source, *_ = scheduled_msg
    try:
        new_db_id = replace_sent_reminder_with_pending(
            user_id_from_message(message),
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
    kb = reminders_ux.build_reschedule_note_keyboard(new_db_id)
    await message.answer(confirmation, reply_markup=kb)


async def _reschedule_replied_reminder(
    message: types.Message,
    quick_action: str,
    success_answer: str,
) -> None:
    scheduled_msg = get_replied_sent_schedule(message)
    if not scheduled_msg:
        await message.answer("Ответь этой командой на доставленное напоминание от бота.")
        return

    user_id = user_id_from_message(message)
    now = user_now(user_id)
    hours = day_part_hours(user_id)
    scheduled_time, label = get_quick_scheduled_time(quick_action, now, hours=hours)
    if scheduled_time is None:
        await message.answer("❌ Ошибка при планировании времени.")
        return
    await _apply_replied_reschedule(message, scheduled_msg, scheduled_time, success_answer.format(label=label))


async def reschedule_replied_by_text(message: types.Message, scheduled_msg, text: str) -> None:
    """«завтра 11» / «24/09 в 9» / «через 2 дня» as a reply to a delivered reminder (or /at)."""
    now = user_now(user_id_from_message(message))
    hours = day_part_hours(user_id_from_message(message))
    scheduled_time = parse_reminder_time(text, now, hours=hours)
    if scheduled_time is None:
        await message.answer(
            f"🤔 Не понял время. Примеры: {TIME_TEXT_EXAMPLES}, «в пятницу в 10», «18:30».\n"
            "Или нажми «⏰ Отложить» под напоминанием."
        )
        return
    if scheduled_time <= now:
        await message.answer("❌ Это время уже прошло.")
        return
    await _apply_replied_reschedule(
        message, scheduled_msg, scheduled_time, f"✅ Отложил на {format_when(scheduled_time, now)}."
    )


@dp.message(Command("morning", "day", "evening", "later", "l"))
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
async def cmd_at(message: types.Message, command: CommandObject):
    scheduled_msg = get_replied_sent_schedule(message)
    if not scheduled_msg:
        await message.answer("Ответь этой командой на доставленное напоминание от бота.")
        return

    arg = (command.args or "").strip()
    if not arg:
        await message.answer("Формат: /at 24.09 18:30, /at завтра 11 или /at через 2 дня")
        return
    await reschedule_replied_by_text(message, scheduled_msg, arg)


@dp.message(Command("delete", "d"))
async def cmd_delete_reply(message: types.Message):
    scheduled_msg = get_replied_sent_schedule(message)
    if not scheduled_msg:
        await message.answer("Ответь этой командой на доставленное напоминание от бота.")
        return

    db_id = scheduled_msg[0]
    delete_message(user_id_from_message(message), db_id)
    try:
        if message.reply_to_message is not None:
            await message.reply_to_message.delete()
        await message.delete()
    except TelegramAPIError:
        pass


@dp.callback_query(F.data == "sent_later")
async def show_sent_reminder_time_actions(callback: CallbackQuery):
    callback_message = get_callback_message(callback)
    if callback_message is None:
        await callback.answer("❌ Сообщение недоступно.")
        return

    scheduled_msg = get_scheduled_message_by_delivered_message_id(
        user_id_from_callback(callback), callback_message.message_id
    )
    if not scheduled_msg:
        await callback.answer("Напоминание уже перенесено.")
        return

    # Swap the buttons in place instead of posting a separate «when?» message.
    prefix = f"senttime_{callback_message.message_id}"
    try:
        await callback_message.edit_reply_markup(
            reply_markup=build_time_selection_keyboard(prefix, back_callback=f"{prefix}_back")
        )
    except TelegramBadRequest:
        pass
    user_id = user_id_from_callback(callback)
    await callback.answer(f"{describe_quick_times(user_now(user_id), day_part_hours(user_id))}\nИли ответь текстом: {TIME_TEXT_EXAMPLES}")


@dp.callback_query(F.data.startswith("senttime_"))
async def handle_sent_reminder_time_selection(callback: CallbackQuery, state: FSMContext):
    callback_message = get_callback_message(callback)
    if callback_message is None:
        await callback.answer("❌ Сообщение недоступно.")
        return

    payload = parse_callback_strip_prefix(callback.data, "senttime_")
    if payload is None:
        await callback.answer("❌ Некорректные данные.")
        return

    try:
        delivered_message_id_str, action = payload.rsplit("_", 1)
        delivered_message_id = int(delivered_message_id_str)
    except ValueError:
        await callback.answer("❌ Некорректные данные.")
        return

    if action == "back":
        try:
            await callback_message.edit_reply_markup(reply_markup=build_sent_reminder_actions_keyboard())
        except TelegramBadRequest:
            pass
        await callback.answer()
        return

    scheduled_msg = get_scheduled_message_by_delivered_message_id(
        user_id_from_callback(callback), delivered_message_id
    )
    if not scheduled_msg:
        await callback.answer("Напоминание уже перенесено.")
        return

    db_id, source_message_id, _, _, preview, source, _ = scheduled_msg
    user_id = user_id_from_callback(callback)
    now = user_now(user_id)
    hours = day_part_hours(user_id)

    if action == "custom":
        # Keep the reminder itself intact (it may be media) and ask in a separate message.
        if callback_message.message_id == delivered_message_id:
            try:
                await callback_message.edit_reply_markup(reply_markup=build_sent_reminder_actions_keyboard())
            except TelegramBadRequest:
                pass
            prompt = await callback_message.answer("Выбери дату:", reply_markup=build_manual_date_keyboard())
        else:  # legacy separate «Когда напомнить снова?» message
            prompt = callback_message
            await prompt.edit_text("Выбери дату:", reply_markup=build_manual_date_keyboard())
        await state.clear()
        await state.update_data(
            scheduled_db_id=db_id,
            source_message_id=source_message_id,
            preview=preview or "",
            source=source or "",
            prompt_msg_id=prompt.message_id,
            target_message_id=delivered_message_id,
        )
        await push_screen(state, "manual_date")
        await callback.answer()
        return

    scheduled_time, label = get_quick_scheduled_time(action, now, hours=hours)
    if scheduled_time is None:
        await callback.answer("❌ Ошибка при планировании времени.")
        return

    try:
        new_db_id = replace_sent_reminder_with_pending(
            user_id_from_callback(callback),
            db_id,
            source_message_id,
            serialize_datetime(scheduled_time),
            preview or "",
            source or "",
        )
    except Exception:
        logger.exception("replace_sent_reminder_with_pending failed in callback")
        await callback.answer(USER_FACING_ERROR, show_alert=True)
        return

    for message_id in {delivered_message_id, callback_message.message_id}:
        try:
            await bot.delete_message(user_id_from_callback(callback), message_id)
        except TelegramAPIError:
            pass

    await callback.answer()
    kb = reminders_ux.build_reschedule_note_keyboard(new_db_id)
    await bot.send_message(user_id_from_callback(callback), f"✅ Отложил {label}.", reply_markup=kb)


@dp.callback_query(F.data == "sent_delete")
async def delete_sent_reminder(callback: CallbackQuery):
    callback_message = get_callback_message(callback)
    if callback_message is None:
        await callback.answer("❌ Сообщение недоступно.")
        return

    scheduled_msg = get_scheduled_message_by_delivered_message_id(
        user_id_from_callback(callback), callback_message.message_id
    )
    if not scheduled_msg:
        await callback.answer("❌ Напоминание уже обработано.", show_alert=True)
        return

    delete_message(user_id_from_callback(callback), scheduled_msg[0])
    try:
        await callback_message.delete()
    except TelegramAPIError:
        pass
    await callback.answer("Удалено!")


_SNOOZE_HOURS = {"snooze_1h": 1, "snooze_3h": 3}  # snooze_1h — legacy button on old messages


@dp.callback_query(F.data.in_({"snooze_1h", "snooze_3h", "snooze_evening", "snooze_tomorrow_morning"}))
async def handle_sent_snooze(callback: CallbackQuery):
    callback_message = get_callback_message(callback)
    if callback_message is None:
        await callback.answer("❌ Сообщение недоступно.")
        return

    scheduled_msg = get_scheduled_message_by_delivered_message_id(
        user_id_from_callback(callback), callback_message.message_id
    )
    if not scheduled_msg:
        await callback.answer("Напоминание уже перенесено.")
        return

    db_id, source_message_id, delivered_message_id, _, preview, source, _ = scheduled_msg
    user_id = user_id_from_callback(callback)
    now = user_now(user_id)
    hours = day_part_hours(user_id)

    action = callback.data
    if action in _SNOOZE_HOURS:
        scheduled_time = (now + timedelta(hours=_SNOOZE_HOURS[action])).replace(second=0, microsecond=0)
    elif action == "snooze_evening":
        scheduled_time = now.replace(hour=hours["evening"], minute=0, second=0, microsecond=0)
        if scheduled_time <= now:
            scheduled_time += timedelta(days=1)
    elif action == "snooze_tomorrow_morning":
        scheduled_time = (now + timedelta(days=1)).replace(hour=hours["morning"], minute=0, second=0, microsecond=0)
    else:
        await callback.answer("❌ Некорректное действие.")
        return

    try:
        new_db_id = replace_sent_reminder_with_pending(
            user_id_from_callback(callback),
            db_id,
            source_message_id,
            serialize_datetime(scheduled_time),
            preview or "",
            source or "",
        )
    except Exception:
        logger.exception("replace_sent_reminder_with_pending failed in snooze callback")
        await callback.answer(USER_FACING_ERROR, show_alert=True)
        return

    for message_id in {delivered_message_id, callback_message.message_id}:
        try:
            await bot.delete_message(user_id_from_callback(callback), message_id)
        except TelegramAPIError:
            pass

    await callback.answer()
    kb = reminders_ux.build_reschedule_note_keyboard(new_db_id)
    await bot.send_message(
        user_id_from_callback(callback),
        f"✅ Отложил на {format_when(scheduled_time, now)}.",
        reply_markup=kb,
    )


@dp.callback_query(F.data.in_({"sent_done", "snooze_done"}))
async def handle_sent_reminder_done(callback: CallbackQuery):
    callback_message = get_callback_message(callback)
    if callback_message is None:
        await callback.answer("❌ Сообщение недоступно.")
        return

    scheduled_msg = get_scheduled_message_by_delivered_message_id(
        user_id_from_callback(callback), callback_message.message_id
    )
    if scheduled_msg:
        db_id = scheduled_msg[0]
        mark_reminder_as_done(user_id_from_callback(callback), db_id)

    try:
        await callback_message.edit_reply_markup(reply_markup=None)
    except (TelegramBadRequest, TelegramAPIError):
        pass

    await callback.answer("Готово")


EMOJI_NUMS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"]
REMINDERS_PER_PAGE = 5


def format_reminder_when(dt_obj: datetime, now: datetime | None = None) -> str:
    """Human-friendly relative time, e.g. 'через 2 ч 30 мин', 'просрочено'."""
    now = now or local_now()
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


def _reminder_tag(row_tag: str | None) -> str:
    return row_tag or "Без тега"


async def render_rem_tags_screen(chat_id: int, target: types.Message | types.CallbackQuery, state: FSMContext):
    await state.set_state(None)
    reminders = get_user_messages(chat_id)
    if not reminders:
        text = "📭 У тебя нет активных напоминаний."
        if isinstance(target, CallbackQuery):
            await target.message.edit_text(text, parse_mode="HTML")
            await target.answer()
        else:
            await target.answer(text, parse_mode="HTML")
        return

    grouped: dict[str, list] = {}
    for row in reminders:
        grouped.setdefault(_reminder_tag(row[4]), []).append(row)

    sorted_tags = sorted(grouped.keys(), key=lambda t: t.lower())
    await state.update_data(rem_tags_list=sorted_tags)

    text = (
        f"📂 <b>Напоминания по тегам</b> (всего: {len(reminders)})\n\n"
        "Выбери тег для фильтра или покажи все:"
    )

    buttons = []
    row = []
    for idx, tag in enumerate(sorted_tags):
        count = len(grouped[tag])
        display_tag = tag[:15] + "..." if len(tag) > 18 else tag
        row.append(InlineKeyboardButton(text=f"🏷 {display_tag} ({count})", callback_data=f"rem_tag_idx_{idx}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    buttons.append([InlineKeyboardButton(text="📅 Все напоминания", callback_data="rem_tag_all")])
    buttons.append([InlineKeyboardButton(text="🏠 В главное меню", callback_data="flow_home")])

    markup = InlineKeyboardMarkup(inline_keyboard=buttons)
    if isinstance(target, CallbackQuery):
        await target.message.edit_text(text, parse_mode="HTML", reply_markup=markup)
        await target.answer()
    else:
        await target.answer(text, parse_mode="HTML", reply_markup=markup)


async def render_rem_list_screen(chat_id: int, tag: str | None, page: int, target: types.Message | types.CallbackQuery, state: FSMContext):
    await state.set_state(None)
    await state.update_data(rem_current_tag=tag, rem_current_page=page)

    reminders = get_user_messages(chat_id)  # ordered by send_at
    if tag:
        filtered = [r for r in reminders if _reminder_tag(r[4]) == tag]
    else:
        filtered = reminders

    if not filtered:
        text = "📭 В этом фильтре нет напоминаний."
        markup = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="flow_back")]]
        )
        if isinstance(target, CallbackQuery):
            await target.message.edit_text(text, parse_mode="HTML", reply_markup=markup)
            await target.answer()
        else:
            await target.answer(text, parse_mode="HTML", reply_markup=markup)
        return

    total_items = len(filtered)
    total_pages = (total_items + REMINDERS_PER_PAGE - 1) // REMINDERS_PER_PAGE
    page = max(1, min(page, total_pages))
    start_idx = (page - 1) * REMINDERS_PER_PAGE
    page_items = filtered[start_idx:start_idx + REMINDERS_PER_PAGE]

    await state.update_data(
        rem_page_ids=[r[0] for r in page_items],
        rem_current_tag=tag,
        rem_current_page=page,
    )

    header = f"🏷 #{tag}" if tag else "⏳ Все напоминания"
    now = user_now(chat_id)
    text = reminders_ux.render_reminder_list_text(header, page, total_pages, page_items, now)
    markup = reminders_ux.build_reminder_list_keyboard(page_items, page, total_pages, tag, now)

    if isinstance(target, CallbackQuery):
        await target.message.edit_text(text, parse_mode="HTML", reply_markup=markup)
        await target.answer()
    else:
        await target.answer(text, parse_mode="HTML", reply_markup=markup)


async def render_rem_detail_screen(chat_id: int, msg_id: int, target: types.Message | types.CallbackQuery, state: FSMContext):
    await state.set_state(None)
    await state.update_data(rem_detail_id=msg_id)

    row = get_reminder_by_id(chat_id, msg_id)
    if not row:
        text = "❌ Напоминание не найдено."
        markup = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="flow_back")]]
        )
        if isinstance(target, CallbackQuery):
            await target.message.edit_text(text, parse_mode="HTML", reply_markup=markup)
            await target.answer()
        else:
            await target.answer(text, parse_mode="HTML", reply_markup=markup)
        return

    now = user_now(chat_id)
    text = reminders_ux.render_reminder_detail_text(row, now)
    message_id = row["message_id"] if "message_id" in row.keys() else row[5]
    has_message_id = bool(message_id and message_id > 0)
    source_url = row["source_url"] if "source_url" in row.keys() else (row[8] if len(row) > 8 else None)
    markup = reminders_ux.build_reminder_detail_keyboard(msg_id, has_message_id, source_url=source_url)

    if isinstance(target, CallbackQuery):
        await target.message.edit_text(text, parse_mode="HTML", reply_markup=markup)
        await target.answer()
    else:
        await target.answer(text, parse_mode="HTML", reply_markup=markup)


reminders_ux.configure(bot=bot, render_detail=render_rem_detail_screen)


async def render_rem_move_screen(chat_id: int, msg_id: int, target: types.Message | types.CallbackQuery, state: FSMContext):
    await state.set_state(SaveState.waiting_for_reminder_tag)

    row = get_reminder_by_id(chat_id, msg_id)
    if not row:
        text = "❌ Напоминание не найдено."
        markup = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="flow_back")]]
        )
        if isinstance(target, CallbackQuery):
            await target.message.edit_text(text, parse_mode="HTML", reply_markup=markup)
            await target.answer()
        else:
            await target.answer(text, parse_mode="HTML", reply_markup=markup)
        return

    tag_safe = html.escape(row[4]) if row[4] else "Без тега"
    text = (
        f"🏷 <b>Изменение тега</b>\n\n"
        f"Текущий тег: <b>#{tag_safe}</b>\n\n"
        f"Выбери существующий тег ниже или пришли новый текстом:"
    )

    tags = get_user_tags(chat_id)
    await state.update_data(rem_move_tags=tags, rem_detail_id=msg_id)

    buttons = []
    row_btns = []
    for idx, t in enumerate(tags):
        display_t = t[:15] + "..." if len(t) > 18 else t
        row_btns.append(InlineKeyboardButton(text=f"📁 {display_t}", callback_data=f"rem_mv_idx_{idx}"))
        if len(row_btns) == 2:
            buttons.append(row_btns)
            row_btns = []
    if row_btns:
        buttons.append(row_btns)

    buttons.append([
        InlineKeyboardButton(text="🔙 Назад", callback_data="flow_back"),
        InlineKeyboardButton(text="🏠 В начало", callback_data="flow_home"),
    ])
    markup = InlineKeyboardMarkup(inline_keyboard=buttons)
    if isinstance(target, CallbackQuery):
        await target.message.edit_text(text, parse_mode="HTML", reply_markup=markup)
        await target.answer()
    else:
        await target.answer(text, parse_mode="HTML", reply_markup=markup)


@dp.message(Command("list"))
async def cmd_list(message: types.Message, state: FSMContext):
    await state.clear()
    if not get_user_messages(user_id_from_message(message)):
        await message.answer("📭 У тебя нет активных напоминаний.\n\nДля дайджестов используй /list_digest.")
        return
    await push_screen(state, "rem_list", tag=None, page=1)
    await render_rem_list_screen(user_id_from_message(message), None, 1, message, state)


@dp.message(Command("list_digest"))
async def cmd_list_digest(message: types.Message, state: FSMContext):
    await state.clear()
    user_id = user_id_from_message(message)
    await message.answer(
        render_digest_settings_text(user_id),
        parse_mode="HTML",
        reply_markup=build_digest_settings_keyboard(get_user_subscriptions(user_id)),
    )


@dp.callback_query(F.data == "rem_ignore")
async def handle_rem_ignore(callback: CallbackQuery):
    await callback.answer()


@dp.callback_query(F.data == "rem_view_tags")
async def handle_rem_view_tags(callback: CallbackQuery, state: FSMContext):
    await push_screen(state, "rem_tags")
    await render_rem_tags_screen(user_id_from_callback(callback), callback, state)


@dp.callback_query(F.data == "rem_tag_all")
async def handle_rem_tag_all(callback: CallbackQuery, state: FSMContext):
    await push_screen(state, "rem_list", tag=None, page=1)
    await render_rem_list_screen(user_id_from_callback(callback), None, 1, callback, state)


@dp.callback_query(F.data.startswith("rem_tag_idx_"))
async def handle_rem_tag_selection(callback: CallbackQuery, state: FSMContext):
    idx = int(callback.data.split("_")[-1])
    data = await state.get_data()
    tags_list = data.get("rem_tags_list", [])
    if idx < len(tags_list):
        tag = tags_list[idx]
        await push_screen(state, "rem_list", tag=tag, page=1)
        await render_rem_list_screen(user_id_from_callback(callback), tag, 1, callback, state)
    else:
        await callback.answer("❌ Ошибка выбора тега.")


@dp.callback_query(F.data == "rem_page_prev")
async def handle_rem_page_prev(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    tag = data.get("rem_current_tag")
    page = max(1, (data.get("rem_current_page") or 1) - 1)
    await render_rem_list_screen(user_id_from_callback(callback), tag, page, callback, state)


@dp.callback_query(F.data == "rem_page_next")
async def handle_rem_page_next(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    tag = data.get("rem_current_tag")
    page = (data.get("rem_current_page") or 1) + 1
    await render_rem_list_screen(user_id_from_callback(callback), tag, page, callback, state)


@dp.callback_query(F.data.startswith("rem_sel_"))
async def handle_rem_select(callback: CallbackQuery, state: FSMContext):
    data = callback.data or ""
    if data.startswith("rem_sel_idx_"):
        idx = int(data.split("_")[-1])
        st_data = await state.get_data()
        page_ids = st_data.get("rem_page_ids", [])
        if idx >= len(page_ids):
            await callback.answer("❌ Напоминание недоступно.")
            return
        msg_id = page_ids[idx]
    else:
        msg_id = int(data.split("_")[-1])
    await push_screen(state, "rem_detail", msg_id=msg_id)
    await render_rem_detail_screen(user_id_from_callback(callback), msg_id, callback, state)


@dp.callback_query(F.data.startswith("rem_del_"))
async def handle_rem_delete(callback: CallbackQuery, state: FSMContext):
    msg_id = int(callback.data.split("_")[-1])
    delete_message(user_id_from_callback(callback), msg_id)
    await callback.answer("✅ Напоминание удалено!")
    await pop_screen(state)
    prev = (await state.get_data()).get("screen_history", [])
    if prev:
        await render_screen(user_id_from_callback(callback), prev[-1], callback, state)
    else:
        await render_rem_list_screen(user_id_from_callback(callback), None, 1, callback, state)


@dp.callback_query(F.data.startswith("rem_tag_init_"))
async def handle_rem_tag_init(callback: CallbackQuery, state: FSMContext):
    msg_id = int(callback.data.split("_")[-1])
    await push_screen(state, "rem_move", msg_id=msg_id)
    await render_rem_move_screen(user_id_from_callback(callback), msg_id, callback, state)


@dp.callback_query(F.data.startswith("rem_mv_idx_"))
async def handle_rem_move_tag_selection(callback: CallbackQuery, state: FSMContext):
    idx = int(callback.data.split("_")[-1])
    data = await state.get_data()
    tags = data.get("rem_move_tags", [])
    msg_id = data.get("rem_detail_id")
    if idx >= len(tags) or not msg_id:
        await callback.answer("❌ Ошибка выбора тега.")
        return
    update_reminder_tag(user_id_from_callback(callback), msg_id, tags[idx])
    await callback.answer(f"✅ Тег: #{tags[idx]}")
    await pop_screen(state)  # leave rem_move
    await render_rem_detail_screen(user_id_from_callback(callback), msg_id, callback, state)


@dp.message(SaveState.waiting_for_reminder_tag)
async def process_reminder_tag(message: types.Message, state: FSMContext):
    new_tag = (message.text or "").strip().lstrip("#").strip()
    if not new_tag:
        await message.answer("❌ Тег не должен быть пустым.", reply_markup=build_back_home_keyboard())
        return

    data = await state.get_data()
    msg_id = data.get("rem_detail_id")
    if not msg_id:
        await message.answer("❌ Ошибка: напоминание не найдено.", reply_markup=build_back_home_keyboard())
        return

    update_reminder_tag(user_id_from_message(message), msg_id, new_tag)
    await pop_screen(state)  # leave rem_move
    confirm = await message.answer(f"✅ Тег: #{new_tag}")
    await state.set_state(None)
    await asyncio.sleep(2)
    try:
        await confirm.delete()
        await message.delete()
    except TelegramAPIError:
        pass
    await render_rem_detail_screen(user_id_from_message(message), msg_id, message, state)


@dp.callback_query(F.data.startswith("rem_tobk_"))
async def handle_rem_to_bookmark(callback: CallbackQuery, state: FSMContext):
    msg_id = int(callback.data.split("_")[-1])
    row = get_reminder_by_id(user_id_from_callback(callback), msg_id)
    if not row:
        await callback.answer("❌ Напоминание не найдено.", show_alert=True)
        return
    _id, _send_at, preview, source, row_tag, _msg_id = row
    try:
        add_saved_message(
            user_id_from_callback(callback),
            preview or "Без текста",
            source or "Неизвестно",
            row_tag or "Без тега",
            serialize_datetime(utc_now()),
        )
    except ValueError as exc:
        await callback.answer(f"🚫 {exc}", show_alert=True)
        return
    delete_message(user_id_from_callback(callback), msg_id)
    await callback.answer("✅ Перенесено в закладки!")
    await pop_screen(state)  # leave rem_detail
    prev = (await state.get_data()).get("screen_history", [])
    if prev:
        await render_screen(user_id_from_callback(callback), prev[-1], callback, state)
    else:
        await render_rem_list_screen(user_id_from_callback(callback), None, 1, callback, state)


@dp.callback_query(F.data.startswith("rem_resched_init_"))
async def handle_rem_resched_init(callback: CallbackQuery, state: FSMContext):
    msg_id = int(callback.data.rsplit("_", 1)[-1])
    row = get_reminder_by_id(user_id_from_callback(callback), msg_id)
    if not row:
        await callback.answer("❌ Напоминание не найдено.", show_alert=True)
        return
    await state.update_data(
        rem_reschedule_id=msg_id,
        preview=row[2] or "",
        source=row[3] or "",
        prompt_msg_id=callback.message.message_id,
    )
    user_id = user_id_from_callback(callback)
    now = user_now(user_id)
    hours = day_part_hours(user_id)
    await callback.message.edit_text(
        f"⏰ <b>Перенос времени</b>\n\nВыбери новое время напоминания:\n{describe_quick_times(now, hours)}",
        parse_mode="HTML",
        reply_markup=build_time_selection_keyboard(f"remresched_{msg_id}"),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("remresched_"))
async def handle_rem_resched_choice(callback: CallbackQuery, state: FSMContext):
    payload = parse_callback_strip_prefix(callback.data, "remresched_")
    if payload is None:
        await callback.answer("❌ Некорректные данные.")
        return
    try:
        msg_id_str, action = payload.rsplit("_", 1)
        msg_id = int(msg_id_str)
    except ValueError:
        await callback.answer("❌ Некорректные данные.")
        return

    if action == "custom":
        await state.update_data(rem_reschedule_id=msg_id, prompt_msg_id=callback.message.message_id)
        await push_screen(state, "manual_date")
        await callback.message.edit_text("Выбери дату:", reply_markup=build_manual_date_keyboard())
        await callback.answer()
        return

    user_id = user_id_from_callback(callback)
    now = user_now(user_id)
    hours = day_part_hours(user_id)
    scheduled_time, label = get_quick_scheduled_time(action, now, hours=hours)
    if scheduled_time is None:
        await callback.answer("❌ Ошибка при планировании времени.")
        return
    update_reminder_time(user_id_from_callback(callback), msg_id, serialize_datetime(scheduled_time))
    await callback.answer(f"✅ Перенесено на {scheduled_time.strftime('%d.%m %H:%M')}")
    # rem_detail is already the top of history (the time picker replaced the message
    # in place without pushing a screen), so just re-render it.
    await render_rem_detail_screen(user_id_from_callback(callback), msg_id, callback, state)


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

    user_id = user_id_from_callback(callback)
    # SEC1 (п.1): подписка должна принадлежать нажавшему, иначе ничего не меняем.
    if not get_subscription_by_id(user_id, sub_id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    delete_subscription(user_id, sub_id)
    try:
        await callback_message.edit_text(
            render_digest_settings_text(user_id),
            parse_mode="HTML",
            reply_markup=build_digest_settings_keyboard(get_user_subscriptions(user_id)),
        )
    except TelegramAPIError:
        pass
    await callback.answer("Удалено!")





@dp.callback_query(F.data.startswith("alert_retry_"))
async def handle_alert_retry(callback: CallbackQuery):
    suffix = callback.data[len("alert_retry_"):]
    try:
        execution_id = int(suffix)
    except ValueError:
        await callback.answer("❌ Некорректный ID выпуска.", show_alert=True)
        return

    # SEC1 (п.1): чужой/несуществующий выпуск не даёт доступа и не меняет состояние.
    callback_user_id = user_id_from_callback(callback)
    execution = get_digest_execution_by_id(execution_id)
    if not execution or execution["user_id"] != callback_user_id:
        await callback.answer("Нет доступа", show_alert=True)
        return

    success = retry_digest_execution(execution_id, callback_user_id)
    if success:
        await callback.answer("🔄 Повтор запущен! Бот попытается собрать выпуск в ближайшем цикле.", show_alert=True)
        callback_msg = get_callback_message(callback)
        if callback_msg:
            try:
                current_text = callback_msg.text or callback_msg.caption or ""
                note = f"\n\n<i>🔄 Повтор запущен пользователем ({utc_now().astimezone(TZ).strftime('%H:%M')}).</i>"
                await callback_msg.edit_text(
                    current_text + note,
                    parse_mode="HTML",
                    reply_markup=build_digest_alert_keyboard(execution_id=None),
                )
            except TelegramAPIError:
                pass
    else:
        await callback.answer("❌ Запись о выпуске не найдена.", show_alert=True)


@dp.callback_query(F.data == "alert_pause_24h")
async def handle_alert_pause_24h(callback: CallbackQuery):
    user_id = user_id_from_callback(callback)
    paused_until = utc_now() + timedelta(hours=24)
    set_user_alerts_paused_until(user_id, serialize_datetime(paused_until))
    await callback.answer("⏸ Технические алерты на паузе на 24 часа.")
    callback_msg = get_callback_message(callback)
    if callback_msg:
        try:
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(text="▶️ Снять паузу", callback_data="alert_toggle_enable"),
                        InlineKeyboardButton(text="⚙️ Настройки", callback_data="ds"),
                    ]
                ]
            )
            await callback_msg.edit_text(
                "⏸ <b>Технические алерты поставлены на паузу на 24 часа.</b>\n\n"
                "Сами дайджесты и автоматические повторы продолжают формироваться по расписанию.",
                parse_mode="HTML",
                reply_markup=keyboard,
            )
        except TelegramAPIError:
            pass


@dp.callback_query(F.data == "alert_pause_7d")
async def handle_alert_pause_7d(callback: CallbackQuery):
    user_id = user_id_from_callback(callback)
    paused_until = utc_now() + timedelta(days=7)
    set_user_alerts_paused_until(user_id, serialize_datetime(paused_until))
    await callback.answer("⏸ Технические алерты на паузе на 7 дней.")
    callback_msg = get_callback_message(callback)
    if callback_msg:
        try:
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(text="▶️ Снять паузу", callback_data="alert_toggle_enable"),
                        InlineKeyboardButton(text="⚙️ Настройки", callback_data="ds"),
                    ]
                ]
            )
            await callback_msg.edit_text(
                "⏸ <b>Технические алерты поставлены на паузу на 7 дней.</b>\n\n"
                "Сами дайджесты и автоматические повторы продолжают формироваться по расписанию.",
                parse_mode="HTML",
                reply_markup=keyboard,
            )
        except TelegramAPIError:
            pass


@dp.callback_query(F.data == "alert_disable")
async def handle_alert_disable(callback: CallbackQuery):
    user_id = user_id_from_callback(callback)
    set_user_alerts_enabled(user_id, False)
    await callback.answer("🔕 Технические алерты отключены.")
    callback_msg = get_callback_message(callback)
    if callback_msg:
        try:
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(text="🔔 Включить тех. алерты", callback_data="alert_toggle_enable"),
                        InlineKeyboardButton(text="⚙️ Настройки", callback_data="ds"),
                    ]
                ]
            )
            await callback_msg.edit_text(
                "🔕 <b>Технические алерты отключены.</b>\n\n"
                "Дайджесты продолжают формироваться по обычному графику. Вы можете включить алерты в любой момент в настройках.",
                parse_mode="HTML",
                reply_markup=keyboard,
            )
        except TelegramAPIError:
            pass


@dp.callback_query(F.data == "alert_toggle_enable")
async def handle_alert_toggle_enable(callback: CallbackQuery):
    user_id = user_id_from_callback(callback)
    set_user_alerts_enabled(user_id, True)
    set_user_alerts_paused_until(user_id, None)
    await callback.answer("🔔 Технические алерты включены!")
    await _refresh_digest_settings(callback)





# ---------------------------------------------------------------------------
# Проверка подписок (Feature 2)
# ---------------------------------------------------------------------------


@dp.message(Command("check"))
async def cmd_check(message: types.Message, state: FSMContext):
    await state.clear()
    user_id = user_id_from_message(message)
    # SEC1 (п.3): /check обходит все подписки и ходит в сеть — общий кулдаун.
    wait_seconds = check_expensive_command_cooldown(user_id)
    if wait_seconds:
        await message.answer(f"⏳ Подожди {wait_seconds} с.")
        return
    subs = get_user_subscriptions(user_id)
    if not subs:
        await message.answer("📭 У тебя нет подписок для проверки.")
        return

    status_msg = await message.answer("🔎 Проверяю доступность каналов...")
    semaphore = channel_source.fetch_semaphore
    old_marker = serialize_datetime(utc_now() - timedelta(days=3650))

    async with create_telegram_http_session() as session:
        async def check_one(sub):
            sub_id, username, title = sub[0], sub[1], sub[2]
            async with semaphore:
                try:
                    await fetch_channel_posts(username, old_marker, session)
                    return sub_id, username, title, True, None
                except ChannelFetchError as exc:
                    return sub_id, username, title, False, str(exc)

        results = await asyncio.gather(*(check_one(sub) for sub in subs))

    ok = sum(1 for r in results if r[3])
    lines = ["🔎 <b>Проверка подписок</b>", f"Доступно: {ok}/{len(results)}", ""]
    for sub_id, username, title, success, err in results:
        name = html.escape(title or username or "Канал")
        if success:
            clear_subscription_failure(user_id, sub_id)
            lines.append(f"✅ {name}")
        else:
            lines.append(f"⚠️ {name} — {html.escape((err or '')[:80])}")

    try:
        await status_msg.edit_text("\n".join(lines), parse_mode="HTML")
    except TelegramAPIError:
        await message.answer("\n".join(lines), parse_mode="HTML")


# ---------------------------------------------------------------------------
# Анализ каналов через DeepSeek (Feature 9)
# ---------------------------------------------------------------------------


def _ai_today_key() -> str:
    return ai_today_key()


def _ai_limit_reached(user_id: int) -> bool:
    if AI_DAILY_LIMIT <= 0:
        return False
    return get_ai_usage_today(user_id, _ai_today_key()) >= AI_DAILY_LIMIT


def _compute_channel_stats(posts: list[dict]) -> dict:
    count = len(posts)
    total_words = sum(len((p.get("text") or "").split()) for p in posts)
    times = sorted(p["time"] for p in posts if p.get("time"))
    if len(times) >= 2:
        span_days = max((times[-1] - times[0]).total_seconds() / 86400, 0.5)
    else:
        span_days = 1.0
    posts_per_day = count / span_days
    avg_words = (total_words / count) if count else 0
    daily_words = posts_per_day * avg_words

    def mins(words: float) -> int:
        return max(1, round(words / READING_WORDS_PER_MINUTE)) if words else 0

    return {
        "count": count,
        "posts_per_day": posts_per_day,
        "avg_words": avg_words,
        "read_daily_min": mins(daily_words),
        "read_weekly_min": mins(daily_words * 7),
        "read_monthly_min": mins(daily_words * 30),
    }


def _recommend_period(posts_per_day: float) -> tuple[str, str]:
    if posts_per_day >= 5:
        return (
            "ежедневный (в отдельное время)",
            "Канал очень активный — лучше отдельный ежедневный дайджест в своё время, "
            "иначе общая утренняя сводка будет слишком большой.",
        )
    if posts_per_day >= 1:
        return (
            "ежедневный или еженедельный",
            "Канал умеренно активный — подойдёт ежедневный или еженедельный дайджест в общей сводке.",
        )
    return (
        "еженедельный или ежемесячный",
        "Канал постит редко — достаточно еженедельного или ежемесячного дайджеста.",
    )


def build_ai_channels_keyboard(channels) -> InlineKeyboardMarkup:
    rows = []
    for idx, (username, title) in enumerate(channels):
        display = (title or username or "Канал")[:24]
        rows.append([InlineKeyboardButton(text=f"📰 {display}", callback_data=f"aich_{idx}")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="digest_settings")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_ai_channel_menu(idx: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🧮 Подстройка дайджеста", callback_data=f"aiadj_{idx}")],
            [InlineKeyboardButton(text="📝 Саммари по каналу", callback_data=f"aisum_{idx}")],
            [InlineKeyboardButton(text="⬅️ К списку каналов", callback_data="digest_ai")],
        ]
    )


def build_ai_back_keyboard(idx: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"aich_{idx}")],
            [InlineKeyboardButton(text="⚙️ Все подписки", callback_data="digest_settings")],
        ]
    )


async def _resolve_ai_channel(callback: CallbackQuery, state: FSMContext, prefix: str):
    idx = parse_callback_int_suffix(callback.data, prefix)
    data = await state.get_data()
    channels = data.get("ai_channels", [])
    if idx is None or idx >= len(channels):
        await callback.answer("❌ Канал не найден. Открой меню анализа заново.", show_alert=True)
        return None, None, None
    username, title = channels[idx]
    return idx, username, title


@dp.callback_query(F.data == "digest_ai")
async def open_ai_menu(callback: CallbackQuery, state: FSMContext):
    user_id = user_id_from_callback(callback)
    subs = get_user_subscriptions(user_id)
    if not subs:
        await callback.answer("Сначала подпишись хотя бы на один канал.", show_alert=True)
        return
    channels = [(s[1], s[2]) for s in subs]
    await state.update_data(ai_channels=channels)
    note = (
        ""
        if ai_assistant.is_enabled()
        else "\n\n⚠️ ИИ-саммари недоступно (не задан DEEPSEEK_API_KEY). Подстройка дайджеста работает и без ИИ."
    )
    text = "🧠 <b>Анализ каналов</b>\n\nВыбери канал — посчитаю частоту постинга, время чтения и дам рекомендацию по дайджесту." + note
    await edit_or_answer(callback, text, reply_markup=build_ai_channels_keyboard(channels))
    await callback.answer()


@dp.callback_query(F.data.startswith("aich_"))
async def open_ai_channel(callback: CallbackQuery, state: FSMContext):
    # SEC1 (п.3): ИИ-меню и анализ — дорогие операции, общий per-user кулдаун.
    wait_seconds = check_expensive_command_cooldown(user_id_from_callback(callback))
    if wait_seconds:
        await callback.answer(f"⏳ Подожди {wait_seconds} с.", show_alert=True)
        return
    idx, username, title = await _resolve_ai_channel(callback, state, "aich_")
    if username is None:
        return
    limit_note = ""
    if ai_assistant.is_enabled() and AI_DAILY_LIMIT > 0:
        used = get_ai_usage_today(user_id_from_callback(callback), _ai_today_key())
        limit_note = f"\n\n🤖 ИИ-запросов сегодня: {used}/{AI_DAILY_LIMIT}"
    text = f"📊 <b>{html.escape(title or username)}</b>\n\nЧто сделать с этим каналом?{limit_note}"
    await edit_or_answer(callback, text, reply_markup=build_ai_channel_menu(idx))
    await callback.answer()


@dp.callback_query(F.data.startswith("aiadj_"))
async def ai_adjust_digest(callback: CallbackQuery, state: FSMContext):
    wait_seconds = check_expensive_command_cooldown(user_id_from_callback(callback))
    if wait_seconds:
        await callback.answer(f"⏳ Подожди {wait_seconds} с.", show_alert=True)
        return
    idx, username, title = await _resolve_ai_channel(callback, state, "aiadj_")
    if username is None:
        return
    user_id = user_id_from_callback(callback)
    await callback.answer("⏳ Анализирую канал...")

    old_marker = serialize_datetime(utc_now() - timedelta(days=3650))
    try:
        async with create_telegram_http_session() as session:
            posts = await fetch_channel_posts(username, old_marker, session)
    except ChannelFetchError as exc:
        await edit_or_answer(
            callback,
            f"⚠️ Не удалось прочитать канал: {html.escape(str(exc))}",
            reply_markup=build_ai_back_keyboard(idx),
        )
        return

    if not posts:
        await edit_or_answer(
            callback,
            "На странице канала нет постов для анализа.",
            reply_markup=build_ai_back_keyboard(idx),
        )
        return

    stats = _compute_channel_stats(posts)
    period_label, base_reco = _recommend_period(stats["posts_per_day"])
    lines = [
        "🧮 <b>Подстройка дайджеста</b>",
        f"📰 {html.escape(title or username)}",
        "",
        f"Частота: ~{stats['posts_per_day']:.1f} постов/день (по {stats['count']} последним)",
        f"Объём: ~{round(stats['avg_words'])} слов/пост",
        "",
        "⏱ Время чтения постов:",
        f"• за день: ~{stats['read_daily_min']} мин",
        f"• за неделю: ~{stats['read_weekly_min']} мин",
        f"• за месяц: ~{stats['read_monthly_min']} мин",
        "",
        f"💡 Рекомендуемая периодичность: <b>{period_label}</b>",
        base_reco,
    ]

    if ai_assistant.is_enabled() and not _ai_limit_reached(user_id):
        sample = "\n---\n".join((p.get("text") or "")[:200] for p in posts[:8])
        system_prompt = (
            "Ты помощник по настройке Telegram-дайджестов. На основе статистики канала и примеров постов "
            "дай короткую (2-3 предложения) рекомендацию на русском: какую периодичность дайджеста выбрать, "
            "стоит ли выносить канал в отдельное время или оставить в общей сводке. Будь конкретен, без воды."
        )
        user_prompt = (
            f"Канал: {title or username}\n"
            f"Постов в день: ~{stats['posts_per_day']:.1f}\n"
            f"Среднее слов в посте: ~{round(stats['avg_words'])}\n"
            f"Примеры постов:\n{sample}"
        )
        llm = await ai_assistant.ask(system_prompt, user_prompt, max_tokens=300)
        if llm:
            increment_ai_usage(user_id, _ai_today_key())
            lines += ["", f"🤖 <i>{html.escape(llm)}</i>"]

    await edit_or_answer(callback, "\n".join(lines), reply_markup=build_ai_back_keyboard(idx))


@dp.callback_query(F.data.startswith("aisum_"))
async def ai_summary(callback: CallbackQuery, state: FSMContext):
    wait_seconds = check_expensive_command_cooldown(user_id_from_callback(callback))
    if wait_seconds:
        await callback.answer(f"⏳ Подожди {wait_seconds} с.", show_alert=True)
        return
    idx, username, title = await _resolve_ai_channel(callback, state, "aisum_")
    if username is None:
        return
    user_id = user_id_from_callback(callback)

    if not ai_assistant.is_enabled():
        await callback.answer("🤖 ИИ не настроен (нет DEEPSEEK_API_KEY).", show_alert=True)
        return
    if _ai_limit_reached(user_id):
        await callback.answer(f"Лимит ИИ на сегодня исчерпан ({AI_DAILY_LIMIT}).", show_alert=True)
        return

    await callback.answer("⏳ Готовлю саммари...")
    since = serialize_datetime(utc_now() - timedelta(days=30))
    rows = get_digest_posts(user_id, since, channel_username=username)
    texts = [r["post_text"] for r in rows if r["post_text"]]

    if not texts:
        old_marker = serialize_datetime(utc_now() - timedelta(days=3650))
        try:
            async with create_telegram_http_session() as session:
                live = await fetch_channel_posts(username, old_marker, session)
            texts = [p["text"] for p in live if p.get("text")]
        except ChannelFetchError:
            texts = []

    if not texts:
        await edit_or_answer(
            callback,
            "Пока нет постов для саммари этого канала.",
            reply_markup=build_ai_back_keyboard(idx),
        )
        return

    context = "\n---\n".join(texts[:60])[:8000]
    system_prompt = (
        "Ты делаешь краткое саммари постов Telegram-канала на русском. Выдели 4-7 главных тем/новостей "
        "маркированным списком, кратко и по делу, без вступлений."
    )
    llm = await ai_assistant.ask(system_prompt, f"Канал: {title or username}\nПосты:\n{context}", max_tokens=800)
    if not llm:
        await edit_or_answer(
            callback,
            "🤖 Не удалось получить ответ ИИ. Попробуй позже.",
            reply_markup=build_ai_back_keyboard(idx),
        )
        return

    increment_ai_usage(user_id, _ai_today_key())
    await edit_or_answer(
        callback,
        f"📝 <b>Саммари: {html.escape(title or username)}</b>\n\n{html.escape(llm)}",
        reply_markup=build_ai_back_keyboard(idx),
    )


# ---------------------------------------------------------------------------
# Экспорт / импорт данных (Feature 8)
# ---------------------------------------------------------------------------


async def _send_export(user_id: int):
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
    await bot.send_document(
        user_id,
        BufferedInputFile(raw, filename=filename),
        caption=caption,
        parse_mode="HTML",
    )


@dp.message(Command("export"))
async def cmd_export(message: types.Message, state: FSMContext):
    await state.clear()
    await _send_export(user_id_from_message(message))


@dp.callback_query(F.data == "data_export")
async def cb_export(callback: CallbackQuery):
    await callback.answer("📤 Готовлю файл...")
    await _send_export(user_id_from_callback(callback))


async def _prompt_import(user_id: int, state: FSMContext):
    await state.set_state(ImportState.waiting_for_file)
    await bot.send_message(
        user_id,
        "📥 Пришли JSON-файл, который ты ранее получил через /export.\n\n"
        "Я добавлю подписки, закладки и будущие напоминания к текущим "
        "(дубликаты подписок пропущу).",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отмена", callback_data="import_cancel")]]
        ),
    )


@dp.message(Command("import"))
async def cmd_import(message: types.Message, state: FSMContext):
    await state.clear()
    await _prompt_import(user_id_from_message(message), state)


@dp.callback_query(F.data == "data_import")
async def cb_import(callback: CallbackQuery, state: FSMContext):
    await _prompt_import(user_id_from_callback(callback), state)
    await callback.answer()


@dp.callback_query(F.data == "import_cancel")
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


@dp.message(ImportState.waiting_for_file, F.document)
async def process_import_file(message: types.Message, state: FSMContext):
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
        await bot.download(document, destination=buffer)
        payload = json.loads(buffer.getvalue().decode("utf-8"))
    except Exception:
        logger.exception("Import: failed to read file")
        await message.answer("❌ Не удалось прочитать файл как JSON. Проверь, что это экспорт из /export.")
        return

    report = _import_user_payload(user_id_from_message(message), payload)
    await message.answer(report, parse_mode="HTML")


@dp.message(ImportState.waiting_for_file)
async def import_wrong_type(message: types.Message):
    await message.answer("📎 Пришли именно файл .json (как документ), либо нажми «Отмена».")


@dp.callback_query(F.data.startswith("bksched_init_"))
async def handle_bksched_init(callback: CallbackQuery, state: FSMContext):
    msg_id = int(callback.data.split("_")[-1])
    msg_data = get_saved_message_by_id(user_id_from_callback(callback), msg_id)
    if not msg_data:
        await callback.answer("❌ Закладка не найдена.")
        return
        
    full_text, source, tag, saved_at = msg_data
    preview = full_text.replace('\n', ' ')[:37] + '...' if len(full_text.replace('\n', ' ')) > 40 else full_text.replace('\n', ' ')
    await state.update_data(
        is_bookmark_scheduling=True,
        bookmark_msg_id=msg_id,
        bookmark_tag=tag,
        preview=preview,
        full_text=full_text,
        source=source,
        prompt_msg_id=callback.message.message_id,
    )

    markup = build_time_selection_keyboard(f"bksched_choice_{msg_id}")
    u_id = user_id_from_callback(callback)
    now = user_now(u_id)
    hours = day_part_hours(u_id)
    await callback.message.edit_text(
        f"⏰ <b>Перенос закладки в напоминание</b>\n\nВыбери время отправки напоминания:\n{describe_quick_times(now, hours)}",
        parse_mode="HTML",
        reply_markup=markup
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("bksched_choice_"))
async def handle_bksched_choice(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split("_")
    msg_id = int(parts[2])
    action = "_".join(parts[3:])
    
    callback_message = get_callback_message(callback)
    if callback_message is None:
        await callback.answer("❌ Сообщение недоступно.")
        return

    msg_data = get_saved_message_by_id(user_id_from_callback(callback), msg_id)
    if not msg_data:
        await callback.answer("❌ Закладка не найдена.")
        return
    full_text, source, tag, saved_at = msg_data
    preview = full_text.replace('\n', ' ')[:37] + '...' if len(full_text.replace('\n', ' ')) > 40 else full_text.replace('\n', ' ')
    
    if action == "custom":
        await state.update_data(
            is_bookmark_scheduling=True,
            bookmark_msg_id=msg_id,
            bookmark_tag=tag,
            preview=preview,
            full_text=full_text,
            source=source,
            prompt_msg_id=callback_message.message_id,
        )
        await push_screen(state, "manual_date")
        await callback_message.edit_text(
            "Выбери дату:",
            reply_markup=build_manual_date_keyboard(),
        )
        await callback.answer()
        return

    u_id = user_id_from_callback(callback)
    now = user_now(u_id)
    hours = day_part_hours(u_id)
    scheduled_time, label = get_quick_scheduled_time(action, now, hours=hours)
    if scheduled_time:
        # Move: create the reminder (keeping the tag) and drop the bookmark.
        try:
            new_id = add_message(
                user_id_from_callback(callback),
                None,
                serialize_datetime(scheduled_time),
                preview,
                source,
                tag,
                full_text=full_text,
            )
        except ValueError as exc:
            await callback.answer(f"🚫 {exc}", show_alert=True)
            return
        delete_saved_message(user_id_from_callback(callback), msg_id)
        reply_markup = reminders_ux.build_reschedule_note_keyboard(new_id) if new_id else None
        confirm_msg = await bot.send_message(
            user_id_from_callback(callback),
            f"✅ Перенесено в напоминания на {scheduled_time.strftime('%d.%m.%Y %H:%M')}!",
            reply_markup=reply_markup,
        )
        await state.clear()
        await push_screen(state, "rem_list", tag=None, page=1)

        if not reply_markup:
            await asyncio.sleep(2)
            try:
                await confirm_msg.delete()
            except TelegramAPIError:
                pass

        await render_screen(user_id_from_callback(callback), {"screen": "rem_list", "args": {"tag": None, "page": 1}}, callback_message, state)
    await callback.answer()


async def complete_manual_schedule(
    chat_id: int,
    state: FSMContext,
    scheduled_time: datetime,
    *,
    reply_target: Message,
    cleanup_message_id: int | None = None,
) -> None:
    if scheduled_time < user_now(chat_id):
        await reply_target.answer("Эта дата уже в прошлом! Попробуй еще раз.")
        return

    data = await state.get_data()
    msg_id = data.get("message_id")
    scheduled_db_id = data.get("scheduled_db_id")
    source_message_id = data.get("source_message_id")
    preview = data.get("preview", "")
    source = data.get("source", "")

    is_bksched = data.get("is_bookmark_scheduling")
    bookmark_msg_id = data.get("bookmark_msg_id")
    bookmark_tag = data.get("bookmark_tag")

    rem_reschedule_id = data.get("rem_reschedule_id")
    rem_current_tag = data.get("rem_current_tag")
    rem_current_page = data.get("rem_current_page")

    # Reschedule an existing reminder in place (from its detail screen).
    if rem_reschedule_id:
        changed = update_reminder_time(
            chat_id, rem_reschedule_id, serialize_datetime(scheduled_time)
        )
        if not changed:
            await reply_target.answer("❌ Напоминание больше не найдено.")
            await state.clear()
            return
        confirmation_text = f"✅ Перенесено на {scheduled_time.strftime('%d.%m.%Y в %H:%M')}."

        cleanup_ids = [cleanup_message_id, data.get("prompt_msg_id")]
        for message_id in {mid for mid in cleanup_ids if mid}:
            try:
                await bot.delete_message(chat_id, message_id)
            except TelegramAPIError:
                pass

        confirm_msg = await bot.send_message(chat_id, confirmation_text or "✅ Время перенесено.")
        await state.clear()
        await state.update_data(rem_current_tag=rem_current_tag, rem_current_page=rem_current_page)
        await push_screen(state, "rem_list", tag=rem_current_tag, page=rem_current_page or 1)
        await push_screen(state, "rem_detail", msg_id=rem_reschedule_id)
        await asyncio.sleep(2)
        try:
            await confirm_msg.delete()
        except TelegramAPIError:
            pass
        await render_screen(chat_id, {"screen": "rem_detail", "args": {"msg_id": rem_reschedule_id}}, reply_target, state)
        return

    full_text = data.get("full_text")
    source_url = data.get("source_url")
    new_db_id = None
    if msg_id:
        try:
            new_db_id = add_message(
                chat_id,
                msg_id,
                serialize_datetime(scheduled_time),
                preview,
                source,
                full_text=full_text,
                source_url=source_url,
            )
        except ValueError as exc:
            await reply_target.answer(f"🚫 {exc}")
            await state.clear()
            return
        confirmation_text = f"✅ Принято! Запланировано на {scheduled_time.strftime('%d.%m.%Y в %H:%M')}."
    elif is_bksched:
        try:
            new_db_id = add_message(
                chat_id,
                None,
                serialize_datetime(scheduled_time),
                preview,
                source,
                bookmark_tag,
                full_text=full_text,
                source_url=source_url,
            )
        except ValueError as exc:
            await reply_target.answer(f"🚫 {exc}")
            await state.clear()
            return
        if bookmark_msg_id:
            delete_saved_message(chat_id, bookmark_msg_id)
        confirmation_text = f"✅ Перенесено в напоминания на {scheduled_time.strftime('%d.%m.%Y в %H:%M')}!"
    elif scheduled_db_id:
        new_db_id = replace_sent_reminder_with_pending(
            chat_id,
            scheduled_db_id,
            source_message_id,
            serialize_datetime(scheduled_time),
            preview,
            source,
        )
        confirmation_text = f"✅ Отложил до {scheduled_time.strftime('%d.%m.%Y %H:%M')}."
    else:
        await reply_target.answer("Ошибка: не найдено исходное напоминание. Попробуй отправить его заново.")
        await state.clear()
        return

    cleanup_ids = [
        cleanup_message_id,
        data.get("prompt_msg_id"),
        data.get("target_message_id"),
        data.get("command_message_id"),
    ]
    seen_ids = set()
    for message_id in cleanup_ids:
        if not message_id or message_id in seen_ids:
            continue
        seen_ids.add(message_id)
        try:
            await bot.delete_message(chat_id, message_id)
        except TelegramAPIError:
            pass

    reply_markup = None
    if (scheduled_db_id or msg_id or is_bksched) and new_db_id:
        reply_markup = reminders_ux.build_reschedule_note_keyboard(new_db_id)
    confirm_msg = await bot.send_message(chat_id, confirmation_text, reply_markup=reply_markup)
    await state.clear()

    if is_bksched:
        # The bookmark was moved into reminders — land on the reminders list.
        await push_screen(state, "rem_list", tag=None, page=1)
        if not reply_markup:
            await asyncio.sleep(2)
            try:
                await confirm_msg.delete()
            except TelegramAPIError:
                pass
        await render_screen(chat_id, {"screen": "rem_list", "args": {"tag": None, "page": 1}}, reply_target, state)
        return

    if not (scheduled_db_id or ((msg_id or is_bksched) and reply_markup)):
        await asyncio.sleep(3)
        try:
            await confirm_msg.delete()
        except TelegramAPIError:
            pass


@dp.message(ScheduleState.waiting_for_datetime)
async def process_custom_datetime(message: types.Message, state: FSMContext):
    user_id = user_id_from_message(message)
    scheduled_time = parse_reminder_time(message.text or "", user_now(user_id), hours=day_part_hours(user_id))
    if scheduled_time is None:
        await message.answer(f"🤔 Не понял время. Примеры: {TIME_TEXT_EXAMPLES}, «15.03.2026 14:30».")
        return

    await complete_manual_schedule(
        user_id_from_message(message),
        state,
        scheduled_time,
        reply_target=message,
        cleanup_message_id=message.message_id,
    )


@dp.message(ScheduleState.waiting_for_time)
async def process_custom_time(message: types.Message, state: FSMContext):
    text = (message.text or "").strip()
    try:
        hour_text, minute_text = text.split(":", maxsplit=1)
        hour = int(hour_text)
        minute = int(minute_text)
    except (ValueError, AttributeError):
        await message.answer("❌ Неверный формат. Пример: 18:30")
        return

    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        await message.answer("❌ Время должно быть в формате ЧЧ:ММ.")
        return

    data = await state.get_data()
    scheduled_time = build_scheduled_time_from_state(data, hour=hour, minute=minute, tz=user_tz(user_id_from_message(message)))
    if scheduled_time is None:
        await message.answer("Ошибка: дата выбора утеряна. Попробуй выбрать время заново.")
        await state.clear()
        return

    await complete_manual_schedule(
        user_id_from_message(message),
        state,
        scheduled_time,
        reply_target=message,
        cleanup_message_id=message.message_id,
    )


@dp.message(ScheduleState.waiting_for_minutes)
async def process_custom_minutes(message: types.Message, state: FSMContext):
    text = (message.text or "").strip()
    try:
        minute = int(text)
    except ValueError:
        await message.answer("❌ Напиши минуты числом от 0 до 59.")
        return

    if not 0 <= minute <= 59:
        await message.answer("❌ Минуты должны быть от 0 до 59.")
        return

    data = await state.get_data()
    hour = data.get("selected_hour")
    if hour is None:
        await message.answer("Ошибка: час выбора утерян. Попробуй выбрать время заново.")
        await state.clear()
        return

    scheduled_time = build_scheduled_time_from_state(data, hour=int(hour), minute=minute, tz=user_tz(user_id_from_message(message)))
    if scheduled_time is None:
        await message.answer("Ошибка: дата выбора утеряна. Попробуй выбрать время заново.")
        await state.clear()
        return

    await complete_manual_schedule(
        user_id_from_message(message),
        state,
        scheduled_time,
        reply_target=message,
        cleanup_message_id=message.message_id,
    )


@dp.message(Command("test_digest"))
async def cmd_test_digest(message: types.Message, state: FSMContext):
    await state.clear()
    user_id = user_id_from_message(message)
    # SEC1 (п.3): тестовая сборка обходит все подписки — общий кулдаун.
    wait_seconds = check_expensive_command_cooldown(user_id)
    if wait_seconds:
        await message.answer(f"⏳ Подожди {wait_seconds} с.")
        return
    user_subs = [sub for sub in get_user_subscriptions(user_id) if not sub[5]]

    if not user_subs:
        await message.answer("📭 У тебя нет активных подписок для парсинга (учитываются только не на паузе).")
        return

    await message.answer("⏳ Собираю единый дайджест за последние 24 часа...")
    now_tz = local_now()
    last_24h_str = serialize_datetime(now_tz - timedelta(days=1))
    semaphore = channel_source.fetch_semaphore

    async with create_telegram_http_session() as session:
        async def load_posts(subscription) -> tuple[int, str, str | None, str, list[dict]]:
            sub_id, username, title, period = subscription[0], subscription[1], subscription[2], subscription[3]
            async with semaphore:
                return sub_id, period, title, username, await fetch_channel_posts(username, last_24h_str, session)

        results: list[tuple[int, str, str | None, str, list[dict]] | BaseException] = await asyncio.gather(
            *(load_posts(sub) for sub in user_subs),
            return_exceptions=True,
        )

    sections: list[dict] = []
    for result in results:
        if isinstance(result, BaseException):
            logger.error(
                "test_digest channel fetch failed",
                exc_info=(type(result), result, result.__traceback__),
            )
            continue

        sub_id, period, title, username, posts = result
        if posts:
            selected_posts, omitted_count = prioritize_channel_posts(username, posts)
            if selected_posts:
                sections.append(
                    {
                        "sub_id": sub_id,
                        "period": period,
                        "title": title or "Канал",
                        "username": username,
                        "posts": selected_posts,
                        "omitted_count": omitted_count,
                    }
                )
            try:
                add_digest_posts(user_id, username, title, posts)
            except Exception:
                logger.exception("Не удалось сохранить посты в базу знаний (test_digest)")

    if not sections:
        await message.answer("📭 За последние 24 часа новых постов ни в одном из каналов не было.")
        return

    await deliver_digest(user_id, "Твоя газета за последние 24 часа", sections, group_by_period=False)


EMAIL_CODE_TTL_MINUTES = 15
EMAIL_CODE_RESEND_SECONDS = 60


def build_email_menu(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    back_row = [InlineKeyboardButton(text="⚙️ Настройки дайджеста", callback_data="digest_settings")]
    title = "📧 <b>Дайджест на почту</b>\n\n"
    if not email_digest.is_enabled():
        return title + "Отправка писем пока не настроена на сервере.", InlineKeyboardMarkup(inline_keyboard=[back_row])

    address, email_periods = get_email_delivery(user_id)
    if address:
        text = (
            f"{title}Адрес: <b>{html.escape(address)}</b>\n\n"
            "Каждый тип дайджеста приходит <b>либо сюда, либо на почту</b> — не в оба места. "
            "Нажми на тип, чтобы переключить:"
        )
        rows = [
            [
                InlineKeyboardButton(
                    text=f"{_PERIOD_TITLES[period]}: {'📧 Почта' if period in email_periods else '💬 Telegram'}",
                    callback_data=f"email_period_{period}",
                )
            ]
            for period in _PERIOD_TITLES
        ]
        rows += [
            [
                InlineKeyboardButton(text="✏️ Сменить адрес", callback_data="email_set"),
                InlineKeyboardButton(text="🗑 Удалить", callback_data="email_delete"),
            ],
            back_row,
        ]
    else:
        text = (
            f"{title}Дайджесты можно получать на почту — с той же вёрсткой, что в Telegraph. "
            "Выбранные типы (ежедневный, еженедельный, ежемесячный) будут приходить только на почту, "
            "остальные — как раньше, сюда.\n\n"
            "Укажи адрес, и я пришлю на него код подтверждения."
        )
        rows = [[InlineKeyboardButton(text="✏️ Указать адрес", callback_data="email_set")], back_row]
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def build_email_cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отмена", callback_data="email_menu")]])


@dp.message(Command("email"))
async def cmd_email(message: types.Message, state: FSMContext):
    await state.clear()
    text, markup = build_email_menu(user_id_from_message(message))
    await message.answer(text, parse_mode="HTML", reply_markup=markup)


@dp.callback_query(F.data == "email_menu")
async def open_email_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    text, markup = build_email_menu(user_id_from_callback(callback))
    await edit_or_answer(callback, text, reply_markup=markup)
    await callback.answer()


@dp.callback_query(F.data.startswith("email_period_"))
async def toggle_email_period(callback: CallbackQuery):
    period = parse_callback_strip_prefix(callback.data, "email_period_")
    if period not in _PERIOD_TITLES:
        await callback.answer("❌ Некорректные данные.")
        return
    user_id = user_id_from_callback(callback)
    address, email_periods = get_email_delivery(user_id)
    if not address:
        await callback.answer("Сначала подтверди адрес почты.", show_alert=True)
        return
    to_email = period not in email_periods
    set_email_period(user_id, period, to_email)
    name = _PERIOD_PLURAL[period]
    warning = (
        f"📧 Теперь {name} дайджесты приходят только на почту {address}.\n\n"
        "⚠️ В Telegram они больше приходить не будут."
        if to_email
        else f"💬 Теперь {name} дайджесты приходят сюда, в Telegram.\n\n⚠️ Письма по ним отключены."
    )
    text, markup = build_email_menu(user_id)
    await edit_or_answer(callback, text, reply_markup=markup)
    await callback.answer(warning, show_alert=True)


@dp.callback_query(F.data == "email_delete")
async def remove_email(callback: CallbackQuery):
    _, email_periods = get_email_delivery(user_id_from_callback(callback))
    delete_user_email(user_id_from_callback(callback))
    text, markup = build_email_menu(user_id_from_callback(callback))
    await edit_or_answer(callback, text, reply_markup=markup)
    if email_periods:
        await callback.answer("Адрес удалён. ⚠️ Все дайджесты снова приходят в Telegram.", show_alert=True)
    else:
        await callback.answer("Адрес удалён")


@dp.callback_query(F.data == "email_set")
async def ask_email_address(callback: CallbackQuery, state: FSMContext):
    if not email_digest.is_enabled():
        await callback.answer("Отправка писем не настроена.", show_alert=True)
        return
    await state.set_state(EmailState.waiting_for_email)
    await edit_or_answer(
        callback,
        "📧 Пришли адрес почты одним сообщением, например <code>name@example.com</code>.",
        reply_markup=build_email_cancel_keyboard(),
    )
    await callback.answer()


@dp.message(EmailState.waiting_for_email)
async def process_email_address(message: types.Message, state: FSMContext):
    address = email_digest.normalize_email(message.text)
    if not address:
        await message.answer("❌ Не похоже на адрес почты. Пример: name@example.com", reply_markup=build_email_cancel_keyboard())
        return

    user_id = user_id_from_message(message)
    now = utc_now()
    record = get_user_email(user_id)
    if record and record.get("code_sent_at"):
        if parse_db_datetime(record["code_sent_at"]) > now - timedelta(seconds=EMAIL_CODE_RESEND_SECONDS):
            await message.answer("⏳ Код только что отправлялся — подожди минуту и пришли адрес снова.")
            return

    # SEC1 (п.4): не даём рассылать письма с SMTP бота на произвольные адреса.
    since = serialize_datetime(now - timedelta(days=1))
    if count_email_sends_for_user(user_id, since) >= EMAIL_DAILY_LIMIT_PER_USER:
        await message.answer("🚫 Суточный лимит писем исчерпан. Попробуй позже.")
        return
    if count_email_sends_for_address(address, since) >= EMAIL_DAILY_LIMIT_PER_ADDRESS:
        await message.answer("🚫 На этот адрес сегодня уже отправлялось слишком много писем.")
        return

    code = f"{secrets.randbelow(10**6):06d}"
    sent = await email_digest.send_email(
        address,
        f"Код подтверждения Unagi: {code}",
        f"Код для подключения дайджестов Unagi к этой почте: {code}\n\n"
        f"Он действует {EMAIL_CODE_TTL_MINUTES} минут. Если вы ничего не запрашивали, просто проигнорируйте письмо.",
    )
    if not sent:
        await message.answer(
            f"❌ Не удалось отправить письмо на {html.escape(address)}. Проверь адрес и попробуй ещё раз.",
            reply_markup=build_email_cancel_keyboard(),
        )
        return

    start_email_verification(
        user_id,
        address,
        code,
        serialize_datetime(now),
        serialize_datetime(now + timedelta(minutes=EMAIL_CODE_TTL_MINUTES)),
    )
    record_email_send(user_id, address, serialize_datetime(now))
    await state.set_state(EmailState.waiting_for_code)
    await message.answer(
        f"✉️ Отправил код на <b>{html.escape(address)}</b>. Пришли его сюда — он действует "
        f"{EMAIL_CODE_TTL_MINUTES} минут. Если письма нет, проверь «Спам».",
        parse_mode="HTML",
        reply_markup=build_email_cancel_keyboard(),
    )


@dp.message(EmailState.waiting_for_code)
async def process_email_code(message: types.Message, state: FSMContext):
    code = re.sub(r"\D", "", message.text or "")
    result = confirm_email_code(user_id_from_message(message), code, serialize_datetime(utc_now()))
    if result == "invalid":
        await message.answer("❌ Неверный код. Попробуй ещё раз.", reply_markup=build_email_cancel_keyboard())
        return
    await state.clear()
    notices = {
        "ok": "✅ Почта подтверждена. Выбери ниже, какие дайджесты получать на почту вместо Telegram.",
        "expired": "⌛ Код устарел — запроси новый.",
        "too_many": "🚫 Слишком много попыток — запроси новый код.",
    }
    text, markup = build_email_menu(user_id_from_message(message))
    await message.answer(
        f"{notices.get(result, 'Запроси код заново.')}\n\n{text}", parse_mode="HTML", reply_markup=markup
    )


async def resolve_public_channel(username: str) -> tuple[str, str] | None:
    """Return (username, title) if @username is a public channel we can build digests from."""
    try:
        chat = await bot.get_chat(f"@{username}")
    except TelegramAPIError:
        chat = None
    if chat is not None:
        if chat.type != "channel" or not chat.username:
            return None
        return chat.username, chat.title or chat.username
    # Bot API may not see some channels; the web preview is what digests read anyway.
    try:
        async with create_telegram_http_session() as session:
            await get_latest_posts(username, serialize_datetime(utc_now()), session=session)
    except ChannelFetchError:
        return None
    except Exception:
        logger.warning("Web check for @%s failed", username, exc_info=True)
        return None
    return username, username


def message_link_urls(message: types.Message) -> list[str]:
    entities = (message.entities or []) + (message.caption_entities or [])
    return [entity.url for entity in entities if entity.type == "text_link" and entity.url]


@fallback_router.message()
async def catch_message(message: types.Message, state: FSMContext):
    # A text reply to a delivered reminder is a new time for it: «завтра 11», «через 2 дня».
    if message.text and not message.text.startswith("/"):
        scheduled_msg = get_replied_sent_schedule(message)
        if scheduled_msg:
            await state.clear()
            await reschedule_replied_by_text(message, scheduled_msg, message.text)
            return

    current_state = await state.get_state()
    await state.clear()

    # T17: Followup reschedule for recently delivered reminder without reply
    if (
        current_state is None
        and message.text
        and not message.text.startswith("/")
        and message.reply_to_message is None
        and not getattr(message, "forward_origin", None)
    ):
        if await reminders_ux.handle_followup_text_reschedule(message):
            return

    channel_username = None
    channel_title = None
    channel_note = ""

    if message.forward_origin and message.forward_origin.type == "channel":
        if getattr(message.forward_origin.chat, "username", None):
            channel_username = message.forward_origin.chat.username
            channel_title = message.forward_origin.chat.title
    else:
        text = message.text or message.caption or ""
        referenced = find_channel_username(text, extra_urls=message_link_urls(message))
        if referenced:
            resolved = await resolve_public_channel(referenced)
            if resolved:
                channel_username, channel_title = resolved
            elif text.strip().startswith("@") or re.search(r"t\.me|telegram\.(?:me|dog)|tg://", text, re.I):
                channel_note = f"\n\nℹ️ @{html.escape(referenced)} — не публичный канал или не найден, дайджест недоступен."
        elif is_private_invite(text):
            channel_note = "\n\nℹ️ Это приглашение в закрытый канал — дайджест работает только с публичными."

    kb = build_standard_message_keyboard().inline_keyboard

    prompt = "Что мне сделать с этим сообщением?"
    if channel_username:
        kb.insert(0, [InlineKeyboardButton(text="📡 Собирать дайджест", callback_data="digest_setup")])
        await state.update_data(channel_username=channel_username, channel_title=channel_title)
        prompt = f"📡 Канал <b>{html.escape(channel_title or channel_username)}</b> (@{html.escape(channel_username)})\nЧто сделать?"

    user_id = user_id_from_message(message)
    now = user_now(user_id)
    hours = day_part_hours(user_id)
    await message.reply(
        f"{prompt}{channel_note}\n\n⏰ {describe_quick_times(now, hours)}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),
    )


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
        [InlineKeyboardButton(text="📰 В Ленту (без расписания)", callback_data="feed_add")],
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

    try:
        if callback_message.reply_to_message:
            await bot.delete_message(user_id_from_callback(callback), callback_message.reply_to_message.message_id)
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


@dp.callback_query(F.data.startswith("time_"))
async def handle_time_selection(callback: CallbackQuery, state: FSMContext):
    callback_message = get_callback_message(callback)
    if callback_message is None:
        await callback.answer("❌ Сообщение недоступно.")
        return

    user_id = user_id_from_callback(callback)
    now = user_now(user_id)
    hours = day_part_hours(user_id)
    if callback_message.reply_to_message is None:
        await callback_message.edit_text("❌ Ошибка: не могу найти оригинальное сообщение.")
        return

    orig_msg = callback_message.reply_to_message
    preview, source = get_message_preview(orig_msg)
    full_text = get_message_full_text(orig_msg)
    source_url = reminders_ux.extract_source_url(orig_msg)
    if source == "Твой текст":
        log_name, stripped = reminders_ux.parse_copied_log_prefix(full_text)
        if log_name:
            source = log_name
            normalized = stripped.replace('\n', ' ')
            preview = normalized[:37] + '...' if len(normalized) > 40 else normalized

    action = parse_callback_strip_prefix(callback.data, "time_")
    if action is None:
        await callback_message.edit_text("❌ Некорректные данные.")
        return

    if action == "custom":
        await state.update_data(
            message_id=orig_msg.message_id,
            preview=preview,
            source=source,
            source_url=source_url,
            full_text=full_text,
            prompt_msg_id=callback_message.message_id,
        )
        await push_screen(state, "manual_date")
        await callback_message.edit_text(
            "Выбери дату:",
            reply_markup=build_manual_date_keyboard(),
        )
        await callback.answer()
        return

    scheduled_time, label = get_quick_scheduled_time(action, now, hours=hours)

    if scheduled_time:
        try:
            new_id = add_message(
                user_id_from_callback(callback),
                orig_msg.message_id,
                serialize_datetime(scheduled_time),
                preview,
                source,
                full_text=full_text,
                source_url=source_url,
            )
        except ValueError as exc:
            await callback.answer(f"🚫 {exc}", show_alert=True)
            return
        reply_markup = reminders_ux.build_reschedule_note_keyboard(new_id) if new_id else None
        try:
            await callback_message.edit_text(
                f"✅ Принято! Отправлю это сообщение тебе {label}.",
                reply_markup=reply_markup,
            )
        except TelegramAPIError:
            pass


@dp.callback_query(F.data.startswith("sdate_"))
async def handle_manual_date_selection(callback: CallbackQuery, state: FSMContext):
    callback_message = get_callback_message(callback)
    if callback_message is None:
        await callback.answer("❌ Сообщение недоступно.")
        return

    action = parse_callback_strip_prefix(callback.data, "sdate_")
    if action is None:
        await callback.answer("❌ Некорректные данные.")
        return

    data = await state.get_data()
    if not has_schedule_context(data):
        await callback.answer("❌ Выбор времени устарел. Отправь сообщение заново.", show_alert=True)
        await state.clear()
        return

    user_id = user_id_from_callback(callback)
    user_time = user_now(user_id)
    hours = day_part_hours(user_id)

    if action == "manual":
        suggested_time = get_suggested_manual_time(user_time, hours=hours)
        suggested_str = suggested_time.strftime("%d.%m.%Y %H:%M")
        await state.set_state(ScheduleState.waiting_for_datetime)
        await callback_message.edit_text(
            f"Напиши, когда напомнить: {TIME_TEXT_EXAMPLES}, «в пятницу в 10» или точно — ДД.ММ.ГГГГ ЧЧ:ММ.\n\n"
            "💡 Нажми на время ниже, чтобы скопировать его:\n\n"
            f"`{suggested_str}`",
            parse_mode="Markdown",
            reply_markup=build_back_home_keyboard(),
        )
        await callback.answer()
        return

    selected = get_manual_date(action, user_time)
    if selected is None:
        await callback.answer("❌ Некорректная дата.")
        return

    await state.update_data(selected_date=selected.strftime("%Y-%m-%d"))
    await push_screen(state, "manual_hour", selected_date=selected.strftime("%d.%m.%Y"))
    await callback_message.edit_text(
        f"Дата: {selected.strftime('%d.%m.%Y')}\nВыбери час:",
        reply_markup=build_manual_hour_keyboard(),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("shour_"))
async def handle_manual_hour_selection(callback: CallbackQuery, state: FSMContext):
    callback_message = get_callback_message(callback)
    if callback_message is None:
        await callback.answer("❌ Сообщение недоступно.")
        return

    action = parse_callback_strip_prefix(callback.data, "shour_")
    if action is None:
        await callback.answer("❌ Некорректные данные.")
        return

    data = await state.get_data()
    if not has_schedule_context(data):
        await callback.answer("❌ Выбор времени устарел. Отправь сообщение заново.", show_alert=True)
        await state.clear()
        return

    if not data.get("selected_date"):
        await callback.answer("❌ Сначала выбери дату.", show_alert=True)
        return

    if action == "manual":
        await state.set_state(ScheduleState.waiting_for_time)
        await callback_message.edit_text("Напиши время в формате ЧЧ:ММ, например 18:30.", reply_markup=build_back_home_keyboard())
        await callback.answer()
        return

    try:
        hour = int(action)
    except ValueError:
        await callback.answer("❌ Некорректный час.")
        return

    if not 0 <= hour <= 23:
        await callback.answer("❌ Некорректный час.")
        return

    await state.update_data(selected_hour=hour)
    await push_screen(state, "manual_minute", selected_hour=hour)
    await callback_message.edit_text(
        f"Время: {hour:02d}:00\nВыбери минуты:",
        reply_markup=build_manual_minute_keyboard(),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("smin_"))
async def handle_manual_minute_selection(callback: CallbackQuery, state: FSMContext):
    callback_message = get_callback_message(callback)
    if callback_message is None:
        await callback.answer("❌ Сообщение недоступно.")
        return

    action = parse_callback_strip_prefix(callback.data, "smin_")
    if action is None:
        await callback.answer("❌ Некорректные данные.")
        return

    data = await state.get_data()
    if not has_schedule_context(data):
        await callback.answer("❌ Выбор времени устарел. Отправь сообщение заново.", show_alert=True)
        await state.clear()
        return

    hour = data.get("selected_hour")
    if hour is None:
        await callback.answer("❌ Сначала выбери час.", show_alert=True)
        return

    if action == "manual":
        await state.set_state(ScheduleState.waiting_for_minutes)
        await callback_message.edit_text("Напиши минуты числом от 0 до 59.", reply_markup=build_back_home_keyboard())
        await callback.answer()
        return

    try:
        minute = int(action)
    except ValueError:
        await callback.answer("❌ Некорректные минуты.")
        return

    if not 0 <= minute <= 59:
        await callback.answer("❌ Некорректные минуты.")
        return

    scheduled_time = build_scheduled_time_from_state(
        data, hour=int(hour), minute=minute, tz=user_tz(user_id_from_callback(callback))
    )
    if scheduled_time is None:
        await callback.answer("❌ Дата выбора утеряна.", show_alert=True)
        await state.clear()
        return

    try:
        await complete_manual_schedule(
            user_id_from_callback(callback),
            state,
            scheduled_time,
            reply_target=callback_message,
        )
    except Exception:
        logger.exception("complete_manual_schedule failed in minute picker")
        await callback.answer(USER_FACING_ERROR, show_alert=True)
        return

    await callback.answer()


# Fallback router with catch-all catch_message must be included after all feature routers
dp.include_router(fallback_router)


async def check_messages():
    while True:
        now_str = serialize_datetime(utc_now())
        pending = get_pending_messages(now_str)

        for row in pending:
            db_id = row["id"]
            chat_id = row["user_id"]
            message_id = row["message_id"]
            retry_count = row["retry_count"]
            text_preview = row["text_preview"]
            source_name = row["source_name"]
            full_text = row["full_text"]
            note = row["note"]
            attempt_number = retry_count + 1
            try:
                if message_id is None or message_id == 0:
                    raw_text = full_text or text_preview or ""
                    content_text = html.escape(raw_text)
                    if source_name:
                        text_to_send = f"👤 <b>Источник:</b> {html.escape(source_name)}\n\n{content_text}"
                    else:
                        text_to_send = content_text
                    sent_message = await bot.send_message(
                        chat_id=int(chat_id),
                        text=text_to_send,
                        parse_mode="HTML",
                        reply_markup=build_sent_reminder_actions_keyboard(int(chat_id)),
                    )
                else:
                    sent_message = await bot.copy_message(
                        chat_id=int(chat_id),
                        from_chat_id=int(chat_id),
                        message_id=message_id,
                        reply_markup=build_sent_reminder_actions_keyboard(int(chat_id)),
                    )
                mark_as_sent(db_id, sent_message.message_id)
                if note:
                    try:
                        await bot.send_message(
                            chat_id=int(chat_id),
                            text=f"📝 {note}",
                            reply_to_message_id=sent_message.message_id,
                        )
                    except TelegramAPIError:
                        logger.warning("Не удалось отправить примечание к сообщению %s", db_id)
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


async def process_user_edition(
    user_id: int,
    subs: list,
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore,
    now_str: str,
    prefetched_channels: dict[str, dict],
) -> dict[str, int]:
    """Collect and deliver one edition of the day for a user.

    The edition may mix periods (daily + weekly/monthly on the same day) but goes out as a
    single delivery. Execution records, cursors and failure accounting stay per period.
    """
    stats = {"failures": 0, "delivered_users": 0, "delivered_posts": 0}
    by_period: dict[str, list] = {}
    for sub in subs:
        by_period.setdefault(sub[4], []).append(sub)
    periods = sorted(by_period, key=lambda p: digest_edition.PERIOD_RANK.get(p, 0))
    executions = {
        period: record_digest_execution_start(
            user_id,
            period,
            min((sub[8] for sub in by_period[period] if len(sub) > 8 and sub[8]), default=now_str),
        )
        for period in periods
    }

    try:
        results = await asyncio.gather(
            *(fetch_subscription_posts(sub, session, semaphore, now_str, prefetched_channels) for sub in subs),
            return_exceptions=True,
        )

        report = {
            period: {"sections": [], "successful": [], "errors": [], "failed": 0, "partial": 0, "temporary": False}
            for period in periods
        }
        for sub, result in zip(subs, results):
            period_report = report[sub[4]]
            if isinstance(result, BaseException):
                stats["failures"] += 1
                period_report["failed"] += 1
                period_report["temporary"] = True
                logger.error(
                    "Изолирована необработанная ошибка чтения для пользователя %s (%s)",
                    user_id,
                    sub[4],
                    exc_info=(type(result), result, result.__traceback__),
                )
                continue
            if result["status"] == "error":
                stats["failures"] += 1
                period_report["failed"] += 1
                period_report["errors"].append(result)
                if not result["is_permanent"]:
                    period_report["temporary"] = True
                continue

            period_report["successful"].append(result)
            if result.get("is_partial"):
                period_report["partial"] += 1
            if result["posts"]:
                period_report["sections"].append(
                    {
                        "sub_id": result["sub_id"],
                        "period": result["period"],
                        "title": result["title"] or "Канал",
                        "username": result["username"],
                        "posts": result["posts"],
                        "omitted_count": result.get("omitted_count", 0),
                    }
                )
                try:
                    add_digest_posts(user_id, result["username"], result["title"], result["posts"])
                except Exception:
                    logger.exception("Не удалось сохранить посты в базу знаний")

        edition_sections = digest_edition.dedupe_edition_sections(
            [section for period in periods for section in report[period]["sections"]]
        )
        delivery_error: TelegramAPIError | None = None
        if edition_sections:
            try:
                await deliver_digest(user_id, None, edition_sections)
                reset_user_alert_consecutive_failures(user_id)
                stats["delivered_users"] += 1
                stats["delivered_posts"] += sum(len(section["posts"]) for section in edition_sections)
            except TelegramAPIError as exc:
                delivery_error = exc
                logger.exception("Не удалось отправить дайджест пользователю %s (%s)", user_id, ",".join(periods))

        for period in periods:
            stats["failures"] += _finish_period_execution(
                executions[period], by_period[period], report[period], delivery_error, now_str
            )
            if report[period]["temporary"]:
                logger.info(
                    "Для пользователя %s (%s) чтение части каналов будет повторено позже.",
                    user_id,
                    period,
                )
    except Exception as exc:
        for period in periods:
            stats["failures"] += len(by_period[period])
            record_digest_execution_finish(
                executions[period],
                status="failed",
                posts_count=0,
                channels_count=len(by_period[period]),
                channels_failed=len(by_period[period]),
                channels_partial=0,
                error_message=str(exc),
            )
        logger.exception(
            "Изолирована ошибка обработки дайджеста пользователя %s; продолжаю со следующим пользователем",
            user_id,
        )
    return stats


def _finish_period_execution(
    execution_id: int,
    period_subs: list,
    period_report: dict,
    delivery_error: TelegramAPIError | None,
    now_str: str,
) -> int:
    """Move cursors and close the execution of one period; returns extra failures to count."""
    sections = period_report["sections"]
    successful = period_report["successful"]
    channels_failed = period_report["failed"]
    channels_partial = period_report["partial"]

    if sections and delivery_error is None:
        for result in successful:
            if result["posts"]:
                update_subscription_time(
                    result["sub_id"],
                    result["last_scraped_str"],
                    result["next_send_str"],
                    result["last_post_id"],
                )
            else:
                update_subscription_schedule(result["sub_id"], result["next_send_str"], now_str)
        record_digest_execution_finish(
            execution_id,
            status="partial" if (channels_failed > 0 or channels_partial > 0) else "delivered",
            posts_count=sum(len(section["posts"]) for section in sections),
            channels_count=len(period_subs),
            channels_failed=channels_failed,
            channels_partial=channels_partial,
        )
        return 0

    if sections:
        is_permanent, error_text = classify_telegram_send_error(delivery_error)
        retries_exhausted = False
        for result in successful:
            original = next((item for item in period_subs if item[0] == result["sub_id"]), None)
            new_failure_count = (original[6] if original else 0) + 1
            if new_failure_count >= MAX_DIGEST_RETRIES:
                retries_exhausted = True
            mark_subscription_delivery_error(
                result["sub_id"],
                error_text,
                now_str,
                new_failure_count,
                is_permanent or new_failure_count >= MAX_DIGEST_RETRIES,
            )
        record_digest_execution_finish(
            execution_id,
            status="failed" if (is_permanent or retries_exhausted) else "retrying",
            posts_count=0,
            channels_count=len(period_subs),
            channels_failed=len(period_subs),
            channels_partial=channels_partial,
            error_message=error_text,
        )
        return len(successful)

    for result in successful:
        update_subscription_schedule(result["sub_id"], result["next_send_str"], now_str)
    if channels_failed == 0:
        record_digest_execution_finish(
            execution_id,
            status="empty",
            posts_count=0,
            channels_count=len(period_subs),
            channels_failed=0,
            channels_partial=channels_partial,
        )
    else:
        all_permanent = (
            channels_failed == len(period_subs)
            and len(period_report["errors"]) == channels_failed
            and all(error.get("is_permanent", False) for error in period_report["errors"])
        )
        record_digest_execution_finish(
            execution_id,
            status="failed" if all_permanent else "retrying",
            posts_count=0,
            channels_count=len(period_subs),
            channels_failed=channels_failed,
            channels_partial=channels_partial,
            error_message="Не удалось прочитать каналы",
        )
    return 0


def plan_due_editions(due_subs: list, now: datetime) -> tuple[dict[int, list], int]:
    """Group due subscriptions into one edition per user for today.

    Juniors due before the senior period's time are deferred to it (persisted, so they
    stop occupying the due batch); later-today subscriptions are pulled into the edition.
    Returns ({user_id: edition subscriptions}, number of deferred subscriptions).
    """
    day_end = digest_edition.local_day_end(now)
    editions: dict[int, list] = {}
    deferred = 0
    for user_id in dict.fromkeys(sub[1] for sub in due_subs):
        user_subs = get_user_subscriptions_due_until(user_id, day_end)
        defer_until, edition_subs = digest_edition.plan_daily_edition(user_subs, now)
        if defer_until is not None:
            deferred += defer_subscriptions([sub[0] for sub in edition_subs], serialize_datetime(defer_until))
            continue
        if edition_subs:
            editions[user_id] = edition_subs
    return editions, deferred


async def run_digest_cycle(session: aiohttp.ClientSession, semaphore: asyncio.Semaphore) -> int:
    cycle_started_at = time.monotonic()
    now = utc_now()
    now_str = serialize_datetime(now)
    due_total = count_due_subscriptions(now_str)
    due_subs = get_due_subscriptions(now_str, DIGEST_DUE_BATCH_SIZE)
    if not due_subs:
        logger.info(
            "digest_cycle due_total=0 processed=0 channels=0 fetched_posts=0 "
            "delivered_users=0 delivered_posts=0 failures=0 duration_ms=%d next_check_min=%d",
            round((time.monotonic() - cycle_started_at) * 1000),
            DIGEST_CHECK_INTERVAL_SECONDS // 60,
        )
        return 0

    editions, deferred_count = plan_due_editions(due_subs, now)
    edition_subs = [sub for subs in editions.values() for sub in subs]
    logger.info(
        "🔄 Начинаю проверку %s/%s due-дайджестов (batch_limit=%s, next_send_at <= %s): "
        "выпусков %s, подписок в них %s, отложено до старшего выпуска %s",
        len(due_subs),
        due_total,
        DIGEST_DUE_BATCH_SIZE,
        now_str,
        len(editions),
        len(edition_subs),
        deferred_count,
    )
    prefetched_channels = await prefetch_due_channels(edition_subs, session, semaphore) if edition_subs else {}
    fetched_posts_count = sum(
        len(result["posts"])
        for result in prefetched_channels.values()
        if result.get("error") is None
    )
    channel_failures_count = sum(
        1 for result in prefetched_channels.values() if result.get("error") is not None
    )
    failures_count = 0
    delivered_users_count = 0
    delivered_posts_count = 0

    for user_id, subs in editions.items():
        stats = await process_user_edition(user_id, subs, session, semaphore, now_str, prefetched_channels)
        failures_count += stats["failures"]
        delivered_users_count += stats["delivered_users"]
        delivered_posts_count += stats["delivered_posts"]

    logger.info(
        "digest_cycle due_total=%s processed=%s deferred=%s backlog=%s channels=%s channel_failures=%s "
        "fetched_posts=%s delivered_users=%s delivered_posts=%s subscription_failures=%s duration_ms=%s",
        due_total,
        len(edition_subs),
        deferred_count,
        max(due_total - len(due_subs), 0),
        len(prefetched_channels),
        channel_failures_count,
        fetched_posts_count,
        delivered_users_count,
        delivered_posts_count,
        failures_count,
        round((time.monotonic() - cycle_started_at) * 1000),
    )
    backlog = max(due_total - len(due_subs), 0)
    record_digest_cycle_heartbeat(backlog=backlog)
    return backlog


async def check_digests():
    semaphore = channel_source.fetch_semaphore
    logger.info(
        "📬 Сервис дайджестов запущен (проверка каждые %d сек / %d мин)",
        DIGEST_CHECK_INTERVAL_SECONDS,
        DIGEST_CHECK_INTERVAL_SECONDS // 60,
    )

    async with create_telegram_http_session() as session:
        while True:
            try:
                backlog = await run_digest_cycle(session, semaphore)
                await process_pending_digest_alerts(bot)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Цикл дайджестов упал, но будет автоматически перезапущен")
                backlog = 0
            await asyncio.sleep(
                DIGEST_BACKLOG_RETRY_SECONDS if backlog else DIGEST_CHECK_INTERVAL_SECONDS
            )


async def cleanup_database():
    while True:
        now_str = serialize_datetime(utc_now())
        try:
            # Bulk DELETEs over the history tables — keep them off the event loop.
            cleanup_stats = await asyncio.to_thread(cleanup_old_records, now_str)
            if any(cleanup_stats.values()):
                logger.info("Очистка SQLite завершена: %s", cleanup_stats)
        except Exception:
            logger.exception("Очистка SQLite упала, повторю в следующем цикле")
        await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)


async def main():
    global BOT_USERNAME
    init_db()
    # Register bot commands
    try:
        await set_bot_commands(bot)
        logger.info("🤖 Меню подсказок команд зарегистрировано в Telegram")
    except Exception as exc:
        logger.warning("⚠️ Не удалось зарегистрировать подсказки команд: %s", exc)
    if not BOT_USERNAME:
        me = await bot.get_me()
        BOT_USERNAME = (me.username or "").lstrip("@")
    await bot.delete_webhook(drop_pending_updates=True)
    await channel_source.start()
    background_tasks = [
        asyncio.create_task(check_messages(), name="reminder-scheduler"),
        asyncio.create_task(check_digests(), name="digest-scheduler"),
        asyncio.create_task(cleanup_database(), name="database-cleanup"),
        asyncio.create_task(run_watchdog_loop(bot), name="watchdog-monitor"),
        asyncio.create_task(feed.run_autodelivery_loop(), name="feed-autodelivery"),
    ]
    logger.info("✅ Бот успешно запущен")
    logger.info("📋 Фоновые процессы:")
    logger.info("  • Напоминания: проверка каждые 30 сек")
    logger.info("  • Дайджесты: проверка каждые %d мин (оптимизировано с 1 мин)", DIGEST_CHECK_INTERVAL_SECONDS // 60)
    logger.info("  • Очистка БД: раз в %d часов", CLEANUP_INTERVAL_SECONDS // 3600)
    logger.info("  • Watchdog: проверка каждые %d сек", WATCHDOG_CHECK_INTERVAL_SECONDS)
    logger.info("  • Автодоставка Ленты: проверка каждые %d сек", feed.FEED_AUTODELIVERY_CHECK_SECONDS)
    logger.info("🚀 Ожидаю входящих сообщений...")
    try:
        await dp.start_polling(bot)
    finally:
        for task in background_tasks:
            task.cancel()
        await asyncio.gather(*background_tasks, return_exceptions=True)
        await feed.cancel_backfill_tasks()
        await channel_source.close()


if __name__ == "__main__":
    asyncio.run(main())
