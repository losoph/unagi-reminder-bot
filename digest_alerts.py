import html
import logging
import os
from datetime import datetime

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from data.database import (
    get_delivered_executions_pending_recovery_alert,
    get_digest_alert,
    get_failed_digest_executions_pending_alert,
    get_latest_telegraph_digest,
    get_user_alert_settings,
    has_digest_alert_been_sent,
    is_user_alerting_active,
    parse_db_datetime,
    record_digest_alert_sent,
    record_user_alert_dispatched,
    serialize_datetime,
    set_user_alerts_enabled,
    utc_now,
)

logger = logging.getLogger(__name__)

DIGEST_ALERT_GRACE_PERIOD_SECONDS = max(60, int(os.getenv("DIGEST_ALERT_GRACE_PERIOD_SECONDS", "3600")))
MAX_DIGEST_RETRIES = max(1, int(os.getenv("MAX_DIGEST_RETRIES", "5")))
ALERT_BASE_COOLDOWN_SECONDS = max(60, int(os.getenv("ALERT_BASE_COOLDOWN_SECONDS", "14400")))
ALERT_MAX_COOLDOWN_SECONDS = max(60, int(os.getenv("ALERT_MAX_COOLDOWN_SECONDS", "86400")))

PERIOD_TITLES: dict[str, str] = {
    "daily": "утренний дайджест",
    "weekly": "еженедельный дайджест",
    "monthly": "ежемесячный дайджест",
}

PERIOD_NAMES: dict[str, str] = {
    "daily": "Ежедневный",
    "weekly": "Еженедельный",
    "monthly": "Ежемесячный",
}


def calculate_alert_cooldown(
    consecutive_failures: int,
    base_cooldown: int = ALERT_BASE_COOLDOWN_SECONDS,
    max_cooldown: int = ALERT_MAX_COOLDOWN_SECONDS,
) -> int:
    if consecutive_failures <= 0:
        return 0
    exponent = max(0, consecutive_failures - 1)
    cooldown = base_cooldown * (2 ** min(exponent, 10))
    return min(cooldown, max_cooldown)


def is_alert_allowed_by_cooldown(
    user_id: int,
    now: datetime | None = None,
    base_cooldown: int = ALERT_BASE_COOLDOWN_SECONDS,
    max_cooldown: int = ALERT_MAX_COOLDOWN_SECONDS,
) -> bool:
    if now is None:
        now = utc_now()
    settings = get_user_alert_settings(user_id)
    last_sent_str = settings.get("last_alert_sent_at")
    if not last_sent_str:
        return True

    consecutive = settings.get("consecutive_failures", 0)
    cooldown_seconds = calculate_alert_cooldown(consecutive, base_cooldown, max_cooldown)
    try:
        last_sent = parse_db_datetime(last_sent_str)
        elapsed = (now - last_sent).total_seconds()
        return elapsed >= cooldown_seconds
    except Exception:
        return True


def format_missed_digest_alert(
    period: str,
    scheduled_at: str | datetime,
    channels_count: int,
    channels_failed: int,
    will_retry: bool = False,
    error_message: str | None = None,
) -> str:
    title = PERIOD_TITLES.get(period, f"дайджест ({period})")
    period_name = PERIOD_NAMES.get(period, period)
    if isinstance(scheduled_at, datetime):
        sched_str = scheduled_at.strftime("%d.%m.%Y %H:%M UTC")
    else:
        try:
            dt = parse_db_datetime(scheduled_at)
            sched_str = dt.strftime("%d.%m.%Y %H:%M UTC")
        except Exception:
            sched_str = html.escape(str(scheduled_at))

    lines = [
        f"⚠️ <b>Не удалось сформировать {html.escape(title)}</b>\n",
        f"Период: <b>{html.escape(period_name)}</b>",
        f"Запланирован: <b>{sched_str}</b>",
        f"Недоступно каналов: <b>{channels_failed} из {channels_count}</b>",
    ]
    if will_retry:
        lines.append("Повтор: <b>бот предпримет ещё одну попытку</b>")
    else:
        lines.append("Повтор: <b>автоматические попытки исчерпаны</b>")

    if error_message and error_message.strip():
        lines.append(f"\nПричина: <code>{html.escape(error_message[:150])}</code>")

    return "\n".join(lines)


