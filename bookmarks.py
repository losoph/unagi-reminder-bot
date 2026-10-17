"""Модуль закладок (База знаний).

Содержит FSM-состояния, экраны навигации, рендеринг карточек и обработчики
для команд /save, /saved и сопутствующих callback-запросов закладок.
"""

import asyncio
import html
import logging
from typing import Any

from aiogram import F, Router, types
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InaccessibleMessage,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from data.database import (
    add_saved_message,
    delete_message,
    delete_saved_message,
    get_saved_message_by_id,
    get_saved_messages,
    get_scheduled_message_by_delivered_message_id,
    get_user_tags,
    serialize_datetime,
    update_saved_message_tag,
    utc_now,
)
from time_utils import display_db_datetime

logger = logging.getLogger(__name__)

router = Router(name="bookmarks")

_render_screen: Any = None


def configure(*, render_screen: Any = None) -> None:
    """Внедрение центрального диспетчера экранов для возврата назад."""
    global _render_screen
    if render_screen is not None:
        _render_screen = render_screen


class SaveState(StatesGroup):
    waiting_for_tag = State()
    waiting_for_new_tag = State()
    waiting_for_reminder_tag = State()


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


def get_message_full_text(msg: types.Message) -> str:
    return msg.text or msg.caption or "🖼 Медиафайл (без текста)"


def get_message_preview(msg: types.Message) -> tuple[str, str]:
    text = msg.text or msg.caption or "🖼 Медиафайл"
    normalized_text = text.replace("\n", " ")
    preview = normalized_text[:37] + "..." if len(normalized_text) > 40 else normalized_text

    source = "Твой текст"
    if msg.forward_origin:
        if msg.forward_origin.type == "channel":
            source = getattr(msg.forward_origin.chat, "title", "Канал")
        elif msg.forward_origin.type == "user":
            source = getattr(msg.forward_origin.sender_user, "first_name", "Пользователь")
        elif msg.forward_origin.type == "hidden_user":
            source = getattr(msg.forward_origin, "sender_user_name", "Скрытый пользователь")
        elif msg.forward_origin.type == "chat":
            source = getattr(msg.forward_origin.chat, "title", "Группа")

    return preview, source


def get_replied_sent_schedule(message: types.Message):
    replied = message.reply_to_message
    if not replied:
        return None
    return get_scheduled_message_by_delivered_message_id(user_id_from_message(message), replied.message_id)


def build_back_home_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🔙 Назад", callback_data="flow_back"),
                InlineKeyboardButton(text="🏠 В начало", callback_data="flow_home"),
            ]
        ]
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
        normalized_full = (full_text or "").replace("\n", " ")
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
        InlineKeyboardButton(text="🏠 В начало", callback_data="flow_home"),
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
            ],
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
    tags = get_user_tags(user_id_from_message(message))
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


# === Message Handlers ===


@router.message(Command("saved"))
async def cmd_saved(message: types.Message, state: FSMContext):
    await state.clear()
    await push_screen(state, "kb_tags")
    await render_kb_tags_screen(user_id_from_message(message), message, state)


@router.message(Command("save", "s"))
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


@router.message(SaveState.waiting_for_new_tag)
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

    update_saved_message_tag(user_id_from_message(message), msg_id, new_tag)
    await pop_screen(state)
    confirm = await message.answer(f"✅ Перенесено в #{new_tag}!")
    await state.set_state(None)

    await asyncio.sleep(2)
    try:
        await confirm.delete()
        await message.delete()
    except TelegramAPIError:
        pass

    await render_kb_detail_screen(user_id_from_message(message), msg_id, message, state)


@router.message(SaveState.waiting_for_tag)
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
    try:
        add_saved_message(user_id_from_message(message), full_text, source, tag, saved_at)
    except ValueError as exc:
        await message.answer(f"🚫 {exc}")
        return

    try:
        await message.delete()
        if message.bot:
            if prompt_msg_id:
                await message.bot.delete_message(user_id_from_message(message), prompt_msg_id)
            if orig_msg_id:
                await message.bot.delete_message(user_id_from_message(message), orig_msg_id)
            if target_message_id:
                await message.bot.delete_message(user_id_from_message(message), target_message_id)
            if command_message_id:
                await message.bot.delete_message(user_id_from_message(message), command_message_id)
    except TelegramAPIError:
        pass

    if scheduled_db_id:
        delete_message(user_id_from_message(message), scheduled_db_id)

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


# === Callback Query Handlers ===


