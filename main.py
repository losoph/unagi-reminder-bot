import asyncio
import calendar
import html
import logging
import os
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
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, InaccessibleMessage, Message
from aiohttp_socks import ProxyConnector # type: ignore
from dotenv import load_dotenv

from data.database import (
    add_message,
    add_saved_message,
    add_subscription,
    cleanup_old_records,
    delete_message,
    delete_saved_message,
    get_digest_settings,
    delete_subscription,
    get_due_subscriptions,
    get_pending_messages,
    get_saved_message_by_id,
    get_scheduled_message_by_delivered_message_id,
    get_subscription_by_id,
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
    update_subscriptions_next_send_at,
    update_subscription_time,
    upsert_digest_settings,
    utc_now,
    update_saved_message_tag,
)
from scraper import ChannelFetchError, REQUEST_TIMEOUT, get_latest_posts

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
BOT_USERNAME = os.getenv("BOT_USERNAME", "").lstrip("@")
TELEGRAM_PROXY = os.getenv("TELEGRAM_PROXY") or os.getenv("PROXY_URL")

if not BOT_TOKEN:
    raise ValueError("❌ Токен бота не найден! Проверьте файл .env")

bot_session = AiohttpSession(proxy=TELEGRAM_PROXY) if TELEGRAM_PROXY else None
bot = Bot(token=BOT_TOKEN, session=bot_session)
dp = Dispatcher()
TZ = ZoneInfo(os.getenv("APP_TIMEZONE", "Europe/Moscow"))
MAX_MESSAGE_RETRIES = 5
MAX_DIGEST_RETRIES = 5
DIGEST_FETCH_CONCURRENCY = 5
DIGEST_CHECK_INTERVAL_SECONDS = int(os.getenv("DIGEST_CHECK_INTERVAL_SECONDS", 30 * 60))  # Можно переопределить через .env
CLEANUP_INTERVAL_SECONDS = 6 * 60 * 60

USER_FACING_ERROR = "Что-то пошло не так. Попробуй ещё раз позже."


