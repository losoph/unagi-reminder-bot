"""Feed (Лента) management, UX and orchestration.

Pure ranking and rendering live in separate modules (feed_ranking, feed_render).
This module provides the aiogram Router for /feed, channel management, tags, and UX.
Never imports main to avoid circular dependencies (pattern T14b).
"""

import asyncio
from datetime import datetime, timedelta
import html
import logging
import os
from typing import Any

from aiogram import F, Router, types
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from data.database import (
    add_feed_subscription,
    count_feed_subscriptions,
    delete_feed_subscription,
    get_channel_feed_backfilled_at,
    get_channel_scan_cursor,
    get_feed_subscription,
    get_subscription_by_id,
    list_feed_subscriptions,
    list_feed_tags,
    normalize_channel_username,
    normalize_feed_tag,
    parse_db_datetime,
    serialize_datetime,
    set_channel_feed_backfilled_at,
    set_feed_paused,
    set_feed_tag,
    update_channel_scan_cursor,
    upsert_channel_posts,
    utc_now,
)

logger = logging.getLogger(__name__)

router = Router(name="feed")

_bot: Any = None
_channel_source: Any = None
_backfill_hook: Any = None
_fetch_semaphore: asyncio.Semaphore | None = None
_backfill_tasks: dict[str, asyncio.Task] = {}
_user_build_locks: dict[int, asyncio.Lock] = {}


def get_fetch_semaphore() -> asyncio.Semaphore:
    """Return shared fetch semaphore with DIGEST_FETCH_CONCURRENCY limit."""
    global _fetch_semaphore
    if _fetch_semaphore is None:
        concurrency = int(os.getenv("DIGEST_FETCH_CONCURRENCY", "5"))
        _fetch_semaphore = asyncio.Semaphore(concurrency)
    return _fetch_semaphore


def get_user_build_lock(user_id: int) -> asyncio.Lock:
    """Ensure at most one feed build per user concurrently."""
    if user_id not in _user_build_locks:
        _user_build_locks[user_id] = asyncio.Lock()
    return _user_build_locks[user_id]


def configure(
    bot: Any = None,
    channel_source: Any = None,
    backfill_hook: Any = None,
    fetch_semaphore: Any = None,
) -> None:
    """Inject bot, channel source, backfill hook and semaphore without importing main."""
    global _bot, _channel_source, _backfill_hook, _fetch_semaphore
    if bot is not None:
        _bot = bot
    if channel_source is not None:
        _channel_source = channel_source
    if backfill_hook is not None:
        _backfill_hook = backfill_hook
    if fetch_semaphore is not None:
        _fetch_semaphore = fetch_semaphore


async def ingest_window(
    channels: list[str] | set[str],
    window_start: datetime | str,
    *,
    now: datetime | None = None,
    is_backfill: bool = False,
    channel_source: Any = None,
    web_session: Any = None,
) -> dict[str, list[dict]]:
    """Load posts for channels within the given window.

    marker = max(window_start, cursor) if cursor exists and history is backfilled,
             else max(window_start, now - 30 days).
    Cursor is advanced only if there are no holes (rule 1.7).
    """
    cs = channel_source if channel_source is not None else _channel_source
    if cs is None:
        logger.warning("ingest_window called without channel_source")
        return {}

    now_dt = now or utc_now()
    if isinstance(window_start, str):
        window_start_dt = parse_db_datetime(window_start)
    else:
        window_start_dt = window_start

    sem = get_fetch_semaphore()
    thirty_days_ago = now_dt - timedelta(days=30)

    async def _ingest_one(raw_channel: str) -> tuple[str, list[dict]]:
        clean_username = normalize_channel_username(raw_channel)
        if not clean_username:
            return raw_channel, []

        backfilled_at = get_channel_feed_backfilled_at(clean_username)
        cursor_str, _ = get_channel_scan_cursor(clean_username)

        if backfilled_at is not None and cursor_str is not None:
            cursor_dt = parse_db_datetime(cursor_str)
            marker_dt = max(window_start_dt, cursor_dt)
        else:
            marker_dt = max(window_start_dt, thirty_days_ago)

        marker_str = serialize_datetime(marker_dt)

        try:
            async with sem:
                posts, source_name = await cs.fetch(clean_username, marker_str, web_session=web_session)
        except Exception as exc:
            logger.warning("Failed to fetch posts for feed channel @%s: %s", clean_username, exc)
            return clean_username, []

        try:
            upsert_channel_posts(clean_username, posts, source_name)
        except Exception as exc:
            logger.warning("Failed to upsert channel posts for @%s: %s", clean_username, exc)
            return clean_username, posts

        # Mark backfilled if full 30-day window was fetched
        if is_backfill or marker_dt <= thirty_days_ago:
            try:
                set_channel_feed_backfilled_at(clean_username, now_dt)
            except Exception as exc:
                logger.debug("Failed to set feed_backfilled_at for @%s: %s", clean_username, exc)

        # Rule 1.7: Cursor movement without holes
        if posts:
            max_time = max(p["time"] for p in posts)
            max_id = max((p["id"] for p in posts if p.get("id") is not None), default=None)
            if cursor_str is not None:
                cursor_dt = parse_db_datetime(cursor_str)
                if marker_dt <= cursor_dt:
                    # No hole: we fetched from <= cursor_dt up to now
                    update_channel_scan_cursor(clean_username, max_time, max_id)
                else:
                    # Hole: marker_dt > cursor_dt. Rule 1.7: cursor must NOT be touched!
                    pass
            else:
                # cursor_str is None: only advance cursor if full 30-day backfill
                if is_backfill or marker_dt <= thirty_days_ago:
                    update_channel_scan_cursor(clean_username, max_time, max_id)

        return clean_username, posts

    results = await asyncio.gather(*(_ingest_one(ch) for ch in channels))
    return dict(results)


