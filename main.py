import asyncio
import calendar
import html
import io
import json
import logging
import os
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import aiohttp
from aiogram import Bot, Dispatcher, F, types
from aiogram.client.session.aiohttp import AiohttpSession
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
    add_message,
    add_saved_message,
    add_subscription,
    cleanup_old_records,
    clear_subscription_failure,
    count_due_subscriptions,
    count_user_subscriptions,
    delete_message,
    delete_saved_message,
    export_user_data,
    get_ai_usage_today,
    get_digest_post_channels,
    get_digest_posts,
    get_digest_settings,
    delete_subscription,
    get_channel_posts_since,
    get_due_subscriptions,
    get_pending_messages,
    get_saved_message_by_id,
    get_scheduled_message_by_delivered_message_id,
    get_subscription_by_id,
    get_subscription_tags,
    get_saved_messages,
    get_user_messages,
    get_user_subscriptions,
    get_user_tags,
    increment_ai_usage,
    init_db,
    mark_as_sent,
    mark_message_delivery_error,
    mark_subscription_delivery_error,
    normalize_channel_username,
    parse_db_datetime,
    replace_sent_reminder_with_pending,
    serialize_datetime,
    set_all_subscriptions_paused,
    set_subscription_paused,
    set_subscription_tag,
    unsubscribe_all,
    update_subscriptions_next_send_at,
    update_subscription_time,
    update_subscription_schedule,
    upsert_channel_posts,
    upsert_digest_settings,
    utc_now,
    update_saved_message_tag,
)
from scraper import ChannelFetchError, REQUEST_TIMEOUT
from channel_source import HybridChannelSource
import ai_assistant
from telegraph_publisher import publish_digest

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
TZ = ZoneInfo(os.getenv("APP_TIMEZONE", "Europe/Moscow"))
MAX_MESSAGE_RETRIES = 5
MAX_DIGEST_RETRIES = 5
DIGEST_FETCH_CONCURRENCY = 5
DIGEST_DUE_BATCH_SIZE = max(1, int(os.getenv("DIGEST_DUE_BATCH_SIZE", "100")))
DIGEST_BACKLOG_RETRY_SECONDS = max(1, int(os.getenv("DIGEST_BACKLOG_RETRY_SECONDS", "5")))
DIGEST_CHECK_INTERVAL_SECONDS = int(os.getenv("DIGEST_CHECK_INTERVAL_SECONDS", 30 * 60))  # Можно переопределить через .env
CLEANUP_INTERVAL_SECONDS = 6 * 60 * 60
# Дайджесты с таким числом постов и более публикуются в Telegraph одной ссылкой.
DIGEST_TELEGRAPH_THRESHOLD = int(os.getenv("DIGEST_TELEGRAPH_THRESHOLD", "4"))
# Лимиты на импорт, чтобы один файл не положил бота.
MAX_IMPORT_SUBSCRIPTIONS = int(os.getenv("MAX_IMPORT_SUBSCRIPTIONS", "300"))
MAX_IMPORT_BOOKMARKS = int(os.getenv("MAX_IMPORT_BOOKMARKS", "1000"))
MAX_IMPORT_REMINDERS = int(os.getenv("MAX_IMPORT_REMINDERS", "500"))
MAX_IMPORT_FILE_BYTES = int(os.getenv("MAX_IMPORT_FILE_BYTES", str(2 * 1024 * 1024)))
AI_DAILY_LIMIT = ai_assistant.AI_DAILY_LIMIT
# Средняя скорость чтения для оценки времени прочтения постов канала.
READING_WORDS_PER_MINUTE = 180
EXPORT_VERSION = 1

USER_FACING_ERROR = "Что-то пошло не так. Попробуй ещё раз позже."


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

_QUICK_RESCHEDULE_ACTION: dict[str, str] = {
    "morning": "morning",
    "day": "day",
    "evening": "evening",
    "later": "now",
    "l": "now",
}

_PERIOD_RU = {"daily": "каждый день", "weekly": "раз в неделю", "monthly": "раз в месяц"}
_PERIOD_TITLES = {"daily": "Ежедневный", "weekly": "Еженедельный", "monthly": "Ежемесячный"}
_WEEKDAY_NOM_RU = {0: "понедельник", 1: "вторник", 2: "среда", 3: "четверг", 4: "пятница", 5: "суббота", 6: "воскресенье"}
_WEEKDAY_ACC_RU = {0: "понедельник", 1: "вторник", 2: "среду", 3: "четверг", 4: "пятницу", 5: "субботу", 6: "воскресенье"}
_WEEKDAY_SHORT_RU = {0: "Пн", 1: "Вт", 2: "Ср", 3: "Чт", 4: "Пт", 5: "Сб", 6: "Вс"}


class ScheduleState(StatesGroup):
    waiting_for_datetime = State()
    waiting_for_time = State()
    waiting_for_minutes = State()


class SaveState(StatesGroup):
    waiting_for_tag = State()
    waiting_for_new_tag = State()


class SubTagState(StatesGroup):
    waiting_for_folder = State()


class ImportState(StatesGroup):
    waiting_for_file = State()


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


def local_now() -> datetime:
    return datetime.now(TZ)


def display_db_datetime(value: str) -> datetime:
    return parse_db_datetime(value).astimezone(TZ)


def get_default_digest_settings(period: str, now: datetime | None = None) -> dict[str, int | str]:
    now = now or local_now()
    return {
        "send_hour": 7,
        "send_minute": 0,
        "weekday": now.weekday(),
        "month_day": now.day,
        "monthly_mode": "date",
    }


def resolve_digest_settings(user_id: int, period: str, now: datetime | None = None) -> dict[str, int | str]:
    defaults = get_default_digest_settings(period, now)
    stored = get_digest_settings(user_id).get(period)
    if stored is None:
        return defaults
    try:
        send_hour = min(max(int(stored["send_hour"]), 0), 23)
    except (TypeError, ValueError):
        send_hour = int(defaults["send_hour"])
    try:
        send_minute = min(max(int(stored["send_minute"]), 0), 59)
    except (TypeError, ValueError):
        send_minute = int(defaults["send_minute"])
    try:
        weekday = min(max(int(stored["weekday"]), 0), 6)
    except (TypeError, ValueError):
        weekday = int(defaults["weekday"])
    try:
        month_day = min(max(int(stored["month_day"]), 1), 31)
    except (TypeError, ValueError):
        month_day = int(defaults["month_day"])
    monthly_mode = stored["monthly_mode"] if stored["monthly_mode"] in {"date", "weekday"} else defaults["monthly_mode"]
    return {
        "send_hour": send_hour,
        "send_minute": send_minute,
        "weekday": weekday,
        "month_day": month_day,
        "monthly_mode": monthly_mode,
    }


def _build_digest_candidate(now: datetime, hour: int, minute: int) -> datetime:
    return now.replace(hour=hour, minute=minute, second=0, microsecond=0)


def _nth_weekday_of_month(year: int, month: int, weekday: int, occurrence: int) -> int:
    month_days = calendar.monthrange(year, month)[1]
    matching_days = [
        day
        for day in range(1, month_days + 1)
        if datetime(year, month, day, tzinfo=TZ).weekday() == weekday
    ]
    if not matching_days:
        return 1
    occurrence_index = min(max(occurrence, 1), len(matching_days)) - 1
    return matching_days[occurrence_index]


