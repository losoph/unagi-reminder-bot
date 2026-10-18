"""Модуль настроек и расписания дайджеста (экраны и колбэки).

Содержит FSM-состояния, экраны навигации, рендеринг карточек каналов,
пикеры расписания (время, день недели, число/режим месяца) и обработчики
соответствующих callback-запросов подписок на дайджест.
"""

import asyncio
import calendar
import html
import logging
from datetime import datetime, timedelta

from aiogram import F, Router, types
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InaccessibleMessage,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from config import settings as app_settings
import email_digest
from data.database import (
    add_subscription,
    count_user_subscriptions,
    delete_subscription,
    get_digest_settings,
    get_email_delivery,
    get_subscription_by_id,
    get_subscription_tags,
    get_user_alert_settings,
    get_user_subscriptions,
    parse_db_datetime,
    serialize_datetime,
    set_all_subscriptions_paused,
    set_subscription_paused,
    set_subscription_tag,
    unsubscribe_all,
    update_subscription_time,
    update_subscriptions_next_send_at,
    upsert_digest_settings,
    utc_now,
)
from time_utils import TZ, display_db_datetime, local_now

logger = logging.getLogger(__name__)

router = Router(name="digest_settings")

_PERIOD_RU = {"daily": "каждый день", "weekly": "раз в неделю", "monthly": "раз в месяц"}
_PERIOD_TITLES = {"daily": "Ежедневный", "weekly": "Еженедельный", "monthly": "Ежемесячный"}
_PERIOD_PLURAL = {"daily": "ежедневные", "weekly": "еженедельные", "monthly": "ежемесячные"}
_WEEKDAY_NOM_RU = {0: "понедельник", 1: "вторник", 2: "среда", 3: "четверг", 4: "пятница", 5: "суббота", 6: "воскресенье"}
_WEEKDAY_ACC_RU = {0: "понедельник", 1: "вторник", 2: "среду", 3: "четверг", 4: "пятницу", 5: "субботу", 6: "воскресенье"}
_WEEKDAY_SHORT_RU = {0: "Пн", 1: "Вт", 2: "Ср", 3: "Чт", 4: "Пт", 5: "Сб", 6: "Вс"}


class SubTagState(StatesGroup):
    waiting_for_folder = State()


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


def get_default_digest_settings(period: str, now: datetime | None = None) -> dict[str, int | str]:
    now = now or local_now()
    return {
        "send_hour": 7,
        "send_minute": 0,
        "weekday": now.weekday(),
        "month_day": now.day,
        "monthly_mode": "date",
    }


# ghsub склеивает недельное письмо в воскресенье 07:10 МСК и берёт файлы, лежащие в ящике к 06:15.
# Цикл дайджестов просыпается раз в DIGEST_CHECK_INTERVAL_SECONDS (по умолчанию 30 мин), поэтому почтовый
# недельный дайджест ставится на воскресенье 05:00 — с запасом на интервал цикла и чтение каналов.
GHSUB_WEEKLY_PIN: dict[str, int] = {"weekday": 6, "send_hour": 5, "send_minute": 0}


def is_weekly_pinned_for_ghsub(user_id: int) -> bool:
    """Недельный дайджест уходит на почту через ghsub — его время задаёт ghsub, а не пользователь."""
    if app_settings().email.delivery != "ghsub":
        return False
    _, email_periods = get_email_delivery(user_id)
    return "weekly" in email_periods


def resolve_digest_settings(user_id: int, period: str, now: datetime | None = None) -> dict[str, int | str]:
    """Расписание, по которому реально уходит дайджест периода (с учётом почтового окна ghsub)."""
    resolved = resolve_user_digest_settings(user_id, period, now)
    if period == "weekly" and is_weekly_pinned_for_ghsub(user_id):
        resolved.update(GHSUB_WEEKLY_PIN)
    return resolved


def resolve_user_digest_settings(user_id: int, period: str, now: datetime | None = None) -> dict[str, int | str]:
    """Расписание периода, выбранное пользователем (без почтового окна ghsub)."""
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
        [
            InlineKeyboardButton(text="📁 Папка", callback_data=f"dtag_{sub_id}"),
            InlineKeyboardButton(text="📰 В Ленту", callback_data=f"feed_copy_{sub_id}"),
        ],
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
            (s[6] or "").lower(),  # tag/folder, untagged last
            (s[2] or "").lower(),
        ),
    )