def schedule_channel_backfill(channel_username: str) -> asyncio.Task | None:
    """Schedule background ingestion for a newly added feed channel (T21)."""
    clean_username = normalize_channel_username(channel_username)
    if not clean_username:
        return None

    if _backfill_hook:
        try:
            return _backfill_hook(clean_username)
        except Exception:
            logger.exception("Failed to invoke channel backfill hook for @%s", clean_username)
            return None

    existing = _backfill_tasks.get(clean_username)
    if existing is not None and not existing.done():
        logger.debug("Backfill already in progress for @%s", clean_username)
        return existing

    async def _runner():
        try:
            now = utc_now()
            window_start = now - timedelta(days=30)
            await ingest_window([clean_username], window_start, now=now, is_backfill=True)
        except Exception as exc:
            logger.warning("Background backfill failed for @%s: %s", clean_username, exc)
        finally:
            _backfill_tasks.pop(clean_username, None)

    try:
        loop = asyncio.get_running_loop()
        task = loop.create_task(_runner())
        _backfill_tasks[clean_username] = task
        return task
    except RuntimeError:
        return None


class FeedTagState(StatesGroup):
    waiting_for_tag = State()


# ---------------------------------------------------------------------------
# Keyboards and Screen Builders
# ---------------------------------------------------------------------------


def build_feed_main_keyboard(total_channels: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="☀️ День", callback_data="feed_period_day"),
                InlineKeyboardButton(text="🗓 Неделя", callback_data="feed_period_week"),
                InlineKeyboardButton(text="📆 Месяц", callback_data="feed_period_month"),
            ],
            [
                InlineKeyboardButton(
                    text=f"📋 Каналы Ленты ({total_channels})",
                    callback_data="feed_channels_0",
                ),
            ],
            [
                InlineKeyboardButton(text="ℹ️ Как добавить", callback_data="feed_help"),
            ],
        ]
    )


def build_feed_channel_actions(sub_id: int, is_paused: bool) -> InlineKeyboardMarkup:
    toggle_btn = (
        InlineKeyboardButton(text="▶️ Возобновить", callback_data=f"feed_resume:{sub_id}")
        if is_paused
        else InlineKeyboardButton(text="⏸ Пауза", callback_data=f"feed_pause:{sub_id}")
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🏷 Тег", callback_data=f"ftag:{sub_id}")],
            [toggle_btn],
            [InlineKeyboardButton(text="🗑 Убрать из Ленты", callback_data=f"feed_del:{sub_id}")],
            [InlineKeyboardButton(text="🔙 К списку", callback_data="feed_channels_0")],
        ]
    )


# ---------------------------------------------------------------------------
# UX: Main screen & Help
# ---------------------------------------------------------------------------