def create_telegram_http_session() -> aiohttp.ClientSession:
    if TELEGRAM_PROXY:
        return aiohttp.ClientSession(
            timeout=REQUEST_TIMEOUT,
            connector=ProxyConnector.from_url(TELEGRAM_PROXY),
        )
    return aiohttp.ClientSession(timeout=REQUEST_TIMEOUT)

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
    return {
        "send_hour": stored["send_hour"] if stored["send_hour"] is not None else defaults["send_hour"],
        "send_minute": stored["send_minute"] if stored["send_minute"] is not None else defaults["send_minute"],
        "weekday": stored["weekday"] if stored["weekday"] is not None else defaults["weekday"],
        "month_day": stored["month_day"] if stored["month_day"] is not None else defaults["month_day"],
        "monthly_mode": stored["monthly_mode"] if stored["monthly_mode"] else defaults["monthly_mode"],
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


def build_digest_channel_actions(sub_id: int, current_period: str) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="🚫 Отписаться", callback_data=f"unsub_{sub_id}")]]
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
    rows.append([InlineKeyboardButton(text="⚙️ Настройки дайджеста", callback_data="digest_settings")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


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
    for sub in user_subs:
        title = sub[2] or "Канал"
        short_title = title[:22] + "…" if len(title) > 23 else title
        rows.append(
            [InlineKeyboardButton(text=f"📰 {short_title}", callback_data=f"dsub_{sub[0]}")]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def render_digest_settings_text(user_id: int) -> str:
    user_subs = get_user_subscriptions(user_id)
    grouped = {"daily": [], "weekly": [], "monthly": []}
    for sub in user_subs:
        grouped.setdefault(sub[3], []).append(sub)

    lines = ["⚙️ <b>Настройки дайджеста</b>", ""]
    for period in ("daily", "weekly", "monthly"):
        settings = resolve_digest_settings(user_id, period)
        lines.append(
            f"• <b>{_PERIOD_TITLES[period]}</b>: {format_digest_schedule(period, settings)}"
        )
    lines.append("")
    lines.append("<b>Подписки</b>")
    for period in ("daily", "weekly", "monthly"):
        subs = grouped.get(period, [])
        lines.append(f"{_PERIOD_TITLES[period]}: {len(subs)}")
        if subs:
            for sub in subs:
                lines.append(f"• {html.escape(sub[2]) if sub[2] else 'Канал'}")
        else:
            lines.append("• Пока пусто")
        lines.append("")
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


async def fetch_subscription_posts(
    sub: tuple,
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore,
    now_str: str,
):
    sub_id, user_id, username, title, period, last_scraped, failure_count = sub
    title_safe = html.escape(title) if title else "Канал"
    settings = resolve_digest_settings(user_id, period)
    next_send_str = serialize_datetime(get_next_digest_time(period, local_now(), settings))

    async with semaphore:
        try:
            posts = await get_latest_posts(username, last_scraped, session=session)
            return {
                "status": "ok",
                "sub_id": sub_id,
                "period": period,
                "username": username,
                "title": title,
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
        "💡 <i>Перешлите пост из любого открытого канала, чтобы подписаться на него.</i>"
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


@dp.callback_query(F.data == "digest_settings")
async def open_digest_settings(callback: CallbackQuery):
    user_id = callback.from_user.id
    await edit_or_answer(
        callback,
        render_digest_settings_text(user_id),
        reply_markup=build_digest_settings_keyboard(get_user_subscriptions(user_id)),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("dsub_"))
async def open_digest_subscription_actions(callback: CallbackQuery):
    user_id = callback.from_user.id
    sub_id = parse_callback_int_suffix(callback.data, "dsub_")
    if sub_id is None:
        await callback.answer("❌ Некорректные данные.", show_alert=True)
        return
    sub = get_subscription_by_id(user_id, sub_id)
    if not sub:
        await callback.answer("❌ Подписка не найдена.", show_alert=True)
        return
    text = (
        f"📰 <b>{html.escape(sub['channel_title']) if sub['channel_title'] else 'Канал'}</b>\n"
        f"Сейчас: {_PERIOD_TITLES[sub['period']]}\n"
        f"Следующая отправка: {display_db_datetime(sub['next_send_at']).strftime('%d.%m в %H:%M')}"
    )
    keyboard = build_digest_channel_actions(sub_id, sub["period"]).inline_keyboard
    keyboard.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="digest_settings")])
    await edit_or_answer(callback, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
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
    add_subscription(
        user_id,
        sub["channel_username"],
        sub["channel_title"],
        new_period,
        sub["last_scraped_at"] or serialize_datetime(utc_now()),
        next_send_at,
    )
    delete_subscription(user_id, sub_id)

    updated_subs = get_user_subscriptions(user_id)
    await edit_or_answer(
        callback,
        render_digest_settings_text(user_id),
        reply_markup=build_digest_settings_keyboard(updated_subs),
    )
    await callback.answer(f"Перенёс в {_PERIOD_TITLES[new_period].lower()} дайджест.")


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
    user_subs = get_user_subscriptions(user_id)

    if not user_subs:
        await message.answer("📭 У тебя нет активных подписок для парсинга.")
        return

    await message.answer("⏳ Собираю единый дайджест за последние 24 часа...")
    now_tz = local_now()
    last_24h_str = serialize_datetime(now_tz - timedelta(days=1))
    has_news = False
    semaphore = asyncio.Semaphore(DIGEST_FETCH_CONCURRENCY)

    async with create_telegram_http_session() as session:
        async def load_posts(subscription) -> tuple[int, str, str | None, str, list[dict]]:
            sub_id, username, title, period, _ = subscription
            async with semaphore:
                return sub_id, period, title, username, await get_latest_posts(username, last_24h_str, session=session)

        results: list[tuple[int, str, str | None, str, list[dict]] | BaseException] = await asyncio.gather(
            *(load_posts(sub) for sub in user_subs),
            return_exceptions=True,
        )

    for result in results:
        if isinstance(result, BaseException):
            logger.error(
                "test_digest channel fetch failed",
                exc_info=(type(result), result, result.__traceback__),
            )
            continue

        sub_id, period, title, _, posts = result
        if posts:
            has_news = True

    if not has_news:
        await message.answer("📭 За последние 24 часа новых постов ни в одном из каналов не было.")
        return

    digest_lines = ["📰 <b>Твоя тестовая утренняя газета</b> ☕️", ""]
    for result in results:
        if isinstance(result, BaseException):
            continue
        sub_id, period, title, _, posts = result
        if posts:
            append_digest_channel_lines(digest_lines, sub_id, period, title, posts)
    digest_lines.append(
        build_digest_action_link("⚙️ Настройки дайджеста", "ds")
    )
    await send_digest_chunks(user_id, digest_lines)


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
    add_subscription(
        callback_message.chat.id,
        username,
        title,
        period,
        last_scraped,
        serialize_datetime(next_send),
    )

    await callback.answer(
        f"✅ Дайджест {title} оформлен!\nБудет приходить {format_digest_schedule(period, settings)}.",
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


async def check_digests():
    semaphore = asyncio.Semaphore(DIGEST_FETCH_CONCURRENCY)
    logger.info("📬 Сервис дайджестов запущен (проверка каждые %d сек / %d мин)", 
                DIGEST_CHECK_INTERVAL_SECONDS, DIGEST_CHECK_INTERVAL_SECONDS // 60)

    async with create_telegram_http_session() as session:
        while True:
            now_str = serialize_datetime(utc_now())
            due_subs = get_due_subscriptions(now_str)
            
            if not due_subs:
                logger.debug("⏭ Нет дайджестов для отправки. Следующая проверка через %d мин", 
                             DIGEST_CHECK_INTERVAL_SECONDS // 60)
            else:
                logger.info("🔄 Начинаю проверку %s дайджестов (next_send_at <= %s)", len(due_subs), now_str)

                users_subs = {}
                for sub in due_subs:
                    users_subs.setdefault(sub[1], []).append(sub)

                for user_id, subs in users_subs.items():
                    digest_lines = ["📰 <b>Твоя утренняя газета</b> ☕️", ""]
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
                            append_digest_channel_lines(
                                digest_lines,
                                result["sub_id"],
                                result["period"],
                                result["title"],
                                result["posts"],
                            )

                    if has_news:
                        try:
                            digest_lines.append(build_digest_action_link("⚙️ Настройки дайджеста", "ds"))
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

            await asyncio.sleep(DIGEST_CHECK_INTERVAL_SECONDS)


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
    asyncio.create_task(check_messages())
    asyncio.create_task(check_digests())
    asyncio.create_task(cleanup_database())
    logger.info("✅ Бот успешно запущен")
    logger.info("📋 Фоновые процессы:")
    logger.info("  • Напоминания: проверка каждые 30 сек")
    logger.info("  • Дайджесты: проверка каждые %d мин (оптимизировано с 1 мин)", DIGEST_CHECK_INTERVAL_SECONDS // 60)
    logger.info("  • Очистка БД: раз в %d часов", CLEANUP_INTERVAL_SECONDS // 3600)
    logger.info("🚀 Ожидаю входящих сообщений...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