def build_digest_settings_keyboard(user_subs, user_id: int | None = None) -> InlineKeyboardMarkup:
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
    if email_digest.is_enabled():
        rows.append([InlineKeyboardButton(text="📧 Дайджест на почту", callback_data="email_menu")])

    uid = user_id if user_id is not None else (user_subs[0][1] if user_subs else None)
    if uid is not None:
        alert_settings = get_user_alert_settings(uid)
        is_paused = False
        if alert_settings["alerts_paused_until"]:
            dt_pause = parse_db_datetime(alert_settings["alerts_paused_until"])
            is_paused = dt_pause > utc_now()
        if not alert_settings["alerts_enabled"]:
            rows.append([InlineKeyboardButton(text="🔔 Включить тех. алерты", callback_data="alert_toggle_enable")])
        elif is_paused:
            rows.append([InlineKeyboardButton(text="▶️ Снять паузу с алертов", callback_data="alert_toggle_enable")])
        else:
            rows.append([InlineKeyboardButton(text="🔕 Отключить тех. алерты", callback_data="alert_disable")])

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


def get_email_delivery_periods(user_id: int) -> tuple[str | None, set[str]]:
    """Digest periods that go to email *instead of* Telegram (empty when email is unavailable)."""
    if not email_digest.is_enabled():
        return None, set()
    return get_email_delivery(user_id)


def render_digest_settings_text(user_id: int) -> str:
    user_subs = get_user_subscriptions(user_id)

    lines = ["⚙️ <b>Настройки дайджеста</b>", ""]
    lines.append("<b>Расписание:</b>")
    _, email_periods = get_email_delivery_periods(user_id)
    for period in ("daily", "weekly", "monthly"):
        settings = resolve_digest_settings(user_id, period)
        where = " · 📧 на почту" if period in email_periods else ""
        lines.append(
            f"• <b>{_PERIOD_TITLES[period]}</b>: {format_digest_schedule(period, settings)}{where}"
        )
    lines.append("")

    alert_settings = get_user_alert_settings(user_id)
    if not alert_settings["alerts_enabled"]:
        lines.append("🔕 <b>Технические алерты:</b> отключены")
    elif alert_settings["alerts_paused_until"]:
        dt_pause = parse_db_datetime(alert_settings["alerts_paused_until"])
        if dt_pause > utc_now():
            lines.append(f"⏸ <b>Технические алерты:</b> на паузе до {dt_pause.strftime('%d.%m %H:%M UTC')}")
        else:
            lines.append("🔔 <b>Технические алерты:</b> включены")
    else:
        lines.append("🔔 <b>Технические алерты:</b> включены")
    lines.append("")

    if not user_subs:
        lines.append("<b>Подписки:</b> пока пусто.")
        lines.append("")
        lines.append("💡 Перешли пост из открытого канала или пришли ссылку на него, чтобы подписаться.")
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
    if period == "weekly" and is_weekly_pinned_for_ghsub(user_id):
        lines.append(
            "📧 Еженедельный дайджест уходит на почту в общем недельном письме, которое собирается "
            "в воскресенье утром, поэтому он готовится в воскресенье в 05:00. "
            "Выбранное здесь время вернётся, если переключить дайджест обратно в Telegram."
        )
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


def sync_weekly_email_schedule(user_id: int) -> None:
    """Пересчитать недельное расписание после смены почтовой доставки (только при доставке через ghsub)."""
    if app_settings().email.delivery == "ghsub":
        reschedule_user_period(user_id, "weekly")


async def _refresh_digest_settings(callback: CallbackQuery):
    user_id = user_id_from_callback(callback)
    subs = await asyncio.to_thread(get_user_subscriptions, user_id)
    await edit_or_answer(
        callback,
        render_digest_settings_text(user_id),
        reply_markup=build_digest_settings_keyboard(subs, user_id=user_id),
    )


async def render_subscription_actions(callback: CallbackQuery, sub_id: int) -> bool:
    user_id = user_id_from_callback(callback)
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


@router.callback_query(F.data.in_({"digest_settings", "ds"}))
async def open_digest_settings(callback: CallbackQuery, state: FSMContext):
    await state.set_state(None)
    await _refresh_digest_settings(callback)
    await callback.answer()


@router.callback_query(F.data.startswith("dsub_"))
async def open_digest_subscription_actions(callback: CallbackQuery):
    sub_id = parse_callback_int_suffix(callback.data, "dsub_")
    if sub_id is None:
        await callback.answer("❌ Некорректные данные.", show_alert=True)
        return
    if await render_subscription_actions(callback, sub_id):
        await callback.answer()