def build_digest_alert_keyboard(execution_id: int | None = None) -> InlineKeyboardMarkup:
    rows = []
    if execution_id is not None:
        rows.append(
            [
                InlineKeyboardButton(
                    text="🔄 Повторить сейчас",
                    callback_data=f"alert_retry_{execution_id}",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(text="⏸ Пауза 24 ч", callback_data="alert_pause_24h"),
            InlineKeyboardButton(text="⏸ На 7 дней", callback_data="alert_pause_7d"),
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(text="🔕 Отключить тех. алерты", callback_data="alert_disable"),
            InlineKeyboardButton(text="⚙️ Настройки", callback_data="ds"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def should_send_digest_failure_alert(
    execution: dict,
    now: datetime | None = None,
    grace_period_seconds: int | None = None,
    base_cooldown: int | None = None,
    max_cooldown: int | None = None,
) -> bool:
    status = execution.get("status")
    # Semantic frame: 'empty', 'delivered', 'partial' must never trigger missed digest alert
    if status in ("empty", "delivered", "partial"):
        return False

    retries_exhausted = (status == "failed") or (
        status == "retrying" and execution.get("retry_count", 0) >= MAX_DIGEST_RETRIES
    )
    if not retries_exhausted:
        return False

    user_id = execution.get("user_id")
    if not user_id or not is_user_alerting_active(user_id, now=now):
        return False

    period = execution.get("period", "")
    scheduled_at = execution.get("scheduled_at", "")
    if has_digest_alert_been_sent(user_id, period, scheduled_at, "failure"):
        return False

    if grace_period_seconds is None:
        grace_period_seconds = DIGEST_ALERT_GRACE_PERIOD_SECONDS

    if now is None:
        now = utc_now()

    try:
        sched_dt = parse_db_datetime(scheduled_at)
        if (now - sched_dt).total_seconds() < grace_period_seconds:
            return False
    except Exception:
        pass

    b_cd = base_cooldown if base_cooldown is not None else ALERT_BASE_COOLDOWN_SECONDS
    m_cd = max_cooldown if max_cooldown is not None else ALERT_MAX_COOLDOWN_SECONDS
    if not is_alert_allowed_by_cooldown(user_id, now=now, base_cooldown=b_cd, max_cooldown=m_cd):
        return False

    return True


async def send_digest_failure_alert(
    bot: Bot,
    execution: dict,
    reply_markup=None,
    now: datetime | None = None,
    base_cooldown: int | None = None,
    max_cooldown: int | None = None,
) -> bool:
    user_id = execution.get("user_id")
    period = execution.get("period", "")
    scheduled_at = execution.get("scheduled_at", "")
    channels_count = execution.get("channels_count", 0)
    channels_failed = execution.get("channels_failed", 0)
    error_message = execution.get("error_message")

    if not is_user_alerting_active(user_id, now=now):
        return False

    if execution.get("status") in ("empty", "delivered", "partial"):
        return False

    existing_alert = get_digest_alert(user_id, period, scheduled_at, "failure")
    now_val = now or utc_now()
    sent_at_str = serialize_datetime(now_val)

    text = format_missed_digest_alert(
        period=period,
        scheduled_at=scheduled_at,
        channels_count=channels_count,
        channels_failed=channels_failed,
        will_retry=False,
        error_message=error_message,
    )

    if reply_markup is None:
        reply_markup = build_digest_alert_keyboard(execution.get("id"))

    # If alert was already sent for this edition: update existing message if message_id is available
    if existing_alert:
        msg_id = existing_alert.get("message_id")
        existing_error = existing_alert.get("error_text")
        if not msg_id or (error_message or "").strip() == (existing_error or "").strip():
            return False
        try:
            await bot.edit_message_text(
                chat_id=user_id,
                message_id=msg_id,
                text=text,
                parse_mode="HTML",
                reply_markup=reply_markup,
            )
            record_digest_alert_sent(
                user_id=user_id,
                period=period,
                scheduled_at=scheduled_at,
                alert_type="failure",
                sent_at=sent_at_str,
                message_id=msg_id,
                error_text=error_message,
            )
            logger.info("Обновлён существующий алерт: user=%s, period=%s, msg_id=%s", user_id, period, msg_id)
            return True
        except TelegramForbiddenError:
            logger.warning("Пользователь %s заблокировал бота при обновлении алерта; отключаю алерты", user_id)
            set_user_alerts_enabled(user_id, False)
            return False
        except (TelegramBadRequest, TelegramAPIError) as exc:
            logger.debug("Не удалось обновить алерт %s для пользователя %s: %s", msg_id, user_id, exc)
            return False

    # Check cooldown before sending a new alert message
    b_cd = base_cooldown if base_cooldown is not None else ALERT_BASE_COOLDOWN_SECONDS
    m_cd = max_cooldown if max_cooldown is not None else ALERT_MAX_COOLDOWN_SECONDS
    if not is_alert_allowed_by_cooldown(user_id, now=now_val, base_cooldown=b_cd, max_cooldown=m_cd):
        logger.info("Отправка нового алерта пользователю %s отложена по anti-spam cooldown", user_id)
        return False

    try:
        sent_msg = await bot.send_message(
            chat_id=user_id,
            text=text,
            parse_mode="HTML",
            reply_markup=reply_markup,
        )
        msg_id = getattr(sent_msg, "message_id", None)
        record_digest_alert_sent(
            user_id=user_id,
            period=period,
            scheduled_at=scheduled_at,
            alert_type="failure",
            sent_at=sent_at_str,
            message_id=msg_id,
            error_text=error_message,
        )
        record_user_alert_dispatched(user_id, sent_at_str)
        logger.info(
            "Отправлен пользовательский алерт о пропущенном дайджесте: user=%s, period=%s, sched=%s",
            user_id,
            period,
            scheduled_at,
        )
        return True
    except TelegramForbiddenError:
        logger.warning(
            "Пользователь %s заблокировал бота; отключаю технические алерты",
            user_id,
        )
        set_user_alerts_enabled(user_id, False)
        record_digest_alert_sent(
            user_id=user_id,
            period=period,
            scheduled_at=scheduled_at,
            alert_type="failure",
            sent_at=sent_at_str,
            error_text="TelegramForbiddenError",
        )
        return False
    except TelegramAPIError as exc:
        logger.warning(
            "Не удалось доставить алерт пользователю %s: %s",
            user_id,
            exc,
        )
        return False


async def process_pending_digest_alerts(
    bot: Bot,
    now: datetime | None = None,
    grace_period_seconds: int | None = None,
    reply_markup_builder=None,
) -> int:
    if now is None:
        now = utc_now()
    if grace_period_seconds is None:
        grace_period_seconds = DIGEST_ALERT_GRACE_PERIOD_SECONDS

    now_str = serialize_datetime(now)
    pending_list = get_failed_digest_executions_pending_alert(
        older_than_seconds=grace_period_seconds,
        max_retries=MAX_DIGEST_RETRIES,
        now_str=now_str,
    )
    sent_count = 0
    for execution in pending_list:
        try:
            if not should_send_digest_failure_alert(
                execution,
                now=now,
                grace_period_seconds=grace_period_seconds,
            ):
                continue
            markup = (
                reply_markup_builder(execution)
                if reply_markup_builder
                else build_digest_alert_keyboard(execution.get("id"))
            )
            sent = await send_digest_failure_alert(
                bot=bot,
                execution=execution,
                reply_markup=markup,
                now=now,
            )
            if sent:
                sent_count += 1
        except Exception:
            logger.exception(
                "Ошибка при обработке алерта по execution %s",
                execution.get("id"),
            )

    recovery_count = await process_pending_recovery_alerts(bot, now=now)
    return sent_count + recovery_count


def format_recovery_alert(
    period: str,
    scheduled_at: str | datetime,
    posts_count: int = 0,
    digest_url: str | None = None,
) -> str:
    title = PERIOD_TITLES.get(period, f"дайджест ({period})")
    if isinstance(scheduled_at, datetime):
        sched_str = scheduled_at.strftime("%d.%m.%Y %H:%M UTC")
    else:
        try:
            dt = parse_db_datetime(scheduled_at)
            sched_str = dt.strftime("%d.%m.%Y %H:%M UTC")
        except Exception:
            sched_str = html.escape(str(scheduled_at))

    lines = [
        f"✅ <b>{html.escape(title.capitalize())} восстановлен</b>\n",
        f"Выпуск за <b>{sched_str}</b> успешно собран.",
    ]
    if posts_count > 0:
        lines.append(f"Количество постов: <b>{posts_count}</b>")

    if digest_url and digest_url.strip():
        lines.append(f'\n📖 <a href="{html.escape(digest_url.strip())}">Открыть дайджест в Telegraph</a>')

    return "\n".join(lines)


def build_recovery_alert_keyboard(digest_url: str | None = None) -> InlineKeyboardMarkup | None:
    if digest_url and digest_url.strip():
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="📖 Открыть дайджест", url=digest_url.strip())]
            ]
        )
    return None


