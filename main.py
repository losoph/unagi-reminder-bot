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
DIGEST_CHECK_INTERVAL_SECONDS = int(os.getenv("DIGEST_CHECK_INTERVAL_SECONDS", 30 * 60))  # Можно переопределить через .env
CLEANUP_INTERVAL_SECONDS = 6 * 60 * 60

USER_FACING_ERROR = "Что-то пошло не так. Попробуй ещё раз позже."

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


class SaveState(StatesGroup):
    waiting_for_tag = State()


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
                InlineKeyboardButton(text="✍️ Вручную", callback_data=f"{prefix}_custom"),
            ],
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


async def send_digest_intro(user_id: int, title: str):
    await bot.send_message(
        user_id,
        title,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="⚙️ Настройки дайджеста", callback_data="digest_settings")]
            ]
        ),
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


async def send_digest_channel_block(
    user_id: int,
    sub_id: int,
    period: str,
    channel_title: str | None,
    posts: list[dict],
):
    lines = [f"📌 <b>{html.escape(channel_title) if channel_title else 'Канал'}</b>"]
    for post in posts:
        text_safe = html.escape(post["text"])
        lines.append(f"🔹 <i>{text_safe}</i> <a href='{post['link']}'>[Читать]</a>\n")

    chunks = chunk_html_text(lines)
    for index, chunk in enumerate(chunks):
        await bot.send_message(
            user_id,
            chunk,
            parse_mode="HTML",
            reply_markup=build_digest_channel_actions(sub_id, period) if index == 0 else None,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )


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