async def render_feed_main(target: types.Message | types.CallbackQuery, user_id: int, edit: bool = False):
    total = count_feed_subscriptions(user_id, include_paused=True)
    text = f"📰 <b>Лента</b> — главное из {total} каналов"
    kb = build_feed_main_keyboard(total)

    if edit and isinstance(target, types.CallbackQuery) and target.message:
        try:
            await target.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
            return
        except TelegramAPIError:
            pass

    if isinstance(target, types.CallbackQuery) and target.message:
        await target.message.answer(text, parse_mode="HTML", reply_markup=kb)
    elif isinstance(target, types.Message):
        await target.answer(text, parse_mode="HTML", reply_markup=kb)


@router.message(Command("feed"))
async def cmd_feed(message: types.Message, state: FSMContext):
    await state.clear()
    await render_feed_main(message, message.chat.id, edit=False)


@router.callback_query(F.data == "feed_main")
async def cb_feed_main(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await render_feed_main(callback, callback.from_user.id, edit=True)
    await callback.answer()


@router.callback_query(F.data == "feed_help")
async def cb_feed_help(callback: types.CallbackQuery):
    text = (
        "ℹ️ <b>Как добавить канал в Ленту</b>\n\n"
        "• Перешли пост из любого публичного канала в этот чат и нажми «📰 В Ленту».\n"
        "• Или отправь ссылку на канал (например, <code>@durov</code> или "
        "<code>https://t.me/durov</code>) и выбери «📰 В Ленту».\n"
        "• Если канал уже есть в твоём дайджесте, открой его карточку в /list_digest "
        "и нажми «📰 В Ленту»."
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🔙 К Ленте", callback_data="feed_main")]]
    )
    if callback.message:
        try:
            await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
        except TelegramAPIError:
            await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("feed_period_"))
async def cb_feed_period_stub(callback: types.CallbackQuery):
    """Placeholder for period building (fully implemented in T24)."""
    await callback.answer("⏳ Сборка Ленты будет подключена в T24.", show_alert=True)


# ---------------------------------------------------------------------------
# UX: Channel List & Card
# ---------------------------------------------------------------------------


@router.callback_query(F.data.startswith("feed_channels_"))
async def cb_feed_channels(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    user_id = callback.from_user.id
    try:
        page = int(callback.data.split("_")[2])
    except (IndexError, ValueError):
        page = 0

    subs = list_feed_subscriptions(user_id, include_paused=True)
    total = len(subs)
    page_size = 8
    start = page * page_size
    page_subs = subs[start : start + page_size]

    if not subs:
        text = "📋 <b>Каналы Ленты</b>\n\nВ Ленте пока нет каналов."
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="ℹ️ Как добавить", callback_data="feed_help")],
                [InlineKeyboardButton(text="🔙 К Ленте", callback_data="feed_main")],
            ]
        )
    else:
        text = f"📋 <b>Каналы Ленты ({total})</b>\nВыбери канал для управления:"
        buttons = []
        for s in page_subs:
            title = s["channel_title"] or s["channel_username"]
            tag_str = f" · #{s['tag']}" if s["tag"] else ""
            status_prefix = "⏸ " if s["is_paused"] else ""
            btn_text = f"📰 {status_prefix}{title}{tag_str}"
            buttons.append([InlineKeyboardButton(text=btn_text, callback_data=f"feed_ch:{s['id']}")])

        nav_row = []
        if page > 0:
            nav_row.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"feed_channels_{page - 1}"))
        if start + page_size < total:
            nav_row.append(InlineKeyboardButton(text="Вперёд ▶️", callback_data=f"feed_channels_{page + 1}"))
        if nav_row:
            buttons.append(nav_row)

        buttons.append([InlineKeyboardButton(text="🔙 К Ленте", callback_data="feed_main")])
        kb = InlineKeyboardMarkup(inline_keyboard=buttons)

    if callback.message:
        try:
            await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
        except TelegramAPIError:
            await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("feed_ch:"))
async def cb_feed_channel_card(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    user_id = callback.from_user.id
    try:
        sub_id = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        await callback.answer("❌ Некорректные данные.")
        return

    sub = get_feed_subscription(user_id, sub_id)
    if not sub:
        await callback.answer("❌ Канал не найден.")
        return

    title_safe = html.escape(sub["channel_title"] or sub["channel_username"])
    status_str = "⏸ На паузе" if sub["is_paused"] else "✅ Активен"
    tag_str = f"#{html.escape(sub['tag'])}" if sub["tag"] else "<i>без тега</i>"

    text = (
        f"📰 Канал <b>{title_safe}</b> (@{html.escape(sub['channel_username'])})\n\n"
        f"🏷 Раздел: {tag_str}\n"
        f"Статус: {status_str}"
    )
    kb = build_feed_channel_actions(sub_id, bool(sub["is_paused"]))

    if callback.message:
        try:
            await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
        except TelegramAPIError:
            await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("feed_pause:"))