async def send_digest_recovery_alert(
    bot: Bot,
    execution: dict,
    digest_url: str | None = None,
    now: datetime | None = None,
) -> bool:
    user_id = execution.get("user_id")
    period = execution.get("period", "")
    scheduled_at = execution.get("scheduled_at", "")
    posts_count = execution.get("posts_count", 0)

    if not user_id:
        return False

    now_val = now or utc_now()
    sent_at_str = serialize_datetime(now_val)

    # 1. Deduplication: exactly one recovery notification per edition
    if has_digest_alert_been_sent(user_id, period, scheduled_at, "recovery"):
        return False

    # 2. Only send recovery if failure alert was sent for this edition
    if not has_digest_alert_been_sent(user_id, period, scheduled_at, "failure"):
        return False

    # 3. Do not send if user alerts are paused or disabled
    if not is_user_alerting_active(user_id, now=now_val):
        record_digest_alert_sent(
            user_id=user_id,
            period=period,
            scheduled_at=scheduled_at,
            alert_type="recovery",
            sent_at=sent_at_str,
            error_text="suppressed_alerts_inactive",
        )
        return False

    # 4. Resolve Telegraph link if available
    if digest_url is None:
        t_row = get_latest_telegraph_digest(user_id, since_str=execution.get("started_at"))
        if t_row:
            digest_url = t_row.get("url")

    text = format_recovery_alert(
        period=period,
        scheduled_at=scheduled_at,
        posts_count=posts_count,
        digest_url=digest_url,
    )
    keyboard = build_recovery_alert_keyboard(digest_url)

    try:
        sent_msg = await bot.send_message(
            chat_id=user_id,
            text=text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        msg_id = getattr(sent_msg, "message_id", None)
        record_digest_alert_sent(
            user_id=user_id,
            period=period,
            scheduled_at=scheduled_at,
            alert_type="recovery",
            sent_at=sent_at_str,
            message_id=msg_id,
        )
        logger.info(
            "Отправлен recovery алерт: user=%s, period=%s, sched=%s, posts=%s",
            user_id,
            period,
            scheduled_at,
            posts_count,
        )
        return True
    except TelegramForbiddenError:
        logger.warning(
            "Пользователь %s заблокировал бота при отправке recovery; отключаю алерты",
            user_id,
        )
        set_user_alerts_enabled(user_id, False)
        record_digest_alert_sent(
            user_id=user_id,
            period=period,
            scheduled_at=scheduled_at,
            alert_type="recovery",
            sent_at=sent_at_str,
            error_text="TelegramForbiddenError",
        )
        return False
    except TelegramAPIError as exc:
        logger.warning(
            "Не удалось доставить recovery алерт пользователю %s: %s",
            user_id,
            exc,
        )
        return False


async def process_pending_recovery_alerts(
    bot: Bot,
    now: datetime | None = None,
) -> int:
    if now is None:
        now = utc_now()
    now_str = serialize_datetime(now)
    pending_list = get_delivered_executions_pending_recovery_alert(now_str=now_str)
    sent_count = 0
    for execution in pending_list:
        try:
            sent = await send_digest_recovery_alert(
                bot=bot,
                execution=execution,
                now=now,
            )
            if sent:
                sent_count += 1
        except Exception:
            logger.exception(
                "Ошибка при обработке recovery алерта по execution %s",
                execution.get("id"),
            )
    return sent_count