@router.callback_query(F.data.startswith("dmove_"))
async def move_digest_subscription(callback: CallbackQuery):
    user_id = user_id_from_callback(callback)
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
    try:
        new_sub_id = add_subscription(
            user_id,
            sub["channel_username"],
            sub["channel_title"],
            new_period,
            sub["last_scraped_at"] or serialize_datetime(utc_now()),
            next_send_at,
            tag=sub["tag"],
        )
    except ValueError as exc:
        await callback.answer(f"🚫 {exc}", show_alert=True)
        return
    if new_sub_id != sub_id:
        delete_subscription(user_id, sub_id)

    await render_subscription_actions(callback, new_sub_id)
    await callback.answer(f"Перенёс в {_PERIOD_TITLES[new_period].lower()} дайджест.")


@router.callback_query(F.data.startswith("dpause_"))
async def pause_subscription(callback: CallbackQuery):
    sub_id = parse_callback_int_suffix(callback.data, "dpause_")
    if sub_id is None:
        await callback.answer("❌ Некорректные данные.", show_alert=True)
        return
    set_subscription_paused(user_id_from_callback(callback), sub_id, True)
    await render_subscription_actions(callback, sub_id)
    await callback.answer("⏸ Подписка на паузе.")


@router.callback_query(F.data.startswith("dresume_"))
async def resume_subscription(callback: CallbackQuery):
    sub_id = parse_callback_int_suffix(callback.data, "dresume_")
    if sub_id is None:
        await callback.answer("❌ Некорректные данные.", show_alert=True)
        return
    user_id = user_id_from_callback(callback)
    set_subscription_paused(user_id, sub_id, False)
    # Сдвигаем next_send_at вперёд, чтобы возобновлённая подписка не сработала мгновенно.
    sub = get_subscription_by_id(user_id, sub_id)
    if sub:
        settings = resolve_digest_settings(user_id, sub["period"])
        next_send_at = serialize_datetime(get_next_digest_time(sub["period"], local_now(), settings))
        update_subscription_time(sub_id, sub["last_scraped_at"] or serialize_datetime(utc_now()), next_send_at)
    await render_subscription_actions(callback, sub_id)
    await callback.answer("▶️ Подписка возобновлена.")


@router.callback_query(F.data == "dpauseall")
async def pause_all_subscriptions(callback: CallbackQuery):
    changed = await asyncio.to_thread(set_all_subscriptions_paused, user_id_from_callback(callback), True)
    await _refresh_digest_settings(callback)
    await callback.answer(f"⏸ На паузе: {changed}")


@router.callback_query(F.data == "dresumeall")
async def resume_all_subscriptions(callback: CallbackQuery):
    user_id = user_id_from_callback(callback)
    changed = await asyncio.to_thread(set_all_subscriptions_paused, user_id, False)
    # Пересчитываем расписание для всех периодов после возобновления.
    for period in ("daily", "weekly", "monthly"):
        await asyncio.to_thread(reschedule_user_period, user_id, period)
    await _refresh_digest_settings(callback)
    await callback.answer(f"▶️ Возобновлено: {changed}")


@router.callback_query(F.data == "dunsuball")
async def confirm_unsubscribe_all(callback: CallbackQuery):
    count = count_user_subscriptions(user_id_from_callback(callback))
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


@router.callback_query(F.data == "dunsuball_yes")
async def unsubscribe_all_confirmed(callback: CallbackQuery):
    removed = await asyncio.to_thread(unsubscribe_all, user_id_from_callback(callback))
    await _refresh_digest_settings(callback)
    await callback.answer(f"🗑 Удалено подписок: {removed}")


@router.callback_query(F.data.startswith("dtag_"))
async def open_subscription_folder_menu(callback: CallbackQuery, state: FSMContext):
    user_id = user_id_from_callback(callback)
    sub_id = parse_callback_int_suffix(callback.data, "dtag_")
    if sub_id is None:
        await callback.answer("❌ Некорректные данные.", show_alert=True)
        return
    sub = get_subscription_by_id(user_id, sub_id)
    if not sub:
        await callback.answer("❌ Подписка не найдена.", show_alert=True)
        return
    folders = await asyncio.to_thread(get_subscription_tags, user_id)
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


@router.callback_query(F.data.startswith("dtagset_"))
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
    set_subscription_tag(user_id_from_callback(callback), sub_id, folders[idx])
    await render_subscription_actions(callback, sub_id)
    await callback.answer(f"📁 Папка: {folders[idx]}")


@router.callback_query(F.data.startswith("dtagclear_"))
async def clear_subscription_folder(callback: CallbackQuery):
    sub_id = parse_callback_int_suffix(callback.data, "dtagclear_")
    if sub_id is None:
        await callback.answer("❌ Некорректные данные.", show_alert=True)
        return
    set_subscription_tag(user_id_from_callback(callback), sub_id, None)
    await render_subscription_actions(callback, sub_id)
    await callback.answer("📁 Убрано из папки.")


