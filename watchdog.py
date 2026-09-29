import asyncio
import html
import logging
import os
from datetime import datetime

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError

from data.database import (
    META_ALERT_BACKLOG_SENT,
    META_ALERT_STALL_SENT,
    get_app_meta,
    get_digest_cycle_backlog_history,
    get_last_digest_cycle_at,
    parse_db_datetime,
    serialize_datetime,
    set_app_meta,
    utc_now,
)

logger = logging.getLogger(__name__)

WATCHDOG_CHECK_INTERVAL_SECONDS = max(10, int(os.getenv("WATCHDOG_CHECK_INTERVAL_SECONDS", "300")))
WATCHDOG_CYCLE_STALL_SECONDS = max(60, int(os.getenv("WATCHDOG_CYCLE_STALL_SECONDS", "7200")))
WATCHDOG_BACKLOG_GROWTH_CYCLES = max(2, int(os.getenv("WATCHDOG_BACKLOG_GROWTH_CYCLES", "3")))


def get_admin_chat_ids() -> list[int]:
    raw = os.getenv("ADMIN_USER_ID") or os.getenv("ADMIN_CHAT_ID") or ""
    ids: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if part:
            try:
                ids.append(int(part))
            except ValueError:
                logger.warning("Некорректный ID администратора в конфиге: %s", part)
    return ids


def format_stall_alert(
    last_cycle_at: str | datetime | None,
    elapsed_seconds: float,
    stall_threshold_seconds: int = WATCHDOG_CYCLE_STALL_SECONDS,
) -> str:
    elapsed_minutes = int(elapsed_seconds // 60)
    if isinstance(last_cycle_at, datetime):
        last_str = last_cycle_at.strftime("%d.%m.%Y %H:%M UTC")
    elif last_cycle_at:
        try:
            dt = parse_db_datetime(last_cycle_at)
            last_str = dt.strftime("%d.%m.%Y %H:%M UTC")
        except Exception:
            last_str = html.escape(str(last_cycle_at))
    else:
        last_str = "никогда"

    lines = [
        "🚨 <b>Служебный алерт: простой цикла дайджестов</b>\n",
        f"Цикл дайджестов не завершался уже <b>{elapsed_minutes} мин.</b> (порог: {stall_threshold_seconds // 60} мин.).",
        f"Последний успешный цикл: <b>{last_str}</b>.",
        "\nРекомендуется проверить состояние фонового сервиса и доступность Telegram.",
    ]
    return "\n".join(lines)


def format_backlog_growth_alert(history: list[int], required_cycles: int) -> str:
    recent = history[-required_cycles:]
    arrow_str = " → ".join(str(x) for x in recent)
    lines = [
        "⚠️ <b>Служебный алерт: рост очереди backlog</b>\n",
        f"Размер backlog растёт <b>{required_cycles} циклов подряд</b>:",
        f"Динамика: <b>{arrow_str}</b>",
        "\nВозможно, обработка каналов не успевает или превышен лимит страниц веб-fallback.",
    ]
    return "\n".join(lines)


def is_backlog_strictly_growing(history: list[int], required_cycles: int) -> bool:
    if len(history) < required_cycles:
        return False
    recent = history[-required_cycles:]
    if recent[-1] <= 0:
        return False
    for i in range(1, len(recent)):
        if recent[i] <= recent[i - 1]:
            return False
    return True


async def check_watchdog(
    bot: Bot,
    now: datetime | None = None,
    stall_seconds: int | None = None,
    growth_cycles: int | None = None,
) -> list[str]:
    admin_ids = get_admin_chat_ids()
    if not admin_ids:
        logger.debug("Watchdog: ADMIN_USER_ID / ADMIN_CHAT_ID не настроены, отправка пропущена")
        return []

    if now is None:
        now = utc_now()
    now_str = serialize_datetime(now)

    stall_threshold = stall_seconds if stall_seconds is not None else WATCHDOG_CYCLE_STALL_SECONDS
    required_growth = growth_cycles if growth_cycles is not None else WATCHDOG_BACKLOG_GROWTH_CYCLES

    alerts_triggered: list[str] = []

    # 1. Check Cycle Stall
    last_cycle_str = get_last_digest_cycle_at()
    if last_cycle_str:
        try:
            last_dt = parse_db_datetime(last_cycle_str)
            elapsed = (now - last_dt).total_seconds()
            if elapsed >= stall_threshold:
                stall_alert_sent = get_app_meta(META_ALERT_STALL_SENT)
                if not stall_alert_sent:
                    text = format_stall_alert(last_dt, elapsed, stall_threshold)
                    for chat_id in admin_ids:
                        try:
                            await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
                        except (TelegramForbiddenError, TelegramAPIError) as exc:
                            logger.warning("Не удалось отправить watchdog алерт админу %s: %s", chat_id, exc)
                    set_app_meta(META_ALERT_STALL_SENT, now_str)
                    alerts_triggered.append("stall")
                    logger.warning("Watchdog: отправлен алерт о простое цикла дайджестов (%s сек)", int(elapsed))
        except Exception:
            logger.exception("Ошибка при проверке простоя цикла в watchdog")

    # 2. Check Backlog Consecutive Growth
    history = get_digest_cycle_backlog_history()
    if is_backlog_strictly_growing(history, required_growth):
        backlog_alert_sent = get_app_meta(META_ALERT_BACKLOG_SENT)
        if not backlog_alert_sent:
            text = format_backlog_growth_alert(history, required_growth)
            for chat_id in admin_ids:
                try:
                    await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
                except (TelegramForbiddenError, TelegramAPIError) as exc:
                    logger.warning("Не удалось отправить watchdog алерт админу %s: %s", chat_id, exc)
            set_app_meta(META_ALERT_BACKLOG_SENT, now_str)
            alerts_triggered.append("backlog_growth")
            logger.warning("Watchdog: отправлен алерт о росте backlog (%s циклов подряд)", required_growth)

    return alerts_triggered


async def run_watchdog_loop(bot: Bot):
    logger.info(
        "🛡 Watchdog фоновых задач запущен (проверка каждые %d сек, порог простоя %d сек)",
        WATCHDOG_CHECK_INTERVAL_SECONDS,
        WATCHDOG_CYCLE_STALL_SECONDS,
    )
    while True:
        try:
            await check_watchdog(bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Ошибка в фоновом цикле watchdog")
        await asyncio.sleep(WATCHDOG_CHECK_INTERVAL_SECONDS)