def get_next_digest_time(
    period: str,
    now: datetime | None = None,
    settings: dict[str, int | str] | None = None,
) -> datetime:
    now = now or local_now()
    settings = settings or get_default_digest_settings(period, now)
    send_hour = int(settings.get("send_hour", 7))
    send_minute = int(settings.get("send_minute", 0))

    if period == "daily":
        candidate = _build_digest_candidate(now, send_hour, send_minute)
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate

    if period == "weekly":
        target_weekday = int(settings.get("weekday", now.weekday()))
        days_ahead = (target_weekday - now.weekday()) % 7
        candidate = _build_digest_candidate(now + timedelta(days=days_ahead), send_hour, send_minute)
        if candidate <= now:
            candidate += timedelta(days=7)
        return candidate

    if period == "monthly":
        month_day = min(max(int(settings.get("month_day", now.day)), 1), 31)
        monthly_mode = str(settings.get("monthly_mode", "date"))

        def build_candidate(year: int, month: int) -> datetime:
            if monthly_mode == "weekday":
                target_weekday = int(settings.get("weekday", now.weekday()))
                occurrence = ((month_day - 1) // 7) + 1
                day = _nth_weekday_of_month(year, month, target_weekday, occurrence)
            else:
                day = min(month_day, calendar.monthrange(year, month)[1])
            return datetime(year, month, day, send_hour, send_minute, tzinfo=TZ)

        candidate = build_candidate(now.year, now.month)
        if candidate <= now:
            year = now.year + (1 if now.month == 12 else 0)
            month = 1 if now.month == 12 else now.month + 1
            candidate = build_candidate(year, month)
        return candidate

    raise ValueError(f"Unsupported digest period: {period}")


def format_digest_schedule(period: str, settings: dict[str, int | str]) -> str:
    time_part = f"{int(settings['send_hour']):02d}:{int(settings['send_minute']):02d}"
    if period == "daily":
        return f"каждый день в {time_part}"
    if period == "weekly":
        weekday = _WEEKDAY_ACC_RU[int(settings["weekday"])]
        return f"в {weekday} в {time_part}"
    weekday = _WEEKDAY_NOM_RU[int(settings["weekday"])]
    month_day = int(settings["month_day"])
    if settings.get("monthly_mode") == "weekday":
        occurrence = ((month_day - 1) // 7) + 1
        return f"{occurrence}-й {weekday} месяца в {time_part}"
    return f"{month_day}-го числа в {time_part}"


def build_digest_channel_actions(sub_id: int, current_period: str, is_paused: bool = False) -> InlineKeyboardMarkup:
    if is_paused:
        toggle = InlineKeyboardButton(text="▶️ Возобновить", callback_data=f"dresume_{sub_id}")
    else:
        toggle = InlineKeyboardButton(text="⏸ Пауза", callback_data=f"dpause_{sub_id}")
    rows = [
        [
            InlineKeyboardButton(text="🕒 Изменить время", callback_data=f"dsch_{current_period}"),
            toggle,
        ],
        [InlineKeyboardButton(text="📁 Папка", callback_data=f"dtag_{sub_id}")],
    ]
    move_buttons = []
    for period in ("daily", "weekly", "monthly"):
        if period == current_period:
            continue
        move_buttons.append(
            InlineKeyboardButton(
                text=f"↪️ {_PERIOD_TITLES[period]}",
                callback_data=f"dmove_{sub_id}_{period}",
            )
        )
    rows.append(move_buttons)
    rows.append([InlineKeyboardButton(text="🚫 Отписаться", callback_data=f"unsub_{sub_id}")])
    rows.append([InlineKeyboardButton(text="⚙️ Все подписки", callback_data="digest_settings")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _subscription_status_marker(is_paused, digest_status) -> str:
    if is_paused:
        return "⏸"
    if digest_status in ("failed_temporary", "failed_permanent"):
        return "⚠️"
    return "📰"


def build_post_subscribe_keyboard(sub_id: int, period: str) -> InlineKeyboardMarkup:
    """Quick actions shown right after a channel is added to a digest."""
    rows = [
        [InlineKeyboardButton(text="🕒 Изменить время", callback_data=f"dsch_{period}")],
    ]
    move_buttons = [
        InlineKeyboardButton(
            text=f"↪️ {_PERIOD_TITLES[other]}",
            callback_data=f"dmove_{sub_id}_{other}",
        )
        for other in ("daily", "weekly", "monthly")
        if other != period
    ]
    rows.append(move_buttons)
    rows.append([InlineKeyboardButton(text="📁 Папка", callback_data=f"dtag_{sub_id}")])
    rows.append([InlineKeyboardButton(text="⚙️ Все подписки", callback_data="digest_settings")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _sort_subscriptions_for_display(user_subs):
    return sorted(
        user_subs,
        key=lambda s: (
            (s[6] or "￿").lower(),  # tag/folder, untagged last
            (s[2] or "").lower(),
        ),
    )


def build_digest_settings_keyboard(user_subs) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="⏰ Ежедневный", callback_data="dsch_daily"),
            InlineKeyboardButton(text="🗓 Еженедельный", callback_data="dsch_weekly"),
        ],
        [
            InlineKeyboardButton(text="📆 Ежемесячный", callback_data="dsch_monthly"),
        ],
    ]
    if user_subs:
        rows.append([InlineKeyboardButton(text="🧠 Анализ каналов (ИИ)", callback_data="digest_ai")])
        has_active = any(not s[5] for s in user_subs)
        has_paused = any(s[5] for s in user_subs)
        bulk_row = []
        if has_active:
            bulk_row.append(InlineKeyboardButton(text="⏸ Пауза всех", callback_data="dpauseall"))
        if has_paused:
            bulk_row.append(InlineKeyboardButton(text="▶️ Возобновить все", callback_data="dresumeall"))
        if bulk_row:
            rows.append(bulk_row)
        rows.append([InlineKeyboardButton(text="🗑 Отписаться от всех", callback_data="dunsuball")])
    rows.append([
        InlineKeyboardButton(text="📤 Экспорт", callback_data="data_export"),
        InlineKeyboardButton(text="📥 Импорт", callback_data="data_import"),
    ])
    for sub in _sort_subscriptions_for_display(user_subs):
        title = sub[2] or "Канал"
        marker = _subscription_status_marker(sub[5], sub[7])
        short_title = title[:22] + "…" if len(title) > 23 else title
        rows.append(
            [InlineKeyboardButton(text=f"{marker} {short_title}", callback_data=f"dsub_{sub[0]}")]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def render_digest_settings_text(user_id: int) -> str:
    user_subs = get_user_subscriptions(user_id)

    lines = ["⚙️ <b>Настройки дайджеста</b>", ""]
    lines.append("<b>Расписание:</b>")
    for period in ("daily", "weekly", "monthly"):
        settings = resolve_digest_settings(user_id, period)
        lines.append(
            f"• <b>{_PERIOD_TITLES[period]}</b>: {format_digest_schedule(period, settings)}"
        )
    lines.append("")

    if not user_subs:
        lines.append("<b>Подписки:</b> пока пусто.")
        lines.append("")
        lines.append("💡 Перешли пост из открытого канала, чтобы подписаться.")
        return "\n".join(lines).strip()

    paused_count = sum(1 for s in user_subs if s[5])
    failed_count = sum(1 for s in user_subs if s[7] in ("failed_temporary", "failed_permanent"))
    summary = f"<b>Подписки ({len(user_subs)}"
    if paused_count:
        summary += f", ⏸ {paused_count}"
    if failed_count:
        summary += f", ⚠️ {failed_count}"
    summary += "):</b>"
    lines.append(summary)

    grouped: dict[str, list] = {}
    for sub in _sort_subscriptions_for_display(user_subs):
        folder = sub[6] or "Без папки"
        grouped.setdefault(folder, []).append(sub)

    for folder, subs in grouped.items():
        lines.append(f"\n📁 <b>{html.escape(folder)}</b>")
        for sub in subs:
            marker = _subscription_status_marker(sub[5], sub[7])
            title = html.escape(sub[2]) if sub[2] else "Канал"
            note = _PERIOD_RU.get(sub[3], sub[3])
            if sub[5]:
                note = "на паузе"
            elif sub[7] in ("failed_temporary", "failed_permanent"):
                note = "ошибка чтения"
            lines.append(f"• {marker} {title} <i>({note})</i>")
    return "\n".join(lines).strip()


def build_digest_schedule_keyboard(period: str) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="🕒 Время", callback_data=f"dtime_{period}")]]
    if period in {"weekly", "monthly"}:
        rows.append([InlineKeyboardButton(text="📅 День недели", callback_data=f"dweekday_{period}")])
    if period == "monthly":
        rows.append(
            [
                InlineKeyboardButton(text="📆 Дата", callback_data="dmday_monthly"),
                InlineKeyboardButton(text="🔀 Режим", callback_data="dmode_monthly"),
            ]
        )
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="digest_settings")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def render_digest_schedule_text(user_id: int, period: str) -> str:
    settings = resolve_digest_settings(user_id, period)
    lines = [
        f"⚙️ <b>{_PERIOD_TITLES[period]} дайджест</b>",
        "",
        f"Сейчас: {format_digest_schedule(period, settings)}",
    ]
    if period == "monthly":
        mode = "по дате" if settings.get("monthly_mode") == "date" else "по дню недели"
        lines.append(f"Режим: {mode}")
        lines.append("Для режима по дню недели номер недели берётся из даты: 1-7, 8-14, 15-21, 22-28, 29-31.")
    return "\n".join(lines)


def build_digest_time_hours_keyboard(period: str) -> InlineKeyboardMarkup:
    rows = []
    for start in range(0, 24, 4):
        rows.append(
            [
                InlineKeyboardButton(text=f"{hour:02d}", callback_data=f"dth_{period}_{hour}")
                for hour in range(start, min(start + 4, 24))
            ]
        )
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"dsch_{period}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_digest_time_minutes_keyboard(period: str, hour: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="00", callback_data=f"dtm_{period}_{hour}_0"),
                InlineKeyboardButton(text="15", callback_data=f"dtm_{period}_{hour}_15"),
                InlineKeyboardButton(text="30", callback_data=f"dtm_{period}_{hour}_30"),
                InlineKeyboardButton(text="45", callback_data=f"dtm_{period}_{hour}_45"),
            ],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"dtime_{period}")],
        ]
    )


def build_digest_weekday_keyboard(period: str) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text=_WEEKDAY_SHORT_RU[0], callback_data=f"dwd_{period}_0"),
            InlineKeyboardButton(text=_WEEKDAY_SHORT_RU[1], callback_data=f"dwd_{period}_1"),
            InlineKeyboardButton(text=_WEEKDAY_SHORT_RU[2], callback_data=f"dwd_{period}_2"),
            InlineKeyboardButton(text=_WEEKDAY_SHORT_RU[3], callback_data=f"dwd_{period}_3"),
        ],
        [
            InlineKeyboardButton(text=_WEEKDAY_SHORT_RU[4], callback_data=f"dwd_{period}_4"),
            InlineKeyboardButton(text=_WEEKDAY_SHORT_RU[5], callback_data=f"dwd_{period}_5"),
            InlineKeyboardButton(text=_WEEKDAY_SHORT_RU[6], callback_data=f"dwd_{period}_6"),
        ],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"dsch_{period}")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_digest_monthday_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for start in range(1, 32, 7):
        rows.append(
            [
                InlineKeyboardButton(text=str(day), callback_data=f"dmd_monthly_{day}")
                for day in range(start, min(start + 7, 32))
            ]
        )
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="dsch_monthly")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_digest_monthly_mode_keyboard(current_mode: str) -> InlineKeyboardMarkup:
    date_label = "✅ По дате" if current_mode == "date" else "По дате"
    weekday_label = "✅ По дню недели" if current_mode == "weekday" else "По дню недели"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=date_label, callback_data="dmm_monthly_date"),
                InlineKeyboardButton(text=weekday_label, callback_data="dmm_monthly_weekday"),
            ],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="dsch_monthly")],
        ]
    )


async def edit_or_answer(
    callback: CallbackQuery,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
):
    callback_message = get_callback_message(callback)
    if callback_message is None:
        await callback.answer("❌ Сообщение недоступно.", show_alert=True)
        return
    try:
        await callback_message.edit_text(text, parse_mode="HTML", reply_markup=reply_markup)
    except TelegramBadRequest:
        await callback_message.answer(text, parse_mode="HTML", reply_markup=reply_markup)


def reschedule_user_period(user_id: int, period: str):
    settings = resolve_digest_settings(user_id, period)
    next_send_at = serialize_datetime(get_next_digest_time(period, local_now(), settings))
    update_subscriptions_next_send_at(user_id, period, next_send_at)
    return next_send_at


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
                InlineKeyboardButton(text="🗓 Выбрать", callback_data=f"{prefix}_custom"),
            ],
        ]
    )


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