async def cb_feed_pause(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    try:
        sub_id = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        await callback.answer("❌ Некорректные данные.")
        return

    set_feed_paused(user_id, sub_id, True)
    await callback.answer("⏸ Канал поставлен на паузу")

    sub = get_feed_subscription(user_id, sub_id)
    if sub and callback.message:
        title_safe = html.escape(sub["channel_title"] or sub["channel_username"])
        tag_str = f"#{html.escape(sub['tag'])}" if sub["tag"] else "<i>без тега</i>"
        text = (
            f"📰 Канал <b>{title_safe}</b> (@{html.escape(sub['channel_username'])})\n\n"
            f"🏷 Раздел: {tag_str}\n"
            "Статус: ⏸ На паузе"
        )
        kb = build_feed_channel_actions(sub_id, True)
        try:
            await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
        except TelegramAPIError:
            pass


@router.callback_query(F.data.startswith("feed_resume:"))
async def cb_feed_resume(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    try:
        sub_id = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        await callback.answer("❌ Некорректные данные.")
        return

    set_feed_paused(user_id, sub_id, False)
    await callback.answer("▶️ Канал возобновлён")

    sub = get_feed_subscription(user_id, sub_id)
    if sub and callback.message:
        title_safe = html.escape(sub["channel_title"] or sub["channel_username"])
        tag_str = f"#{html.escape(sub['tag'])}" if sub["tag"] else "<i>без тега</i>"
        text = (
            f"📰 Канал <b>{title_safe}</b> (@{html.escape(sub['channel_username'])})\n\n"
            f"🏷 Раздел: {tag_str}\n"
            "Статус: ✅ Активен"
        )
        kb = build_feed_channel_actions(sub_id, False)
        try:
            await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
        except TelegramAPIError:
            pass


@router.callback_query(F.data.startswith("feed_del:"))
async def cb_feed_delete(callback: types.CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    try:
        sub_id = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        await callback.answer("❌ Некорректные данные.")
        return

    delete_feed_subscription(user_id, sub_id)
    await callback.answer("🗑 Канал убран из Ленты")
    await cb_feed_channels(callback, state)


# ---------------------------------------------------------------------------
# UX: Tag Management (FSM & Keyboards)
# ---------------------------------------------------------------------------


@router.callback_query(F.data.startswith("ftag:"))
async def cb_feed_tag_menu(callback: types.CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    try:
        sub_id = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        await callback.answer("❌ Некорректные данные.")
        return

    sub = get_feed_subscription(user_id, sub_id)
    if not sub:
        await callback.answer("❌ Канал не найден.")
        return

    existing_tags = list_feed_tags(user_id)[:8]
    await state.set_state(FeedTagState.waiting_for_tag)
    await state.update_data(tag_sub_id=sub_id, tag_choices=existing_tags)

    buttons = []
    # Up to 8 existing tags in 2 columns
    row = []
    for idx, tag in enumerate(existing_tags):
        row.append(InlineKeyboardButton(text=f"#{tag}", callback_data=f"ftag_pick:{idx}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    buttons.append([InlineKeyboardButton(text="✏️ Новый тег", callback_data="ftag_new")])
    buttons.append([InlineKeyboardButton(text="🚫 Без тега", callback_data="ftag_clear")])
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data=f"feed_ch:{sub_id}")])

    title_safe = html.escape(sub["channel_title"] or sub["channel_username"])
    text = (
        f"🏷 <b>Тег канала {title_safe}</b>\n\n"
        "Выбери тег из списка или создай новый.\n\n"
        "⚠️ Тег виден на странице Ленты в Telegraph — её может открыть любой, у кого есть ссылка."
    )

    if callback.message:
        try:
            await callback.message.edit_text(
                text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
            )
        except TelegramAPIError:
            await callback.message.answer(
                text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
            )
    await callback.answer()


@router.callback_query(FeedTagState.waiting_for_tag, F.data.startswith("ftag_pick:"))
async def cb_feed_tag_pick(callback: types.CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    try:
        idx = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        await callback.answer("❌ Некорректные данные.")
        return

    data = await state.get_data()
    sub_id = data.get("tag_sub_id")
    choices = data.get("tag_choices") or []

    if sub_id is None or not (0 <= idx < len(choices)):
        await callback.answer("❌ Тег не найден.")
        return

    tag = choices[idx]
    set_feed_tag(user_id, sub_id, tag)
    await state.clear()
    await callback.answer(f"✅ Установлен тег #{tag}")

    # Return to channel card
    callback.data = f"feed_ch:{sub_id}"
    await cb_feed_channel_card(callback, state)


@router.callback_query(FeedTagState.waiting_for_tag, F.data == "ftag_clear")
async def cb_feed_tag_clear(callback: types.CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    data = await state.get_data()
    sub_id = data.get("tag_sub_id")
    if sub_id:
        set_feed_tag(user_id, sub_id, None)
    await state.clear()
    await callback.answer("✅ Тег снят")

    if sub_id:
        callback.data = f"feed_ch:{sub_id}"
        await cb_feed_channel_card(callback, state)


@router.callback_query(FeedTagState.waiting_for_tag, F.data == "ftag_new")
async def cb_feed_tag_new(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    sub_id = data.get("tag_sub_id")

    text = (
        "✏️ <b>Ввод нового тега</b>\n\n"
        "Отправь тег сообщением в чат (до 24 символов, без пробелов):\n\n"
        "⚠️ Тег виден на странице Ленты в Telegraph — её может открыть любой, у кого есть ссылка."
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🔙 Отмена", callback_data=f"feed_ch:{sub_id}")]]
    )
    if callback.message:
        try:
            await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
        except TelegramAPIError:
            await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.message(FeedTagState.waiting_for_tag, F.text)
async def process_feed_tag_text(message: types.Message, state: FSMContext):
    tag = normalize_feed_tag(message.text)
    if not tag:
        await message.answer(
            "❌ Некорректный тег. Пришли слово до 24 символов (например, <code>новости</code>) или нажми /cancel."
        )
        return

    data = await state.get_data()
    sub_id = data.get("tag_sub_id")
    if not sub_id:
        await state.clear()
        await message.answer("❌ Данные канала утеряны. Открой /feed заново.")
        return

    set_feed_tag(message.chat.id, sub_id, tag)
    await state.clear()
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="📰 К карточке канала", callback_data=f"feed_ch:{sub_id}")]]
    )
    await message.answer(f"✅ Установлен тег <b>#{html.escape(tag)}</b>", parse_mode="HTML", reply_markup=kb)


# ---------------------------------------------------------------------------
# Adding channels: from FSM (setup_digest) and from digest card (feed_copy)
# ---------------------------------------------------------------------------


@router.callback_query(F.data == "feed_add")
async def cb_feed_add_from_fsm(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    channel_username = data.get("channel_username")
    channel_title = data.get("channel_title")

    if not channel_username:
        await callback.answer("❌ Данные канала утеряны.", show_alert=True)
        return

    normalized_username = normalize_channel_username(channel_username)
    user_id = callback.from_user.id

    try:
        sub_id = add_feed_subscription(
            user_id,
            normalized_username,
            channel_title=channel_title,
        )
        schedule_channel_backfill(normalized_username)
    except ValueError as e:
        await callback.answer(f"❌ {e}", show_alert=True)
        return

    await callback.answer("✅ Добавлен в Ленту!")

    title_safe = html.escape(channel_title or channel_username)
    text = f"✅ <b>{title_safe}</b> добавлен в Ленту"
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🏷 Тег", callback_data=f"ftag:{sub_id}"),
                InlineKeyboardButton(text="📰 Открыть Ленту", callback_data="feed_main"),
            ],
            [
                InlineKeyboardButton(text="📡 Ещё и в дайджест", callback_data="digest_setup"),
            ],
        ]
    )

    if callback.message:
        try:
            await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
        except TelegramAPIError:
            await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data.startswith("feed_copy_"))
async def cb_feed_copy_from_digest(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    try:
        digest_sub_id = int(callback.data.split("_")[2])
    except (IndexError, ValueError):
        await callback.answer("❌ Некорректные данные.")
        return

    sub = get_subscription_by_id(user_id, digest_sub_id)
    if not sub:
        await callback.answer("❌ Подписка дайджеста не найдена.", show_alert=True)
        return

    username = sub["channel_username"]
    title = sub["channel_title"]
    tag = sub["tag"]

    try:
        add_feed_subscription(user_id, username, channel_title=title, tag=tag)
        schedule_channel_backfill(username)
        await callback.answer("✅ Канал добавлен в Ленту!")
    except ValueError as e:
        await callback.answer(f"❌ {e}", show_alert=True)