@router.callback_query(F.data == "menu_kb")
async def callback_menu_bookmarks(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await push_screen(state, "kb_tags")
    await render_kb_tags_screen(user_id_from_callback(callback), callback, state)


@router.callback_query(F.data == "sent_save")
async def save_sent_reminder(callback: CallbackQuery, state: FSMContext):
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


@router.callback_query(F.data == "bookmark_setup")
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


@router.callback_query(F.data.startswith("kb_tag_idx_"))
async def handle_kb_tag_selection(callback: CallbackQuery, state: FSMContext):
    idx = int(callback.data.split("_")[-1])
    data = await state.get_data()
    tags_list = data.get("kb_tags_list", [])
    if idx < len(tags_list):
        tag = tags_list[idx]
        await push_screen(state, "kb_list", tag=tag, page=1)
        await render_kb_list_screen(user_id_from_callback(callback), tag, 1, callback, state)
    else:
        await callback.answer("❌ Ошибка выбора категории.")


@router.callback_query(F.data == "kb_tag_all")
async def handle_kb_tag_all(callback: CallbackQuery, state: FSMContext):
    await push_screen(state, "kb_list", tag=None, page=1)
    await render_kb_list_screen(user_id_from_callback(callback), None, 1, callback, state)


@router.callback_query(F.data == "kb_view_tags")
async def handle_kb_view_tags(callback: CallbackQuery, state: FSMContext):
    await push_screen(state, "kb_tags")
    await render_kb_tags_screen(user_id_from_callback(callback), callback, state)


@router.callback_query(F.data == "kb_page_prev")
async def handle_kb_page_prev(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    tag = data.get("kb_current_tag")
    page = data.get("kb_current_page", 1)
    prev_page = max(1, page - 1)
    history = list(data.get("screen_history", []))
    if history and history[-1].get("screen") == "kb_list":
        history[-1]["args"]["page"] = prev_page
        await state.update_data(screen_history=history)
    await render_kb_list_screen(user_id_from_callback(callback), tag, prev_page, callback, state)


@router.callback_query(F.data == "kb_page_next")
async def handle_kb_page_next(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    tag = data.get("kb_current_tag")
    page = data.get("kb_current_page", 1)
    next_page = page + 1
    history = list(data.get("screen_history", []))
    if history and history[-1].get("screen") == "kb_list":
        history[-1]["args"]["page"] = next_page
        await state.update_data(screen_history=history)
    await render_kb_list_screen(user_id_from_callback(callback), tag, next_page, callback, state)


@router.callback_query(F.data.startswith("kb_sel_idx_"))
async def handle_kb_sel_idx(callback: CallbackQuery, state: FSMContext):
    idx = int(callback.data.split("_")[-1])
    data = await state.get_data()
    page_msg_ids = data.get("kb_page_msg_ids", [])
    if idx < len(page_msg_ids):
        msg_id = page_msg_ids[idx]
        await push_screen(state, "kb_detail", msg_id=msg_id)
        await render_kb_detail_screen(user_id_from_callback(callback), msg_id, callback, state)
    else:
        await callback.answer("❌ Ошибка выбора закладки.")


@router.callback_query(F.data == "kb_ignore")
async def handle_kb_ignore(callback: CallbackQuery):
    await callback.answer()


@router.callback_query(F.data.startswith("bkmv_init_"))
async def handle_bkmv_init(callback: CallbackQuery, state: FSMContext):
    msg_id = int(callback.data.split("_")[-1])
    await push_screen(state, "kb_move", msg_id=msg_id)
    await render_kb_move_screen(user_id_from_callback(callback), msg_id, callback, state)


@router.callback_query(F.data.startswith("kb_mv_idx_"))
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
        update_saved_message_tag(user_id_from_callback(callback), msg_id, new_tag)
        await callback.answer(f"✅ Перенесено в #{new_tag}")
        await pop_screen(state)
        await render_kb_detail_screen(user_id_from_callback(callback), msg_id, callback, state)
    else:
        await callback.answer("❌ Ошибка переноса.")


@router.callback_query(F.data.startswith("bkdel_"))
async def handle_bkdel(callback: CallbackQuery, state: FSMContext):
    msg_id = int(callback.data.split("_")[-1])
    delete_saved_message(user_id_from_callback(callback), msg_id)
    await callback.answer("✅ Закладка удалена!")
    await pop_screen(state)

    data = await state.get_data()
    history = data.get("screen_history", [])
    if history:
        prev = history[-1]
        if _render_screen is not None:
            await _render_screen(user_id_from_callback(callback), prev, callback, state)
        elif prev.get("screen") == "kb_list":
            args = prev.get("args", {})
            await render_kb_list_screen(
                user_id_from_callback(callback),
                args.get("tag"),
                args.get("page", 1),
                callback,
                state,
            )
        else:
            await render_kb_tags_screen(user_id_from_callback(callback), callback, state)
    else:
        await render_kb_tags_screen(user_id_from_callback(callback), callback, state)