def build_sent_reminder_actions_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="⏰ Отложить", callback_data="sent_later"),
                InlineKeyboardButton(text="🗑 Удалить", callback_data="sent_delete"),
            ],
            [
                InlineKeyboardButton(text="📁 Сохранить", callback_data="sent_save"),
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


def build_scheduled_time_from_state(data: dict, *, hour: int, minute: int) -> datetime | None:
    selected_date = data.get("selected_date")
    if not selected_date:
        return None
    try:
        scheduled_time = datetime.strptime(selected_date, "%Y-%m-%d").replace(
            hour=hour,
            minute=minute,
            second=0,
            microsecond=0,
            tzinfo=TZ,
        )
    except (TypeError, ValueError):
        return None
    return scheduled_time


def has_schedule_context(data: dict) -> bool:
    return bool(data.get("message_id") or (data.get("scheduled_db_id") and data.get("source_message_id")) or data.get("is_bookmark_scheduling"))


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


def append_digest_channel_lines(lines: list[str], sub_id: int, period: str, channel_title: str | None, posts: list[dict]):
    unsubscribe_link = build_digest_action_link("Отписаться", f"du_{sub_id}")
    lines.append(f"📌 <b>{html.escape(channel_title) if channel_title else 'Канал'}</b>  {unsubscribe_link}")
    for post in posts:
        text_safe = html.escape(post["text"])
        lines.append(f"🔹 <i>{text_safe}</i> <a href='{post['link']}'>[Читать]</a>\n")

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
    for section in sections:
        append_digest_channel_lines(
            lines,
            section["sub_id"],
            section["period"],
            section["title"],
            section["posts"],
        )
    lines.append(build_digest_action_link("⚙️ Настройки дайджеста", "ds"))
    return lines


async def deliver_digest(user_id: int, title_plain: str, sections: list[dict]) -> bool:
    """Send a digest, switching to Telegraph for large ones. Returns True if delivered."""
    sections = [s for s in sections if s["posts"]]
    total_posts = sum(len(s["posts"]) for s in sections)
    if total_posts == 0:
        return False

    if total_posts >= DIGEST_TELEGRAPH_THRESHOLD:
        url = await publish_digest(title_plain, sections)
        if url:
            settings_link = build_digest_action_link("⚙️ Настройки дайджеста", "ds")
            text = (
                f"📰 <b>{html.escape(title_plain)}</b> ☕️\n"
                f"{len(sections)} каналов · {total_posts} постов\n\n"
                f"📖 <a href='{url}'>Открыть дайджест целиком</a>\n\n"
                f"{settings_link}"
            )
            await bot.send_message(
                user_id,
                text,
                parse_mode="HTML",
                link_preview_options=LinkPreviewOptions(is_disabled=False),
            )
            return True
        logger.warning("Telegraph publish failed for user %s, falling back to chunks", user_id)

    await send_digest_chunks(user_id, render_digest_lines(title_plain, sections))
    return True


async def fetch_subscription_posts(
    sub: tuple,
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore,
    now_str: str,
    prefetched_channels: dict[str, dict] | None = None,
):
    sub_id, user_id, username, title, period, last_scraped, failure_count, last_post_id = sub
    title_safe = html.escape(title) if title else "Канал"

    async with semaphore:
        try:
            settings = resolve_digest_settings(user_id, period)
            next_send_str = serialize_datetime(get_next_digest_time(period, local_now(), settings))
            prefetched = (prefetched_channels or {}).get(normalize_channel_username(username))
            if prefetched is not None:
                if prefetched.get("error") is not None:
                    raise prefetched["error"]
                fetched_posts = prefetched["posts"]
                source_name = prefetched["source"]
            else:
                fetched_posts, source_name = await channel_source.fetch(
                    username,
                    last_scraped,
                    web_session=session,
                )
                upsert_channel_posts(username, fetched_posts, source_name)
            posts = get_channel_posts_since(username, last_scraped, last_post_id)
            if fetched_posts and not posts:
                # Compatibility for a malformed legacy link without a numeric post id.
                posts = fetched_posts
            delivered_marker = max(
                (post["time"] for post in posts),
                default=parse_db_datetime(last_scraped),
            )
            delivered_post_id = max(
                (post["id"] for post in posts if post.get("id") is not None),
                default=last_post_id,
            )
            return {
                "status": "ok",
                "sub_id": sub_id,
                "period": period,
                "username": username,
                "title": title,
                "title_safe": title_safe,
                "posts": posts,
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
                posts, source_name = await channel_source.fetch(
                    username,
                    marker,
                    web_session=session,
                )
                upsert_channel_posts(username, posts, source_name)
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
    user_id = message.chat.id

    if payload == "ds":
        await message.answer(
            render_digest_settings_text(user_id),
            parse_mode="HTML",
            reply_markup=build_digest_settings_keyboard(get_user_subscriptions(user_id)),
        )
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
        add_subscription(
            user_id,
            sub["channel_username"],
            sub["channel_title"],
            new_period,
            sub["last_scraped_at"] or serialize_datetime(utc_now()),
            next_send_at,
        )
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


WELCOME_TEXT = (
    "Привет! Я готов.\n\n"
    "1️⃣ Перешли мне любое сообщение, чтобы отложить его или сохранить в базу знаний.\n"
    "2️⃣ Перешли пост из открытого канала, чтобы подписаться на его дайджест.\n"
    "3️⃣ Напиши /list для задач, /list_digest для дайджестов или /saved для Избранного.\n"
    "4️⃣ Для уже доставленного напоминания используй кнопки под ним или ответь командой: "
    "/morning, /day, /evening, /later (/l), /at ДД.ММ.ГГГГ ЧЧ:ММ, /save (/s), /delete (/d)."
)


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
            await target.message.answer(WELCOME_TEXT)
            await target.answer()
        else:
            await target.answer(WELCOME_TEXT)


@dp.message(Command("home"), StateFilter("*"))
async def cmd_home(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(WELCOME_TEXT, parse_mode="HTML")


@dp.message(Command("back"), StateFilter("*"))
async def cmd_back(message: types.Message, state: FSMContext):
    prev = await pop_screen(state)
    if prev:
        await render_screen(message.chat.id, prev, message, state)
    else:
        await state.clear()
        await message.answer(WELCOME_TEXT, parse_mode="HTML")


@dp.callback_query(F.data == "flow_home")
async def callback_home(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await callback.message.edit_text(WELCOME_TEXT, parse_mode="HTML")
    except TelegramAPIError:
        await callback.message.answer(WELCOME_TEXT, parse_mode="HTML")
        try:
            await callback.message.delete()
        except TelegramAPIError:
            pass
    await callback.answer()


@dp.callback_query(F.data == "flow_back")
async def callback_back(callback: types.CallbackQuery, state: FSMContext):
    prev = await pop_screen(state)
    if prev:
        await render_screen(callback.message.chat.id, prev, callback, state)
    else:
        await state.clear()
        try:
            await callback.message.edit_text(WELCOME_TEXT, parse_mode="HTML")
        except TelegramAPIError:
            await callback.message.answer(WELCOME_TEXT, parse_mode="HTML")
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
    await message.answer(
        "Привет! Я готов.\n\n"
        "1️⃣ Перешли мне любое сообщение, чтобы отложить его или сохранить в базу знаний.\n"
        "2️⃣ Перешли пост из открытого канала, чтобы подписаться на его дайджест.\n"
        "3️⃣ Напиши /list для задач, /list_digest для дайджестов или /saved для Избранного.\n"
        "4️⃣ Для уже доставленного напоминания используй кнопки под ним или ответь командой: "
        "/morning, /day, /evening, /later (/l), /at ДД.ММ.ГГГГ ЧЧ:ММ, /save (/s), /delete (/d)."
    )


@dp.message(Command("help"))
async def cmd_help(message: types.Message, state: FSMContext):
    await state.clear()
    help_text = (
        "ℹ️ <b>Справка по командам бота:</b>\n\n"
        "🟢 <b>Основные команды:</b>\n"
        "/start — Перезапустить бота\n"
        "/help — Показать эту справку\n"
        "/home — Сбросить текущее действие и вернуться в главное меню\n"
        "/back — Вернуться на один шаг назад во всех меню\n\n"
        "📅 <b>Напоминания (Задачи):</b>\n"
        "/list — Посмотреть мои активные напоминания\n"
        "💡 <i>Перешлите любое сообщение или напишите текст, чтобы запланировать его.</i>\n\n"
        "✍️ <b>Команды управления (ответом на напоминание):</b>\n"
        "Ответьте на сообщение напоминания одной из команд:\n"
        "• <code>/morning</code> — перенести на завтра на утро\n"
        "• <code>/day</code> — перенести на завтра на день\n"
        "• <code>/evening</code> — перенести на завтра на вечер\n"
        "• <code>/later</code> (или <code>/l</code>) — отложить на 3 часа\n"
        "• <code>/at ДД.ММ.ГГГГ ЧЧ:ММ</code> — отложить на точное время\n"
        "• <code>/save</code> (или <code>/s</code>) — сохранить в базу знаний\n"
        "• <code>/delete</code> (или <code>/d</code>) — удалить напоминание\n\n"
        "📁 <b>База знаний (Закладки):</b>\n"
        "/saved — Открыть базу знаний с удобными папками и страницами\n"
        "💡 <i>Вы можете переносить закладки в другие категории, превращать в задачи или удалять через меню действий.</i>\n\n"
        "📡 <b>Подписки и Дайджесты:</b>\n"
        "/list_digest — Посмотреть и настроить мои подписки\n"
        "/test_digest — Мгновенно собрать дайджест по подпискам за 24ч\n"
        "/check — Проверить, что все каналы читаются\n"
        "💡 <i>Перешлите пост из любого открытого канала, чтобы подписаться на него.</i>\n"
        "💡 <i>В «Мои подписки» можно ставить каналы на паузу, раскладывать по папкам и спрашивать ИИ про частоту постинга и саммари.</i>\n"
        "ℹ️ <i>Большие дайджесты (4+ постов) приходят одной ссылкой на Telegraph.</i>\n\n"
        "💾 <b>Бэкап и перенос:</b>\n"
        "/export — Выгрузить напоминания, закладки и подписки в JSON\n"
        "/import — Загрузить JSON-файл, чтобы восстановить данные"
    )
    await message.answer(help_text, parse_mode="HTML")


async def set_bot_commands(bot: Bot):
    commands = [
        types.BotCommand(command="start", description="Начать работу"),
        types.BotCommand(command="help", description="ℹ️ Справка по всем командам"),
        types.BotCommand(command="list", description="📅 Мои напоминания"),
        types.BotCommand(command="list_digest", description="📡 Мои подписки"),
        types.BotCommand(command="saved", description="📁 База знаний (закладки)"),
        types.BotCommand(command="test_digest", description="⏳ Собрать дайджест за 24ч"),
        types.BotCommand(command="check", description="🔎 Проверить подписки"),
        types.BotCommand(command="export", description="📤 Экспорт данных (JSON)"),
        types.BotCommand(command="import", description="📥 Импорт данных (JSON)"),
        types.BotCommand(command="home", description="🏠 В главное меню"),
        types.BotCommand(command="back", description="🔙 Шаг назад"),
    ]
    await bot.set_my_commands(commands)


async def start_save_flow(
    message: types.Message,
    state: FSMContext,
    *,
    full_text: str,
    source: str,
    target_message_id: int | None = None,
    scheduled_db_id: int | None = None,
    command_message_id: int | None = None,
    orig_msg_id: int | None = None,
) -> None:
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
        orig_msg_id=orig_msg_id,
        full_text=full_text,
        source=source or "Неизвестно",
        prompt_msg_id=prompt.message_id,
        target_message_id=target_message_id,
        scheduled_db_id=scheduled_db_id,
        command_message_id=command_message_id,
    )
    await state.set_state(SaveState.waiting_for_tag)


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


@dp.message(Command("save", "s"))
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
    await start_save_flow(
        message,
        state,
        full_text=full_text,
        source=source or "Неизвестно",
        target_message_id=delivered_message_id,
        scheduled_db_id=db_id,
        command_message_id=message.message_id,
    )


@dp.message(Command("delete", "d"))
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


@dp.callback_query(F.data == "sent_later")
async def show_sent_reminder_time_actions(callback: CallbackQuery):
    callback_message = get_callback_message(callback)
    if callback_message is None:
        await callback.answer("❌ Сообщение недоступно.")
        return

    scheduled_msg = get_scheduled_message_by_delivered_message_id(
        callback_message.chat.id, callback_message.message_id
    )
    if not scheduled_msg:
        await callback.answer("❌ Напоминание уже обработано.", show_alert=True)
        return

    await callback_message.answer(
        "Когда напомнить снова?",
        reply_markup=build_time_selection_keyboard(f"senttime_{callback_message.message_id}"),
    )
    await callback.answer()


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

    scheduled_msg = get_scheduled_message_by_delivered_message_id(
        callback_message.chat.id, delivered_message_id
    )
    if not scheduled_msg:
        await callback.answer("❌ Напоминание уже обработано.", show_alert=True)
        return

    db_id, source_message_id, _, _, preview, source, _ = scheduled_msg
    now = local_now()

    if action == "custom":
        await state.update_data(
            scheduled_db_id=db_id,
            source_message_id=source_message_id,
            preview=preview or "",
            source=source or "",
            prompt_msg_id=callback_message.message_id,
            target_message_id=delivered_message_id,
            command_message_id=callback_message.message_id,
        )
        await callback_message.edit_text(
            "Выбери дату:",
            reply_markup=build_manual_date_keyboard(),
        )
        await callback.answer()
        return

    scheduled_time, label = get_quick_scheduled_time(action, now)
    if scheduled_time is None:
        await callback.answer("❌ Ошибка при планировании времени.")
        return

    try:
        replace_sent_reminder_with_pending(
            callback_message.chat.id,
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

    for message_id in (delivered_message_id, callback_message.message_id):
        try:
            await bot.delete_message(callback_message.chat.id, message_id)
        except TelegramAPIError:
            pass

    await callback.answer()
    await bot.send_message(callback_message.chat.id, f"✅ Отложил {label}.")


@dp.callback_query(F.data == "sent_save")
async def save_sent_reminder(callback: CallbackQuery, state: FSMContext):
    callback_message = get_callback_message(callback)
    if callback_message is None:
        await callback.answer("❌ Сообщение недоступно.")
        return

    scheduled_msg = get_scheduled_message_by_delivered_message_id(
        callback_message.chat.id, callback_message.message_id
    )
    if not scheduled_msg:
        await callback.answer("❌ Напоминание уже обработано.", show_alert=True)
        return

    db_id, _, delivered_message_id, _, _, source, _ = scheduled_msg
    await start_save_flow(
        callback_message,
        state,
        full_text=get_message_full_text(callback_message),
        source=source or "Неизвестно",
        target_message_id=delivered_message_id,
        scheduled_db_id=db_id,
    )
    await callback.answer()


@dp.callback_query(F.data == "sent_delete")
async def delete_sent_reminder(callback: CallbackQuery):
    callback_message = get_callback_message(callback)
    if callback_message is None:
        await callback.answer("❌ Сообщение недоступно.")
        return

    scheduled_msg = get_scheduled_message_by_delivered_message_id(
        callback_message.chat.id, callback_message.message_id
    )
    if not scheduled_msg:
        await callback.answer("❌ Напоминание уже обработано.", show_alert=True)
        return

    delete_message(callback_message.chat.id, scheduled_msg[0])
    try:
        await callback_message.delete()
    except TelegramAPIError:
        pass
    await callback.answer("Удалено!")


@dp.message(Command("list"))
async def cmd_list(message: types.Message, state: FSMContext):
    await state.clear()
    user_id = message.chat.id
    user_msgs = get_user_messages(user_id)

    if not user_msgs:
        await message.answer("📭 У тебя нет активных напоминаний.\n\nДля дайджестов используй /list_digest.")
        return

    if user_msgs:
        text_lines = ["⏳ <b>Твои разовые напоминания:</b>\n"]
        buttons = []
        for idx, msg in enumerate(user_msgs, 1):
            db_id, send_at, preview, source = msg
            dt_obj = display_db_datetime(send_at)
            source_safe = html.escape(source) if source else "Неизвестно"
            preview_safe = html.escape(preview) if preview else "Без текста"

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


@dp.message(Command("list_digest"))
async def cmd_list_digest(message: types.Message, state: FSMContext):
    await state.clear()
    user_id = message.chat.id
    await message.answer(
        render_digest_settings_text(user_id),
        parse_mode="HTML",
        reply_markup=build_digest_settings_keyboard(get_user_subscriptions(user_id)),
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
        source_safe = html.escape(source) if source else "Неизвестно"
        preview_safe = html.escape(preview) if preview else "Без текста"
        text_lines.append(
            f"{idx}. 📌 <b>{dt_obj.strftime('%d.%m в %H:%M')}</b> | От: {source_safe}\n"
            f"<i>{preview_safe}</i>\n"
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
    try:
        await callback_message.edit_text(
            render_digest_settings_text(callback_message.chat.id),
            parse_mode="HTML",
            reply_markup=build_digest_settings_keyboard(get_user_subscriptions(callback_message.chat.id)),
        )
    except TelegramAPIError:
        pass
    await callback.answer("Удалено!")


async def _refresh_digest_settings(callback: CallbackQuery):
    user_id = callback.from_user.id
    await edit_or_answer(
        callback,
        render_digest_settings_text(user_id),
        reply_markup=build_digest_settings_keyboard(get_user_subscriptions(user_id)),
    )


async def render_subscription_actions(callback: CallbackQuery, sub_id: int) -> bool:
    user_id = callback.from_user.id
    sub = get_subscription_by_id(user_id, sub_id)
    if not sub:
        await callback.answer("❌ Подписка не найдена.", show_alert=True)
        return False
    is_paused = bool(sub["is_paused"])
    folder = sub["tag"] or "Без папки"
    if is_paused:
        status_line = "\n⏸ <i>На паузе</i>"
    elif sub["digest_status"] in ("failed_temporary", "failed_permanent"):
        status_line = "\n⚠️ <i>Последнее чтение канала не удалось</i>"
    else:
        status_line = ""
    text = (
        f"📰 <b>{html.escape(sub['channel_title']) if sub['channel_title'] else 'Канал'}</b>\n"
        f"Периодичность: {_PERIOD_TITLES[sub['period']]}\n"
        f"📁 Папка: {html.escape(folder)}\n"
        f"Следующая отправка: {display_db_datetime(sub['next_send_at']).strftime('%d.%m в %H:%M')}"
        f"{status_line}"
    )
    await edit_or_answer(callback, text, reply_markup=build_digest_channel_actions(sub_id, sub["period"], is_paused))
    return True


@dp.callback_query(F.data == "digest_settings")
async def open_digest_settings(callback: CallbackQuery, state: FSMContext):
    await state.set_state(None)
    await _refresh_digest_settings(callback)
    await callback.answer()


@dp.callback_query(F.data.startswith("dsub_"))
async def open_digest_subscription_actions(callback: CallbackQuery):
    sub_id = parse_callback_int_suffix(callback.data, "dsub_")
    if sub_id is None:
        await callback.answer("❌ Некорректные данные.", show_alert=True)
        return
    if await render_subscription_actions(callback, sub_id):
        await callback.answer()


@dp.callback_query(F.data.startswith("dmove_"))
async def move_digest_subscription(callback: CallbackQuery):
    user_id = callback.from_user.id
    payload = parse_callback_strip_prefix(callback.data, "dmove_")
    if not payload or "_" not in payload:
        await callback.answer("❌ Некорректные данные.", show_alert=True)
        return
    sub_id_str, new_period = payload.split("_", 1)
    try:
        sub_id = int(sub_id_str)
    except ValueError:
        await callback.answer("❌ Некорректные данные.", show_alert=True)
        return

    sub = get_subscription_by_id(user_id, sub_id)
    if not sub:
        await callback.answer("❌ Подписка не найдена.", show_alert=True)
        return
    if new_period == sub["period"]:
        await callback.answer("Этот период уже выбран.", show_alert=True)
        return

    settings = resolve_digest_settings(user_id, new_period)
    next_send_at = serialize_datetime(get_next_digest_time(new_period, local_now(), settings))
    new_sub_id = add_subscription(
        user_id,
        sub["channel_username"],
        sub["channel_title"],
        new_period,
        sub["last_scraped_at"] or serialize_datetime(utc_now()),
        next_send_at,
        tag=sub["tag"],
    )
    if new_sub_id != sub_id:
        delete_subscription(user_id, sub_id)

    await render_subscription_actions(callback, new_sub_id)
    await callback.answer(f"Перенёс в {_PERIOD_TITLES[new_period].lower()} дайджест.")


# ---------------------------------------------------------------------------
# Подписки: пауза/возобновление, массовые операции, папки (Features 1–3)
# ---------------------------------------------------------------------------


@dp.callback_query(F.data.startswith("dpause_"))
async def pause_subscription(callback: CallbackQuery):
    sub_id = parse_callback_int_suffix(callback.data, "dpause_")
    if sub_id is None:
        await callback.answer("❌ Некорректные данные.", show_alert=True)
        return
    set_subscription_paused(callback.from_user.id, sub_id, True)
    await render_subscription_actions(callback, sub_id)
    await callback.answer("⏸ Подписка на паузе.")


@dp.callback_query(F.data.startswith("dresume_"))
async def resume_subscription(callback: CallbackQuery):
    sub_id = parse_callback_int_suffix(callback.data, "dresume_")
    if sub_id is None:
        await callback.answer("❌ Некорректные данные.", show_alert=True)
        return
    user_id = callback.from_user.id
    set_subscription_paused(user_id, sub_id, False)
    # Сдвигаем next_send_at вперёд, чтобы возобновлённая подписка не сработала мгновенно.
    sub = get_subscription_by_id(user_id, sub_id)
    if sub:
        settings = resolve_digest_settings(user_id, sub["period"])
        next_send_at = serialize_datetime(get_next_digest_time(sub["period"], local_now(), settings))
        update_subscription_time(sub_id, sub["last_scraped_at"] or serialize_datetime(utc_now()), next_send_at)
    await render_subscription_actions(callback, sub_id)
    await callback.answer("▶️ Подписка возобновлена.")


@dp.callback_query(F.data == "dpauseall")
async def pause_all_subscriptions(callback: CallbackQuery):
    changed = set_all_subscriptions_paused(callback.from_user.id, True)
    await _refresh_digest_settings(callback)
    await callback.answer(f"⏸ На паузе: {changed}")


@dp.callback_query(F.data == "dresumeall")
async def resume_all_subscriptions(callback: CallbackQuery):
    user_id = callback.from_user.id
    changed = set_all_subscriptions_paused(user_id, False)
    # Пересчитываем расписание для всех периодов после возобновления.
    for period in ("daily", "weekly", "monthly"):
        reschedule_user_period(user_id, period)
    await _refresh_digest_settings(callback)
    await callback.answer(f"▶️ Возобновлено: {changed}")


@dp.callback_query(F.data == "dunsuball")
async def confirm_unsubscribe_all(callback: CallbackQuery):
    count = count_user_subscriptions(callback.from_user.id)
    if not count:
        await callback.answer("У тебя нет подписок.", show_alert=True)
        return
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"🗑 Да, отписаться от всех ({count})", callback_data="dunsuball_yes")],
            [InlineKeyboardButton(text="⬅️ Отмена", callback_data="digest_settings")],
        ]
    )
    await edit_or_answer(
        callback,
        f"⚠️ <b>Отписаться от всех каналов?</b>\nБудут удалены все {count} подписок. Действие необратимо.",
        reply_markup=keyboard,
    )
    await callback.answer()


@dp.callback_query(F.data == "dunsuball_yes")
async def unsubscribe_all_confirmed(callback: CallbackQuery):
    removed = unsubscribe_all(callback.from_user.id)
    await _refresh_digest_settings(callback)
    await callback.answer(f"🗑 Удалено подписок: {removed}")


def build_subscription_folder_keyboard(sub_id: int, folders: list[str]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for idx, folder in enumerate(folders):
        display = folder[:15] + "…" if len(folder) > 16 else folder
        row.append(InlineKeyboardButton(text=f"📁 {display}", callback_data=f"dtagset_{sub_id}_{idx}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="✍️ Новая папка", callback_data=f"dtagnew_{sub_id}")])
    rows.append([InlineKeyboardButton(text="🗑 Убрать из папки", callback_data=f"dtagclear_{sub_id}")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"dsub_{sub_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@dp.callback_query(F.data.startswith("dtag_"))
async def open_subscription_folder_menu(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    sub_id = parse_callback_int_suffix(callback.data, "dtag_")
    if sub_id is None:
        await callback.answer("❌ Некорректные данные.", show_alert=True)
        return
    sub = get_subscription_by_id(user_id, sub_id)
    if not sub:
        await callback.answer("❌ Подписка не найдена.", show_alert=True)
        return
    folders = get_subscription_tags(user_id)
    await state.update_data(folder_sub_id=sub_id, folder_list=folders)
    current = sub["tag"] or "Без папки"
    text = (
        f"📁 <b>Папка для канала</b>\n"
        f"{html.escape(sub['channel_title']) if sub['channel_title'] else 'Канал'}\n\n"
        f"Текущая папка: <b>{html.escape(current)}</b>\n\n"
        f"Выбери папку, создай новую (✍️) или убери из папки:"
    )
    await edit_or_answer(callback, text, reply_markup=build_subscription_folder_keyboard(sub_id, folders))
    await callback.answer()


@dp.callback_query(F.data.startswith("dtagset_"))
async def set_subscription_folder(callback: CallbackQuery, state: FSMContext):
    payload = parse_callback_strip_prefix(callback.data, "dtagset_")
    if not payload or "_" not in payload:
        await callback.answer("❌ Некорректные данные.", show_alert=True)
        return
    sub_id_str, idx_str = payload.rsplit("_", 1)
    try:
        sub_id = int(sub_id_str)
        idx = int(idx_str)
    except ValueError:
        await callback.answer("❌ Некорректные данные.", show_alert=True)
        return
    data = await state.get_data()
    folders = data.get("folder_list", [])
    if idx >= len(folders):
        await callback.answer("❌ Папка не найдена.", show_alert=True)
        return
    set_subscription_tag(callback.from_user.id, sub_id, folders[idx])
    await render_subscription_actions(callback, sub_id)
    await callback.answer(f"📁 Папка: {folders[idx]}")


@dp.callback_query(F.data.startswith("dtagclear_"))
async def clear_subscription_folder(callback: CallbackQuery):
    sub_id = parse_callback_int_suffix(callback.data, "dtagclear_")
    if sub_id is None:
        await callback.answer("❌ Некорректные данные.", show_alert=True)
        return
    set_subscription_tag(callback.from_user.id, sub_id, None)
    await render_subscription_actions(callback, sub_id)
    await callback.answer("📁 Убрано из папки.")


@dp.callback_query(F.data.startswith("dtagnew_"))
async def prompt_new_subscription_folder(callback: CallbackQuery, state: FSMContext):
    sub_id = parse_callback_int_suffix(callback.data, "dtagnew_")
    if sub_id is None:
        await callback.answer("❌ Некорректные данные.", show_alert=True)
        return
    await state.update_data(folder_sub_id=sub_id)
    await state.set_state(SubTagState.waiting_for_folder)
    await edit_or_answer(
        callback,
        "✍️ Пришли название новой папки текстом (например: Новости, Работа).",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отмена", callback_data=f"dsub_{sub_id}")]]
        ),
    )
    await callback.answer()


@dp.message(SubTagState.waiting_for_folder)
async def process_new_subscription_folder(message: types.Message, state: FSMContext):
    folder = (message.text or "").strip().lstrip("#").strip()
    data = await state.get_data()
    sub_id = data.get("folder_sub_id")
    if not folder:
        await message.answer("❌ Название папки не должно быть пустым. Попробуй ещё раз.")
        return
    if not sub_id:
        await message.answer("❌ Не нашёл подписку. Открой /list_digest заново.")
        await state.set_state(None)
        return
    set_subscription_tag(message.chat.id, sub_id, folder)
    await state.set_state(None)
    confirm = await message.answer(f"📁 Канал перенесён в папку <b>{html.escape(folder)}</b>.", parse_mode="HTML")
    await asyncio.sleep(2)
    try:
        await confirm.delete()
        await message.delete()
    except TelegramAPIError:
        pass
    await message.answer(
        render_digest_settings_text(message.chat.id),
        parse_mode="HTML",
        reply_markup=build_digest_settings_keyboard(get_user_subscriptions(message.chat.id)),
    )


@dp.callback_query(F.data.startswith("dsch_"))
async def open_digest_schedule(callback: CallbackQuery):
    period = parse_callback_strip_prefix(callback.data, "dsch_")
    if period is None:
        await callback.answer("❌ Некорректные данные.", show_alert=True)
        return
    await edit_or_answer(
        callback,
        render_digest_schedule_text(callback.from_user.id, period),
        reply_markup=build_digest_schedule_keyboard(period),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("dtime_"))
async def open_digest_time_picker(callback: CallbackQuery):
    period = parse_callback_strip_prefix(callback.data, "dtime_")
    if period is None:
        await callback.answer("❌ Некорректные данные.", show_alert=True)
        return
    await edit_or_answer(
        callback,
        f"🕒 <b>{_PERIOD_TITLES[period]} дайджест</b>\n\nВыбери час отправки:",
        reply_markup=build_digest_time_hours_keyboard(period),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("dth_"))
async def open_digest_minute_picker(callback: CallbackQuery):
    payload = parse_callback_strip_prefix(callback.data, "dth_")
    if not payload or "_" not in payload:
        await callback.answer("❌ Некорректные данные.", show_alert=True)
        return
    period, hour_str = payload.rsplit("_", 1)
    try:
        hour = int(hour_str)
    except ValueError:
        await callback.answer("❌ Некорректные данные.", show_alert=True)
        return
    await edit_or_answer(
        callback,
        f"🕒 <b>{_PERIOD_TITLES[period]} дайджест</b>\n\nЧас: {hour:02d}. Выбери минуты:",
        reply_markup=build_digest_time_minutes_keyboard(period, hour),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("dtm_"))
async def save_digest_time(callback: CallbackQuery):
    payload = parse_callback_strip_prefix(callback.data, "dtm_")
    if not payload:
        await callback.answer("❌ Некорректные данные.", show_alert=True)
        return
    parts = payload.split("_")
    if len(parts) != 3:
        await callback.answer("❌ Некорректные данные.", show_alert=True)
        return
    period, hour_str, minute_str = parts
    try:
        hour = int(hour_str)
        minute = int(minute_str)
    except ValueError:
        await callback.answer("❌ Некорректные данные.", show_alert=True)
        return
    upsert_digest_settings(callback.from_user.id, period, send_hour=hour, send_minute=minute)
    reschedule_user_period(callback.from_user.id, period)
    await edit_or_answer(
        callback,
        render_digest_schedule_text(callback.from_user.id, period),
        reply_markup=build_digest_schedule_keyboard(period),
    )
    await callback.answer(f"Время обновлено: {hour:02d}:{minute:02d}")


@dp.callback_query(F.data.startswith("dweekday_"))
async def open_digest_weekday_picker(callback: CallbackQuery):
    period = parse_callback_strip_prefix(callback.data, "dweekday_")
    if period is None:
        await callback.answer("❌ Некорректные данные.", show_alert=True)
        return
    await edit_or_answer(
        callback,
        f"📅 <b>{_PERIOD_TITLES[period]} дайджест</b>\n\nВыбери день недели:",
        reply_markup=build_digest_weekday_keyboard(period),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("dwd_"))
async def save_digest_weekday(callback: CallbackQuery):
    payload = parse_callback_strip_prefix(callback.data, "dwd_")
    if not payload:
        await callback.answer("❌ Некорректные данные.", show_alert=True)
        return
    period, weekday_str = payload.rsplit("_", 1)
    try:
        weekday = int(weekday_str)
    except ValueError:
        await callback.answer("❌ Некорректные данные.", show_alert=True)
        return
    upsert_digest_settings(callback.from_user.id, period, weekday=weekday)
    reschedule_user_period(callback.from_user.id, period)
    await edit_or_answer(
        callback,
        render_digest_schedule_text(callback.from_user.id, period),
        reply_markup=build_digest_schedule_keyboard(period),
    )
    await callback.answer(f"День обновлён: {_WEEKDAY_ACC_RU[weekday]}.")


@dp.callback_query(F.data == "dmday_monthly")
async def open_digest_monthday_picker(callback: CallbackQuery):
    await edit_or_answer(
        callback,
        "📆 <b>Ежемесячный дайджест</b>\n\nВыбери дату месяца:",
        reply_markup=build_digest_monthday_keyboard(),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("dmd_monthly_"))
async def save_digest_monthday(callback: CallbackQuery):
    day = parse_callback_int_suffix(callback.data, "dmd_monthly_")
    if day is None:
        await callback.answer("❌ Некорректные данные.", show_alert=True)
        return
    upsert_digest_settings(callback.from_user.id, "monthly", month_day=day)
    reschedule_user_period(callback.from_user.id, "monthly")
    await edit_or_answer(
        callback,
        render_digest_schedule_text(callback.from_user.id, "monthly"),
        reply_markup=build_digest_schedule_keyboard("monthly"),
    )
    await callback.answer(f"Дата обновлена: {day}.")


@dp.callback_query(F.data == "dmode_monthly")
async def open_digest_monthly_mode_picker(callback: CallbackQuery):
    settings = resolve_digest_settings(callback.from_user.id, "monthly")
    await edit_or_answer(
        callback,
        "🔀 <b>Ежемесячный дайджест</b>\n\nВыбери режим отправки:",
        reply_markup=build_digest_monthly_mode_keyboard(str(settings.get("monthly_mode", "date"))),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("dmm_monthly_"))
async def save_digest_monthly_mode(callback: CallbackQuery):
    mode = parse_callback_strip_prefix(callback.data, "dmm_monthly_")
    if mode not in {"date", "weekday"}:
        await callback.answer("❌ Некорректные данные.", show_alert=True)
        return
    upsert_digest_settings(callback.from_user.id, "monthly", monthly_mode=mode)
    reschedule_user_period(callback.from_user.id, "monthly")
    await edit_or_answer(
        callback,
        render_digest_schedule_text(callback.from_user.id, "monthly"),
        reply_markup=build_digest_schedule_keyboard("monthly"),
    )
    await callback.answer("Режим обновлён.")


# ---------------------------------------------------------------------------
# Проверка подписок (Feature 2)
# ---------------------------------------------------------------------------


@dp.message(Command("check"))
async def cmd_check(message: types.Message, state: FSMContext):
    await state.clear()
    user_id = message.chat.id
    subs = get_user_subscriptions(user_id)
    if not subs:
        await message.answer("📭 У тебя нет подписок для проверки.")
        return

    status_msg = await message.answer("🔎 Проверяю доступность каналов...")
    semaphore = asyncio.Semaphore(DIGEST_FETCH_CONCURRENCY)
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
    return local_now().strftime("%Y-%m-%d")


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
    user_id = callback.from_user.id
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
    idx, username, title = await _resolve_ai_channel(callback, state, "aich_")
    if username is None:
        return
    limit_note = ""
    if ai_assistant.is_enabled() and AI_DAILY_LIMIT > 0:
        used = get_ai_usage_today(callback.from_user.id, _ai_today_key())
        limit_note = f"\n\n🤖 ИИ-запросов сегодня: {used}/{AI_DAILY_LIMIT}"
    text = f"📊 <b>{html.escape(title or username)}</b>\n\nЧто сделать с этим каналом?{limit_note}"
    await edit_or_answer(callback, text, reply_markup=build_ai_channel_menu(idx))
    await callback.answer()


@dp.callback_query(F.data.startswith("aiadj_"))
async def ai_adjust_digest(callback: CallbackQuery, state: FSMContext):
    idx, username, title = await _resolve_ai_channel(callback, state, "aiadj_")
    if username is None:
        return
    user_id = callback.from_user.id
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
    idx, username, title = await _resolve_ai_channel(callback, state, "aisum_")
    if username is None:
        return
    user_id = callback.from_user.id

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
    caption = (
        "📤 <b>Экспорт данных Unagi</b>\n"
        f"• Подписки: {len(data['subscriptions'])}\n"
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
    await _send_export(message.chat.id)


@dp.callback_query(F.data == "data_export")
async def cb_export(callback: CallbackQuery):
    await callback.answer("📤 Готовлю файл...")
    await _send_export(callback.from_user.id)


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
    await _prompt_import(message.chat.id, state)


@dp.callback_query(F.data == "data_import")
async def cb_import(callback: CallbackQuery, state: FSMContext):
    await _prompt_import(callback.from_user.id, state)
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
    bookmarks = payload.get("bookmarks") or []
    reminders = payload.get("reminders") or []
    digest_settings = payload.get("digest_settings") or []
    now = local_now()
    added_subs = added_bms = added_rems = 0

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
        if not username:
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

    for bookmark in bookmarks[:MAX_IMPORT_BOOKMARKS]:
        text = bookmark.get("full_text")
        if not text:
            continue
        try:
            add_saved_message(
                user_id,
                text,
                bookmark.get("source_name") or "Импорт",
                bookmark.get("tag") or "Импорт",
                bookmark.get("saved_at") or serialize_datetime(utc_now()),
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
            add_message(
                user_id,
                None,
                send_at,
                reminder.get("text_preview") or "",
                reminder.get("source_name") or "Импорт",
            )
            added_rems += 1
        except Exception:
            logger.exception("Import: failed to add reminder")

    return (
        "✅ <b>Импорт завершён</b>\n"
        f"• Подписки: +{added_subs}\n"
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

    report = _import_user_payload(message.chat.id, payload)
    await message.answer(report, parse_mode="HTML")


@dp.message(ImportState.waiting_for_file)
async def import_wrong_type(message: types.Message):
    await message.answer("📎 Пришли именно файл .json (как документ), либо нажми «Отмена».")


async def render_kb_tags_screen(chat_id: int, target: types.Message | types.CallbackQuery, state: FSMContext):
    await state.set_state(None)
    saved_msgs = get_saved_messages(chat_id)
    if not saved_msgs:
        text = "📭 Твоя база знаний пока пуста."
        if isinstance(target, CallbackQuery):
            await target.message.edit_text(text, parse_mode="HTML")
            await target.answer()
        else:
            await target.answer(text, parse_mode="HTML")
        return

    grouped = {}
    for item in saved_msgs:
        tag = item[1] or "Без тега"
        grouped.setdefault(tag, []).append(item)

    sorted_tags = sorted(grouped.keys(), key=lambda t: t.lower())
    await state.update_data(kb_tags_list=sorted_tags)

    total_count = len(saved_msgs)
    text = f"📂 <b>База знаний</b> (всего закладок: {total_count})\n\nВыберите категорию для просмотра закладок:"
    
    buttons = []
    row = []
    for idx, tag in enumerate(sorted_tags):
        count = len(grouped[tag])
        display_tag = tag[:15] + "..." if len(tag) > 18 else tag
        btn = InlineKeyboardButton(text=f"🏷 {display_tag} ({count})", callback_data=f"kb_tag_idx_{idx}")
        row.append(btn)
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
        
    buttons.append([InlineKeyboardButton(text="📁 Показать все закладки", callback_data="kb_tag_all")])
    buttons.append([InlineKeyboardButton(text="🏠 В главное меню", callback_data="flow_home")])
    
    markup = InlineKeyboardMarkup(inline_keyboard=buttons)
    
    if isinstance(target, CallbackQuery):
        await target.message.edit_text(text, parse_mode="HTML", reply_markup=markup)
        await target.answer()
    else:
        await target.answer(text, parse_mode="HTML", reply_markup=markup)


async def render_kb_list_screen(chat_id: int, tag: str | None, page: int, target: types.Message | types.CallbackQuery, state: FSMContext):
    await state.update_data(kb_current_tag=tag, kb_current_page=page)
    
    saved_msgs = get_saved_messages(chat_id)
    if tag:
        filtered = [m for m in saved_msgs if (m[1] or "Без тега") == tag]
    else:
        filtered = saved_msgs
        
    if not filtered:
        text = "📭 В этой категории нет закладок."
        markup = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="🔙 К списку тегов", callback_data="flow_back")]]
        )
        if isinstance(target, CallbackQuery):
            await target.message.edit_text(text, parse_mode="HTML", reply_markup=markup)
            await target.answer()
        else:
            await target.answer(text, parse_mode="HTML", reply_markup=markup)
        return

    items_per_page = 5
    total_items = len(filtered)
    total_pages = (total_items + items_per_page - 1) // items_per_page
    if page < 1:
        page = 1
    if page > total_pages:
        page = total_pages
        
    start_idx = (page - 1) * items_per_page
    end_idx = start_idx + items_per_page
    page_items = filtered[start_idx:end_idx]
    
    page_msg_ids = [item[0] for item in page_items]
    await state.update_data(kb_page_msg_ids=page_msg_ids)
    
    tag_title = f"🏷 #{tag}" if tag else "📁 Все закладки"
    text_lines = [f"<b>{tag_title}</b> (стр. {page} из {total_pages})\n"]
    
    emoji_nums = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"]
    for idx, item in enumerate(page_items):
        db_id, item_tag, full_text, source, _ = item
        normalized_full = (full_text or "").replace('\n', ' ')
        preview = normalized_full[:37] + "..." if len(normalized_full) > 40 else normalized_full
        source_safe = html.escape(source) if source else "Неизвестно"
        preview_safe = html.escape(preview) if preview else "Без текста"
        
        text_lines.append(f"{emoji_nums[idx]} От: <b>{source_safe}</b>\n   <i>{preview_safe}</i>")
        text_lines.append("")
        
    text = "\n".join(text_lines)
    
    select_row = []
    for idx in range(len(page_items)):
        select_row.append(InlineKeyboardButton(text=emoji_nums[idx], callback_data=f"kb_sel_idx_{idx}"))
    
    nav_row = []
    if page > 1:
        nav_row.append(InlineKeyboardButton(text="⬅️ Назад", callback_data="kb_page_prev"))
    else:
        nav_row.append(InlineKeyboardButton(text=" ", callback_data="kb_ignore"))
        
    nav_row.append(InlineKeyboardButton(text=f"Стр {page}/{total_pages}", callback_data="kb_ignore"))
    
    if page < total_pages:
        nav_row.append(InlineKeyboardButton(text="Вперёд ➡️", callback_data="kb_page_next"))
    else:
        nav_row.append(InlineKeyboardButton(text=" ", callback_data="kb_ignore"))
        
    back_row = [
        InlineKeyboardButton(text="🔙 К категориям", callback_data="kb_view_tags"),
        InlineKeyboardButton(text="🏠 В начало", callback_data="flow_home")
    ]
    
    markup = InlineKeyboardMarkup(inline_keyboard=[select_row, nav_row, back_row])
    
    if isinstance(target, CallbackQuery):
        await target.message.edit_text(text, parse_mode="HTML", reply_markup=markup)
        await target.answer()
    else:
        await target.answer(text, parse_mode="HTML", reply_markup=markup)


async def render_kb_detail_screen(chat_id: int, msg_id: int, target: types.Message | types.CallbackQuery, state: FSMContext):
    await state.update_data(bookmark_msg_id=msg_id)
    
    msg_data = get_saved_message_by_id(chat_id, msg_id)
    if not msg_data:
        text = "❌ Закладка не найдена."
        markup = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="flow_back")]]
        )
        if isinstance(target, CallbackQuery):
            await target.message.edit_text(text, parse_mode="HTML", reply_markup=markup)
            await target.answer()
        else:
            await target.answer(text, parse_mode="HTML", reply_markup=markup)
        return

    full_text, source, tag, saved_at = msg_data
    dt_obj = display_db_datetime(saved_at)
    tag_safe = html.escape(tag) if tag else "Без тега"
    source_safe = html.escape(source) if source else "Неизвестно"
    full_text_safe = html.escape(full_text) if full_text else "Без текста"
    
    max_len = 3000
    if len(full_text_safe) > max_len:
        full_text_safe = full_text_safe[:max_len] + "\n\n⚠️ <i>(Сообщение обрезано, так как оно слишком длинное)</i>"
        
    text = (
        f"🏷 <b>Категория:</b> #{tag_safe}\n"
        f"👤 <b>Источник:</b> {source_safe}\n"
        f"📅 <b>Сохранено:</b> {dt_obj.strftime('%d.%m.%Y в %H:%M')}\n\n"
        f"📝 <b>Текст закладки:</b>\n{full_text_safe}"
    )
    
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🏷 Изменить тег", callback_data=f"bkmv_init_{msg_id}"),
                InlineKeyboardButton(text="⏰ В напоминание", callback_data=f"bksched_init_{msg_id}"),
            ],
            [
                InlineKeyboardButton(text="🗑 Удалить закладку", callback_data=f"bkdel_{msg_id}"),
            ],
            [
                InlineKeyboardButton(text="🔙 Назад", callback_data="flow_back"),
                InlineKeyboardButton(text="🏠 В начало", callback_data="flow_home"),
            ]
        ]
    )
    
    if isinstance(target, CallbackQuery):
        await target.message.edit_text(text, parse_mode="HTML", reply_markup=markup)
        await target.answer()
    else:
        await target.answer(text, parse_mode="HTML", reply_markup=markup)


