import html
import logging
import os
from datetime import datetime

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from data.database import (
    get_failed_digest_executions_pending_alert,
    has_digest_alert_been_sent,
    is_user_alerting_active,
    parse_db_datetime,
    record_digest_alert_sent,
    serialize_datetime,
    set_user_alerts_enabled,
    utc_now,
)

logger = logging.getLogger(__name__)

DIGEST_ALERT_GRACE_PERIOD_SECONDS = max(60, int(os.getenv("DIGEST_ALERT_GRACE_PERIOD_SECONDS", "3600")))
MAX_DIGEST_RETRIES = max(1, int(os.getenv("MAX_DIGEST_RETRIES", "5")))

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

    return True


async def send_digest_failure_alert(
    bot: Bot,
    execution: dict,
    reply_markup=None,
    now: datetime | None = None,
) -> bool:
    user_id = execution.get("user_id")
    period = execution.get("period", "")
    scheduled_at = execution.get("scheduled_at", "")
    channels_count = execution.get("channels_count", 0)
    channels_failed = execution.get("channels_failed", 0)
    error_message = execution.get("error_message")

    if not is_user_alerting_active(user_id, now=now):
        return False

    if has_digest_alert_been_sent(user_id, period, scheduled_at, "failure"):
        return False

    if execution.get("status") in ("empty", "delivered", "partial"):
        return False

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

    now_val = now or utc_now()
    sent_at_str = serialize_datetime(now_val)

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
    return sent_count