@dp.message(CommandStart())
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Привет! Я готов.\n\n"
        "1️⃣ Перешли мне любое сообщение, чтобы отложить его или сохранить в базу знаний.\n"
        "2️⃣ Перешли пост из открытого канала, чтобы подписаться на его дайджест.\n"
        "3️⃣ Напиши /list для задач или /saved для Избранного.\n"
        "4️⃣ Для уже доставленного напоминания используй кнопки под ним или ответь командой: "
        "/morning, /day, /evening, /later (/l), /at ДД.ММ.ГГГГ ЧЧ:ММ, /save (/s), /delete (/d)."
    )


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
        suggested_time = get_suggested_manual_time(now)
        suggested_str = suggested_time.strftime("%d.%m.%Y %H:%M")
        prompt = await callback_message.answer(
            "Напиши точную дату и время в формате ДД.ММ.ГГГГ ЧЧ:ММ\n\n"
            "💡 *Лайфхак:* нажми на время ниже, чтобы скопировать его:\n\n"
            f"`{suggested_str}`",
            parse_mode="Markdown",
        )
        await state.update_data(
            scheduled_db_id=db_id,
            source_message_id=source_message_id,
            preview=preview or "",
            source=source or "",
            prompt_msg_id=prompt.message_id,
            target_message_id=delivered_message_id,
            command_message_id=callback_message.message_id,
        )
        await state.set_state(ScheduleState.waiting_for_datetime)
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

    if user_subs:
        text_lines = ["📡 <b>Твои подписки на дайджесты:</b>\n"]
        buttons = []
        grouped = {"daily": [], "weekly": [], "monthly": []}
        for sub in user_subs:
            grouped.setdefault(sub[3], []).append(sub)
        idx = 1
        for period in ("daily", "weekly", "monthly"):
            subs = grouped.get(period, [])
            if not subs:
                continue
            text_lines.append(f"<b>{_PERIOD_TITLES[period]}</b>")
            for sub in subs:
                sub_id, _, title, _, next_send_at = sub
                dt_obj = display_db_datetime(next_send_at)
                text_lines.append(
                    f"{idx}. 📰 <b>{html.escape(title) if title else 'Канал'}</b>\n"
                    f"След: {dt_obj.strftime('%d.%m в %H:%M')}\n"
                )
                buttons.append(InlineKeyboardButton(text=f"❌ {idx}", callback_data=f"unsub_{sub_id}"))
                idx += 1
        kb_rows = [buttons[i:i + 5] for i in range(0, len(buttons), 5)]
        kb_rows.append([InlineKeyboardButton(text="⚙️ Настройки дайджеста", callback_data="digest_settings")])
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

    user_subs = get_user_subscriptions(callback_message.chat.id)
    if not user_subs:
        await callback_message.edit_text("📭 Все подписки на дайджесты отменены.")
        return

    text_lines = ["📡 <b>Твои подписки на дайджесты:</b>\n"]
    buttons = []
    grouped = {"daily": [], "weekly": [], "monthly": []}
    for sub in user_subs:
        grouped.setdefault(sub[3], []).append(sub)
    idx = 1
    for period in ("daily", "weekly", "monthly"):
        subs = grouped.get(period, [])
        if not subs:
            continue
        text_lines.append(f"<b>{_PERIOD_TITLES[period]}</b>")
        for sub in subs:
            item_id, _, title, _, next_send_at = sub
            dt_obj = display_db_datetime(next_send_at)
            text_lines.append(
                f"{idx}. 📰 <b>{html.escape(title) if title else 'Канал'}</b>\n"
                f"След: {dt_obj.strftime('%d.%m в %H:%M')}\n"
            )
            buttons.append(InlineKeyboardButton(text=f"❌ {idx}", callback_data=f"unsub_{item_id}"))
            idx += 1

    kb_rows = [buttons[i:i + 5] for i in range(0, len(buttons), 5)]
    kb_rows.append([InlineKeyboardButton(text="⚙️ Настройки дайджеста", callback_data="digest_settings")])
    try:
        await callback_message.edit_text(
            "\n".join(text_lines),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
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
            normalized_full = full_text.replace('\n', ' ')
            preview = normalized_full[:37] + "..." if len(normalized_full) > 40 else normalized_full
            source_safe = html.escape(source) if source else "Неизвестно"
            preview_safe = html.escape(preview) if preview else "Без текста"

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
    tag_safe = html.escape(tag) if tag else "Без тега"
    source_safe = html.escape(source) if source else "Неизвестно"
    full_text_safe = html.escape(full_text) if full_text else "Без текста"
    text = (
        f"🏷 <b>Тег:</b> {tag_safe}\n"
        f"👤 <b>Источник:</b> {source_safe}\n"
        f"📅 <b>Сохранено:</b> {dt_obj.strftime('%d.%m.%Y в %H:%M')}\n\n"
        f"📝 <b>Текст:</b>\n{full_text_safe}"
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
        scheduled_db_id = data.get("scheduled_db_id")
        source_message_id = data.get("source_message_id")
        preview = data.get("preview", "")
        source = data.get("source", "")
        prompt_msg_id = data.get("prompt_msg_id")

        if msg_id:
            add_message(message.chat.id, msg_id, serialize_datetime(scheduled_time), preview, source)
            confirmation_text = f"✅ Принято! Запланировано на {scheduled_time.strftime('%d.%m.%Y в %H:%M')}."
        elif scheduled_db_id and source_message_id:
            replace_sent_reminder_with_pending(
                message.chat.id,
                scheduled_db_id,
                source_message_id,
                serialize_datetime(scheduled_time),
                preview,
                source,
            )
            confirmation_text = f"✅ Отложил до {scheduled_time.strftime('%d.%m.%Y %H:%M')}."
        else:
            await message.answer("Ошибка: не найдено исходное напоминание. Попробуй отправить его заново.")
            await state.clear()
            return

        try:
            await message.delete()
            if prompt_msg_id:
                await bot.delete_message(message.chat.id, prompt_msg_id)
            target_message_id = data.get("target_message_id")
            if target_message_id:
                await bot.delete_message(message.chat.id, target_message_id)
            command_message_id = data.get("command_message_id")
            if command_message_id:
                await bot.delete_message(message.chat.id, command_message_id)
        except TelegramAPIError:
            pass

        confirm_msg = await message.answer(confirmation_text)
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
    has_news = False
    semaphore = asyncio.Semaphore(DIGEST_FETCH_CONCURRENCY)

    async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
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

    await send_digest_intro(user_id, "📰 <b>Твоя тестовая утренняя газета</b> ☕️")
    for result in results:
        if isinstance(result, BaseException):
            continue
        sub_id, period, title, _, posts = result
        if posts:
            await send_digest_channel_block(user_id, sub_id, period, title, posts)


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

    async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
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
                    channel_results = []
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
                            channel_results.append(result)

                    if has_news:
                        try:
                            await send_digest_intro(user_id, "📰 <b>Твоя утренняя газета</b> ☕️")
                            for result in channel_results:
                                await send_digest_channel_block(
                                    user_id,
                                    result["sub_id"],
                                    result["period"],
                                    result["title"],
                                    result["posts"],
                                )
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
    init_db()
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