async def render_kb_move_screen(chat_id: int, msg_id: int, target: types.Message | types.CallbackQuery, state: FSMContext):
    await state.set_state(SaveState.waiting_for_new_tag)
    
    msg_data = get_saved_message_by_id(chat_id, msg_id)
    if not msg_data:
        text = "❌ Закладка не найдена."
        markup = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="flow_back")]]
        )
        if isinstance(target, CallbackQuery):
            await target.message.edit_text(text, parse_mode="HTML", reply_markup=markup)
            await target.answer()
        else:
            await target.answer(text, parse_mode="HTML", reply_markup=markup)
        return
        
    full_text, source, tag, saved_at = msg_data
    tag_safe = html.escape(tag) if tag else "Без тега"
    
    text = (
        f"🏷 <b>Перенос в другую категорию</b>\n\n"
        f"Текущая категория закладки: <b>#{tag_safe}</b>\n\n"
        f"Выберите одну из существующих категорий ниже или пришлите новый тег текстом в чат:"
    )
    
    tags = get_user_tags(chat_id)
    await state.update_data(kb_move_tags=tags, bookmark_msg_id=msg_id)
    
    buttons = []
    row = []
    for idx, t in enumerate(tags):
        display_t = t[:15] + "..." if len(t) > 18 else t
        btn = InlineKeyboardButton(text=f"📁 {display_t}", callback_data=f"kb_mv_idx_{idx}")
        row.append(btn)
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
        
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