@router.callback_query(F.data.startswith("dtagnew_"))
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


@router.message(SubTagState.waiting_for_folder)
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
    set_subscription_tag(user_id_from_message(message), sub_id, folder)
    await state.set_state(None)
    confirm = await message.answer(f"📁 Канал перенесён в папку <b>{html.escape(folder)}</b>.", parse_mode="HTML")
    await asyncio.sleep(2)
    try:
        await confirm.delete()
        await message.delete()
    except TelegramAPIError:
        pass
    subs = await asyncio.to_thread(get_user_subscriptions, user_id_from_message(message))
    await message.answer(
        render_digest_settings_text(user_id_from_message(message)),
        parse_mode="HTML",
        reply_markup=build_digest_settings_keyboard(subs),
    )


@router.callback_query(F.data.startswith("dsch_"))
async def open_digest_schedule(callback: CallbackQuery):
    period = parse_callback_strip_prefix(callback.data, "dsch_")
    if period is None:
        await callback.answer("❌ Некорректные данные.", show_alert=True)
        return
    await edit_or_answer(
        callback,
        render_digest_schedule_text(user_id_from_callback(callback), period),
        reply_markup=build_digest_schedule_keyboard(period),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("dtime_"))
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


@router.callback_query(F.data.startswith("dth_"))
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


@router.callback_query(F.data.startswith("dtm_"))
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
    upsert_digest_settings(user_id_from_callback(callback), period, send_hour=hour, send_minute=minute)
    await asyncio.to_thread(reschedule_user_period, user_id_from_callback(callback), period)
    await edit_or_answer(
        callback,
        render_digest_schedule_text(user_id_from_callback(callback), period),
        reply_markup=build_digest_schedule_keyboard(period),
    )
    await callback.answer(f"Время обновлено: {hour:02d}:{minute:02d}")


@router.callback_query(F.data.startswith("dweekday_"))
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


@router.callback_query(F.data.startswith("dwd_"))
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
    upsert_digest_settings(user_id_from_callback(callback), period, weekday=weekday)
    await asyncio.to_thread(reschedule_user_period, user_id_from_callback(callback), period)
    await edit_or_answer(
        callback,
        render_digest_schedule_text(user_id_from_callback(callback), period),
        reply_markup=build_digest_schedule_keyboard(period),
    )
    await callback.answer(f"День обновлён: {_WEEKDAY_ACC_RU[weekday]}.")


@router.callback_query(F.data == "dmday_monthly")
async def open_digest_monthday_picker(callback: CallbackQuery):
    await edit_or_answer(
        callback,
        "📆 <b>Ежемесячный дайджест</b>\n\nВыбери дату месяца:",
        reply_markup=build_digest_monthday_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("dmd_monthly_"))
async def save_digest_monthday(callback: CallbackQuery):
    day = parse_callback_int_suffix(callback.data, "dmd_monthly_")
    if day is None:
        await callback.answer("❌ Некорректные данные.", show_alert=True)
        return
    upsert_digest_settings(user_id_from_callback(callback), "monthly", month_day=day)
    await asyncio.to_thread(reschedule_user_period, user_id_from_callback(callback), "monthly")
    await edit_or_answer(
        callback,
        render_digest_schedule_text(user_id_from_callback(callback), "monthly"),
        reply_markup=build_digest_schedule_keyboard("monthly"),
    )
    await callback.answer(f"Дата обновлена: {day}.")


@router.callback_query(F.data == "dmode_monthly")
async def open_digest_monthly_mode_picker(callback: CallbackQuery):
    settings = resolve_digest_settings(user_id_from_callback(callback), "monthly")
    await edit_or_answer(
        callback,
        "🔀 <b>Ежемесячный дайджест</b>\n\nВыбери режим отправки:",
        reply_markup=build_digest_monthly_mode_keyboard(str(settings.get("monthly_mode", "date"))),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("dmm_monthly_"))
async def save_digest_monthly_mode(callback: CallbackQuery):
    mode = parse_callback_strip_prefix(callback.data, "dmm_monthly_")
    if mode not in {"date", "weekday"}:
        await callback.answer("❌ Некорректные данные.", show_alert=True)
        return
    upsert_digest_settings(user_id_from_callback(callback), "monthly", monthly_mode=mode)
    await asyncio.to_thread(reschedule_user_period, user_id_from_callback(callback), "monthly")
    await edit_or_answer(
        callback,
        render_digest_schedule_text(user_id_from_callback(callback), "monthly"),
        reply_markup=build_digest_schedule_keyboard("monthly"),
    )
    await callback.answer("Режим обновлён.")