@dp.message(Command("saved"))
async def cmd_saved(message: types.Message, state: FSMContext):
    await state.clear()
    await push_screen(state, "kb_tags")
    await render_kb_tags_screen(message.chat.id, message, state)


@dp.callback_query(F.data.startswith("kb_tag_idx_"))
async def handle_kb_tag_selection(callback: CallbackQuery, state: FSMContext):
    idx = int(callback.data.split("_")[-1])
    data = await state.get_data()
    tags_list = data.get("kb_tags_list", [])
    if idx < len(tags_list):
        tag = tags_list[idx]
        await push_screen(state, "kb_list", tag=tag, page=1)
        await render_kb_list_screen(callback.message.chat.id, tag, 1, callback, state)
    else:
        await callback.answer("❌ Ошибка выбора категории.")


@dp.callback_query(F.data == "kb_tag_all")
async def handle_kb_tag_all(callback: CallbackQuery, state: FSMContext):
    await push_screen(state, "kb_list", tag=None, page=1)
    await render_kb_list_screen(callback.message.chat.id, None, 1, callback, state)


@dp.callback_query(F.data == "kb_view_tags")
async def handle_kb_view_tags(callback: CallbackQuery, state: FSMContext):
    await push_screen(state, "kb_tags")
    await render_kb_tags_screen(callback.message.chat.id, callback, state)


@dp.callback_query(F.data == "kb_page_prev")
async def handle_kb_page_prev(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    tag = data.get("kb_current_tag")
    page = data.get("kb_current_page", 1)
    prev_page = max(1, page - 1)
    history = list(data.get("screen_history", []))
    if history and history[-1].get("screen") == "kb_list":
        history[-1]["args"]["page"] = prev_page
        await state.update_data(screen_history=history)
    await render_kb_list_screen(callback.message.chat.id, tag, prev_page, callback, state)


@dp.callback_query(F.data == "kb_page_next")
async def handle_kb_page_next(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    tag = data.get("kb_current_tag")
    page = data.get("kb_current_page", 1)
    next_page = page + 1
    history = list(data.get("screen_history", []))
    if history and history[-1].get("screen") == "kb_list":
        history[-1]["args"]["page"] = next_page
        await state.update_data(screen_history=history)
    await render_kb_list_screen(callback.message.chat.id, tag, next_page, callback, state)


@dp.callback_query(F.data.startswith("kb_sel_idx_"))
async def handle_kb_sel_idx(callback: CallbackQuery, state: FSMContext):
    idx = int(callback.data.split("_")[-1])
    data = await state.get_data()
    page_msg_ids = data.get("kb_page_msg_ids", [])
    if idx < len(page_msg_ids):
        msg_id = page_msg_ids[idx]
        await push_screen(state, "kb_detail", msg_id=msg_id)
        await render_kb_detail_screen(callback.message.chat.id, msg_id, callback, state)
    else:
        await callback.answer("❌ Ошибка выбора закладки.")


@dp.callback_query(F.data == "kb_ignore")
async def handle_kb_ignore(callback: CallbackQuery):
    await callback.answer()


@dp.callback_query(F.data.startswith("bkmv_init_"))
async def handle_bkmv_init(callback: CallbackQuery, state: FSMContext):
    msg_id = int(callback.data.split("_")[-1])
    await push_screen(state, "kb_move", msg_id=msg_id)
    await render_kb_move_screen(callback.message.chat.id, msg_id, callback, state)


@dp.callback_query(F.data.startswith("kb_mv_idx_"))
async def handle_kb_mv_idx(callback: CallbackQuery, state: FSMContext):
    idx = int(callback.data.split("_")[-1])
    data = await state.get_data()
    tags = data.get("kb_move_tags", [])
    msg_id = data.get("bookmark_msg_id")
    if not msg_id:
        await callback.answer("❌ Ошибка: закладка не найдена.")
        return
    if idx < len(tags):
        new_tag = tags[idx]
        update_saved_message_tag(callback.message.chat.id, msg_id, new_tag)
        await callback.answer(f"✅ Перенесено в #{new_tag}")
        await pop_screen(state)
        await render_kb_detail_screen(callback.message.chat.id, msg_id, callback, state)
    else:
        await callback.answer("❌ Ошибка переноса.")


@dp.message(SaveState.waiting_for_new_tag)
async def process_new_tag(message: types.Message, state: FSMContext):
    new_tag = (message.text or "").strip()
    if not new_tag:
        await message.answer("❌ Тег не должен быть пустым.", reply_markup=build_back_home_keyboard())
        return
        
    if new_tag.startswith("#"):
        new_tag = new_tag[1:].strip()
        
    if not new_tag:
        await message.answer("❌ Некорректный тег.", reply_markup=build_back_home_keyboard())
        return
        
    data = await state.get_data()
    msg_id = data.get("bookmark_msg_id")
    if not msg_id:
        await message.answer("❌ Ошибка: не найден ID закладки. Попробуй сначала.", reply_markup=build_back_home_keyboard())
        return
        
    update_saved_message_tag(message.chat.id, msg_id, new_tag)
    await pop_screen(state)
    confirm = await message.answer(f"✅ Перенесено в #{new_tag}!")
    await state.set_state(None)
    
    await asyncio.sleep(2)
    try:
        await confirm.delete()
        await message.delete()
    except TelegramAPIError:
        pass
        
    await render_kb_detail_screen(message.chat.id, msg_id, message, state)


@dp.callback_query(F.data.startswith("bkdel_"))
async def handle_bkdel(callback: CallbackQuery, state: FSMContext):
    msg_id = int(callback.data.split("_")[-1])
    delete_saved_message(callback.message.chat.id, msg_id)
    await callback.answer("✅ Закладка удалена!")
    await pop_screen(state)
    
    data = await state.get_data()
    history = data.get("screen_history", [])
    if history:
        prev = history[-1]
        await render_screen(callback.message.chat.id, prev, callback, state)
    else:
        await render_kb_tags_screen(callback.message.chat.id, callback, state)


@dp.callback_query(F.data.startswith("bksched_init_"))
async def handle_bksched_init(callback: CallbackQuery, state: FSMContext):
    msg_id = int(callback.data.split("_")[-1])
    msg_data = get_saved_message_by_id(callback.message.chat.id, msg_id)
    if not msg_data:
        await callback.answer("❌ Закладка не найдена.")
        return
        
    full_text, source, tag, saved_at = msg_data
    await state.update_data(
        is_bookmark_scheduling=True,
        bookmark_msg_id=msg_id,
        preview=full_text,
        source=source,
        prompt_msg_id=callback.message.message_id,
    )
    
    markup = build_time_selection_keyboard(f"bksched_choice_{msg_id}")
    await callback.message.edit_text(
        f"⏰ <b>Планирование напоминания из закладки</b>\n\nВыбери время отправки напоминания:",
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
        
    data = await state.get_data()
    current_tag = data.get("kb_current_tag")
    current_page = data.get("kb_current_page")
    
    msg_data = get_saved_message_by_id(callback_message.chat.id, msg_id)
    if not msg_data:
        await callback.answer("❌ Закладка не найдена.")
        return
    full_text, source, tag, saved_at = msg_data
    
    if action == "custom":
        await state.update_data(
            is_bookmark_scheduling=True,
            bookmark_msg_id=msg_id,
            preview=full_text,
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
        
    now = local_now()
    scheduled_time, label = get_quick_scheduled_time(action, now)
    if scheduled_time:
        add_message(
            callback_message.chat.id,
            None,
            serialize_datetime(scheduled_time),
            full_text,
            source,
        )
        confirm_msg = await bot.send_message(
            callback_message.chat.id,
            f"✅ Напоминание запланировано на {scheduled_time.strftime('%d.%m.%Y %H:%M')}!"
        )
        await state.clear()
        
        await state.update_data(kb_current_tag=current_tag, kb_current_page=current_page)
        await push_screen(state, "kb_tags")
        if current_tag:
            await push_screen(state, "kb_list", tag=current_tag, page=current_page or 1)
        await push_screen(state, "kb_detail", msg_id=msg_id)
        
        await asyncio.sleep(2)
        try:
            await confirm_msg.delete()
        except TelegramAPIError:
            pass
            
        await render_screen(callback_message.chat.id, {"screen": "kb_detail", "args": {"msg_id": msg_id}}, callback_message, state)


async def complete_manual_schedule(
    chat_id: int,
    state: FSMContext,
    scheduled_time: datetime,
    *,
    reply_target: Message,
    cleanup_message_id: int | None = None,
) -> None:
    if scheduled_time < local_now():
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
    current_tag = data.get("kb_current_tag")
    current_page = data.get("kb_current_page")

    if msg_id:
        add_message(chat_id, msg_id, serialize_datetime(scheduled_time), preview, source)
        confirmation_text = f"✅ Принято! Запланировано на {scheduled_time.strftime('%d.%m.%Y в %H:%M')}."
    elif is_bksched:
        add_message(chat_id, None, serialize_datetime(scheduled_time), preview, source)
        confirmation_text = f"✅ Напоминание запланировано на {scheduled_time.strftime('%d.%m.%Y в %H:%M')}!"
    elif scheduled_db_id and source_message_id:
        replace_sent_reminder_with_pending(
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

    confirm_msg = await bot.send_message(chat_id, confirmation_text)
    await state.clear()

    if is_bksched and bookmark_msg_id:
        await state.update_data(kb_current_tag=current_tag, kb_current_page=current_page)
        await push_screen(state, "kb_tags")
        if current_tag:
            await push_screen(state, "kb_list", tag=current_tag, page=current_page or 1)
        await push_screen(state, "kb_detail", msg_id=bookmark_msg_id)
        await asyncio.sleep(2)
        try:
            await confirm_msg.delete()
        except TelegramAPIError:
            pass
        await render_screen(chat_id, {"screen": "kb_detail", "args": {"msg_id": bookmark_msg_id}}, reply_target, state)
        return

    await asyncio.sleep(3)
    try:
        await confirm_msg.delete()
    except TelegramAPIError:
        pass


@dp.message(ScheduleState.waiting_for_datetime)
async def process_custom_datetime(message: types.Message, state: FSMContext):
    text = (message.text or "").strip()
    if not text:
        await message.answer("❌ Неверный формат. Пример: 15.03.2026 14:30")
        return

    try:
        scheduled_time = datetime.strptime(text, "%d.%m.%Y %H:%M").replace(tzinfo=TZ)
    except ValueError:
        await message.answer("❌ Неверный формат. Пример: 15.03.2026 14:30")
        return

    await complete_manual_schedule(
        message.chat.id,
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
    scheduled_time = build_scheduled_time_from_state(data, hour=hour, minute=minute)
    if scheduled_time is None:
        await message.answer("Ошибка: дата выбора утеряна. Попробуй выбрать время заново.")
        await state.clear()
        return

    await complete_manual_schedule(
        message.chat.id,
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

    scheduled_time = build_scheduled_time_from_state(data, hour=int(hour), minute=minute)
    if scheduled_time is None:
        await message.answer("Ошибка: дата выбора утеряна. Попробуй выбрать время заново.")
        await state.clear()
        return

    await complete_manual_schedule(
        message.chat.id,
        state,
        scheduled_time,
        reply_target=message,
        cleanup_message_id=message.message_id,
    )


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
    user_subs = [sub for sub in get_user_subscriptions(user_id) if not sub[5]]

    if not user_subs:
        await message.answer("📭 У тебя нет активных подписок для парсинга (учитываются только не на паузе).")
        return

    await message.answer("⏳ Собираю единый дайджест за последние 24 часа...")
    now_tz = local_now()
    last_24h_str = serialize_datetime(now_tz - timedelta(days=1))
    semaphore = asyncio.Semaphore(DIGEST_FETCH_CONCURRENCY)

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
            sections.append({"sub_id": sub_id, "period": period, "title": title or "Канал", "posts": posts})
            try:
                add_digest_posts(user_id, username, title, posts)
            except Exception:
                logger.exception("Не удалось сохранить посты в базу знаний (test_digest)")

    if not sections:
        await message.answer("📭 За последние 24 часа новых постов ни в одном из каналов не было.")
        return

    await deliver_digest(user_id, "Твоя тестовая утренняя газета", sections)


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
    settings = resolve_digest_settings(callback_message.chat.id, period, now)
    next_send = get_next_digest_time(period, now, settings)
    last_scraped = serialize_datetime(utc_now())
    sub_id = add_subscription(
        callback_message.chat.id,
        username,
        title,
        period,
        last_scraped,
        serialize_datetime(next_send),
    )

    await callback.answer("✅ Подписка оформлена!")

    try:
        if callback_message.reply_to_message:
            await bot.delete_message(callback_message.chat.id, callback_message.reply_to_message.message_id)
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

    await start_save_flow(
        callback_message,
        state,
        full_text=full_text,
        source=source,
        orig_msg_id=orig_msg.message_id,
    )
    try:
        await callback_message.delete()
    except TelegramAPIError:
        pass


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
        await push_screen(state, "manual_date")
        await callback_message.edit_text(
            "Выбери дату:",
            reply_markup=build_manual_date_keyboard(),
        )
        await callback.answer()
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

    if action == "manual":
        suggested_time = get_suggested_manual_time(local_now())
        suggested_str = suggested_time.strftime("%d.%m.%Y %H:%M")
        await state.set_state(ScheduleState.waiting_for_datetime)
        await callback_message.edit_text(
            "Напиши точную дату и время в формате ДД.ММ.ГГГГ ЧЧ:ММ\n\n"
            "💡 *Лайфхак:* нажми на время ниже, чтобы скопировать его:\n\n"
            f"`{suggested_str}`",
            parse_mode="Markdown",
            reply_markup=build_back_home_keyboard(),
        )
        await callback.answer()
        return

    selected = get_manual_date(action, local_now())
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

    scheduled_time = build_scheduled_time_from_state(data, hour=int(hour), minute=minute)
    if scheduled_time is None:
        await callback.answer("❌ Дата выбора утеряна.", show_alert=True)
        await state.clear()
        return

    try:
        await complete_manual_schedule(
            callback_message.chat.id,
            state,
            scheduled_time,
            reply_target=callback_message,
        )
    except Exception:
        logger.exception("complete_manual_schedule failed in minute picker")
        await callback.answer(USER_FACING_ERROR, show_alert=True)
        return

    await callback.answer()


async def check_messages():
    while True:
        now_str = serialize_datetime(utc_now())
        pending = get_pending_messages(now_str)

        for db_id, chat_id, message_id, retry_count, text_preview, source_name in pending:
            attempt_number = retry_count + 1
            try:
                if message_id is None or message_id == 0:
                    text_to_send = text_preview or ""
                    if source_name:
                        text_to_send = f"👤 <b>Источник:</b> {html.escape(source_name)}\n\n{text_to_send}"
                    sent_message = await bot.send_message(
                        chat_id=int(chat_id),
                        text=text_to_send,
                        parse_mode="HTML",
                        reply_markup=build_sent_reminder_actions_keyboard(),
                    )
                else:
                    sent_message = await bot.copy_message(
                        chat_id=int(chat_id),
                        from_chat_id=int(chat_id),
                        message_id=message_id,
                        reply_markup=build_sent_reminder_actions_keyboard(),
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


async def run_digest_cycle(session: aiohttp.ClientSession, semaphore: asyncio.Semaphore) -> int:
    cycle_started_at = time.monotonic()
    now_str = serialize_datetime(utc_now())
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

    logger.info(
        "🔄 Начинаю проверку %s/%s due-дайджестов (batch_limit=%s, next_send_at <= %s)",
        len(due_subs),
        due_total,
        DIGEST_DUE_BATCH_SIZE,
        now_str,
    )
    prefetched_channels = await prefetch_due_channels(due_subs, session, semaphore)
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
    users_subs: dict[int, list] = {}
    for sub in due_subs:
        users_subs.setdefault(sub[1], []).append(sub)

    for user_id, subs in users_subs.items():
        try:
            sections: list[dict] = []
            user_has_temporary_failures = False
            successful_subscriptions: list[dict] = []

            results = await asyncio.gather(
                *(
                    fetch_subscription_posts(
                        sub,
                        session,
                        semaphore,
                        now_str,
                        prefetched_channels,
                    )
                    for sub in subs
                ),
                return_exceptions=True,
            )

            for result in results:
                if isinstance(result, BaseException):
                    failures_count += 1
                    user_has_temporary_failures = True
                    logger.error(
                        "Изолирована необработанная ошибка чтения для пользователя %s",
                        user_id,
                        exc_info=(type(result), result, result.__traceback__),
                    )
                    continue
                if result["status"] == "error":
                    failures_count += 1
                    if not result["is_permanent"]:
                        user_has_temporary_failures = True
                    continue

                successful_subscriptions.append(result)
                if result["posts"]:
                    sections.append(
                        {
                            "sub_id": result["sub_id"],
                            "period": result["period"],
                            "title": result["title"] or "Канал",
                            "posts": result["posts"],
                        }
                    )
                    try:
                        add_digest_posts(
                            user_id,
                            result["username"],
                            result["title"],
                            result["posts"],
                        )
                    except Exception:
                        logger.exception("Не удалось сохранить посты в базу знаний")

            if sections:
                try:
                    await deliver_digest(user_id, "Твоя утренняя газета", sections)
                    delivered_users_count += 1
                    delivered_posts_count += sum(len(section["posts"]) for section in sections)
                    for result in successful_subscriptions:
                        if result["posts"]:
                            update_subscription_time(
                                result["sub_id"],
                                result["last_scraped_str"],
                                result["next_send_str"],
                                result["last_post_id"],
                            )
                        else:
                            update_subscription_schedule(
                                result["sub_id"],
                                result["next_send_str"],
                                now_str,
                            )
                except TelegramAPIError as exc:
                    failures_count += len(successful_subscriptions)
                    is_permanent, error_text = classify_telegram_send_error(exc)
                    for result in successful_subscriptions:
                        original = next((item for item in subs if item[0] == result["sub_id"]), None)
                        failure_count = original[6] if original else 0
                        new_failure_count = failure_count + 1
                        mark_subscription_delivery_error(
                            result["sub_id"],
                            error_text,
                            now_str,
                            new_failure_count,
                            is_permanent or new_failure_count >= MAX_DIGEST_RETRIES,
                        )
                    logger.exception("Не удалось отправить дайджест пользователю %s", user_id)
            else:
                for result in successful_subscriptions:
                    update_subscription_schedule(
                        result["sub_id"],
                        result["next_send_str"],
                        now_str,
                    )

            if user_has_temporary_failures:
                logger.info(
                    "Для пользователя %s чтение части каналов будет повторено позже.",
                    user_id,
                )
        except Exception:
            failures_count += len(subs)
            logger.exception(
                "Изолирована ошибка обработки дайджеста пользователя %s; продолжаю со следующим пользователем",
                user_id,
            )

    logger.info(
        "digest_cycle due_total=%s processed=%s backlog=%s channels=%s channel_failures=%s "
        "fetched_posts=%s delivered_users=%s delivered_posts=%s subscription_failures=%s duration_ms=%s",
        due_total,
        len(due_subs),
        max(due_total - len(due_subs), 0),
        len(prefetched_channels),
        channel_failures_count,
        fetched_posts_count,
        delivered_users_count,
        delivered_posts_count,
        failures_count,
        round((time.monotonic() - cycle_started_at) * 1000),
    )
    return max(due_total - len(due_subs), 0)


async def check_digests():
    semaphore = asyncio.Semaphore(DIGEST_FETCH_CONCURRENCY)
    logger.info(
        "📬 Сервис дайджестов запущен (проверка каждые %d сек / %d мин)",
        DIGEST_CHECK_INTERVAL_SECONDS,
        DIGEST_CHECK_INTERVAL_SECONDS // 60,
    )

    async with create_telegram_http_session() as session:
        while True:
            try:
                backlog = await run_digest_cycle(session, semaphore)
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
        cleanup_stats = cleanup_old_records(now_str)
        if any(cleanup_stats.values()):
            logger.info("Очистка SQLite завершена: %s", cleanup_stats)
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
    ]
    logger.info("✅ Бот успешно запущен")
    logger.info("📋 Фоновые процессы:")
    logger.info("  • Напоминания: проверка каждые 30 сек")
    logger.info("  • Дайджесты: проверка каждые %d мин (оптимизировано с 1 мин)", DIGEST_CHECK_INTERVAL_SECONDS // 60)
    logger.info("  • Очистка БД: раз в %d часов", CLEANUP_INTERVAL_SECONDS // 3600)
    logger.info("🚀 Ожидаю входящих сообщений...")
    try:
        await dp.start_polling(bot)
    finally:
        for task in background_tasks:
            task.cancel()
        await asyncio.gather(*background_tasks, return_exceptions=True)
        await channel_source.close()


if __name__ == "__main__":
    asyncio.run(main())
