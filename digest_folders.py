"""Управление папками подписок дайджеста: пауза, удаление, перенос и порядок показа.

«Папка» — это тег подписки (см. digest_settings). Меню «📁 Папки» собирает массовые действия
над всеми каналами папки: пауза/возобновление, удаление, перенос в другую папку, в другой
выпуск (ежедневный/еженедельный/ежемесячный), в Ленту (с сохранением тега) и порядок папок
в дайджесте.
"""

import asyncio
import html
import logging

from aiogram import F, Router, types
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from data.database import (
    add_subscription,
    delete_folder_subscriptions,
    delete_subscription,
    get_folder_subscriptions,
    list_subscription_folders,
    move_folder_subscriptions,
    serialize_datetime,
    set_folder_order,
    set_folder_paused,
    utc_now,
)
from digest_settings import (
    _PERIOD_TITLES,
    edit_or_answer,
    get_next_digest_time,
    parse_callback_int_suffix,
    parse_callback_strip_prefix,
    reschedule_user_period,
    resolve_digest_settings,
    user_id_from_callback,
    user_id_from_message,
)
from time_utils import local_now

logger = logging.getLogger(__name__)

router = Router(name="digest_folders")

NO_FOLDER = "Без папки"
MAX_FOLDER_NAME = 24  # как у тега Ленты: тег переезжает в Ленту без обрезки


class FolderState(StatesGroup):
    waiting_for_target_name = State()


def _title(name: str | None) -> str:
    return html.escape(name) if name else NO_FOLDER


def _back_to_folders() -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton(text="⬅️ К папкам", callback_data="fldmenu")]


async def _load_names(state: FSMContext, user_id: int, *, refresh: bool = False) -> list[str | None]:
    data = await state.get_data()
    names = data.get("fld_names")
    if refresh or names is None:
        folders = await asyncio.to_thread(list_subscription_folders, user_id)
        names = [folder["name"] for folder in folders]
        await state.update_data(fld_names=names)
    return names


async def _pick(callback: CallbackQuery, state: FSMContext, prefix: str) -> tuple[list[str | None], int] | None:
    """Индекс папки из callback_data; None, если папка исчезла (список устарел)."""
    idx = parse_callback_int_suffix(callback.data, prefix)
    user_id = user_id_from_callback(callback)
    names = await _load_names(state, user_id)
    if idx is None or idx >= len(names):
        names = await _load_names(state, user_id, refresh=True)
        await callback.answer("❌ Список папок обновился, открой папку заново.", show_alert=True)
        await _render_menu(callback, state)
        return None
    return names, idx


async def _render_menu(callback: CallbackQuery, state: FSMContext) -> None:
    user_id = user_id_from_callback(callback)
    folders = await asyncio.to_thread(list_subscription_folders, user_id)
    await state.update_data(fld_names=[folder["name"] for folder in folders])
    if not folders:
        await edit_or_answer(
            callback,
            "📁 <b>Папки</b>\n\nПодписок пока нет.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="digest_settings")]]
            ),
        )
        return
    lines = ["📁 <b>Папки</b>", "", "В таком порядке каналы идут в дайджесте (внутри каждого выпуска):"]
    rows: list[list[InlineKeyboardButton]] = []
    for idx, folder in enumerate(folders):
        suffix = f", ⏸ {folder['paused']}" if folder["paused"] else ""
        lines.append(f"{idx + 1}. {_title(folder['name'])} — {folder['total']}{suffix}")
        label = (folder["name"] or NO_FOLDER)[:28]
        rows.append([InlineKeyboardButton(text=f"📁 {label} ({folder['total']})", callback_data=f"fld_{idx}")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="digest_settings")])
    await edit_or_answer(callback, "\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


async def _render_folder(callback: CallbackQuery, state: FSMContext, idx: int, names: list[str | None]) -> None:
    user_id = user_id_from_callback(callback)
    name = names[idx]
    subs = await asyncio.to_thread(get_folder_subscriptions, user_id, name)
    if not subs:
        await _render_menu(callback, state)
        return
    paused = sum(1 for sub in subs if sub["is_paused"])
    by_period = {period: sum(1 for sub in subs if sub["period"] == period) for period in _PERIOD_TITLES}
    lines = [f"📁 <b>{_title(name)}</b>", "", f"Каналов: {len(subs)}" + (f", ⏸ на паузе: {paused}" if paused else "")]
    lines.append(
        "Выпуски: " + ", ".join(f"{_PERIOD_TITLES[p].lower()} — {n}" for p, n in by_period.items() if n)
    )
    lines.append("")
    for sub in subs[:15]:
        mark = "⏸" if sub["is_paused"] else "📰"
        lines.append(f"• {mark} {html.escape(sub['channel_title'] or sub['channel_username'])}")
    if len(subs) > 15:
        lines.append(f"…и ещё {len(subs) - 15}")

    rows: list[list[InlineKeyboardButton]] = []
    toggle = []
    if paused < len(subs):
        toggle.append(InlineKeyboardButton(text="⏸ Пауза", callback_data=f"fldp_{idx}"))
    if paused:
        toggle.append(InlineKeyboardButton(text="▶️ Возобновить", callback_data=f"fldr_{idx}"))
    rows.append(toggle)
    rows.append(
        [
            InlineKeyboardButton(text="📂 В другую папку", callback_data=f"fldm_{idx}"),
            InlineKeyboardButton(text="🔁 В другой выпуск", callback_data=f"fldw_{idx}"),
        ]
    )
    rows.append([InlineKeyboardButton(text="📰 В Ленту", callback_data=f"fldf_{idx}")])
    if name:
        rows.append(
            [
                InlineKeyboardButton(text="⬆️ Выше в дайджесте", callback_data=f"fldu_{idx}"),
                InlineKeyboardButton(text="⬇️ Ниже", callback_data=f"fldv_{idx}"),
            ]
        )
    rows.append([InlineKeyboardButton(text="🗑 Удалить все каналы", callback_data=f"fldd_{idx}")])
    rows.append(_back_to_folders())
    await edit_or_answer(callback, "\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data == "fldmenu")
async def open_folders_menu(callback: CallbackQuery, state: FSMContext):
    await state.set_state(None)
    await _render_menu(callback, state)
    await callback.answer()


@router.callback_query(F.data.startswith("fld_"))
async def open_folder(callback: CallbackQuery, state: FSMContext):
    picked = await _pick(callback, state, "fld_")
    if not picked:
        return
    names, idx = picked
    await _render_folder(callback, state, idx, names)
    await callback.answer()


@router.callback_query(F.data.startswith("fldp_"))
async def pause_folder(callback: CallbackQuery, state: FSMContext):
    picked = await _pick(callback, state, "fldp_")
    if not picked:
        return
    names, idx = picked
    changed = await asyncio.to_thread(set_folder_paused, user_id_from_callback(callback), names[idx], True)
    await _render_folder(callback, state, idx, names)
    await callback.answer(f"⏸ На паузе: {changed}")


@router.callback_query(F.data.startswith("fldr_"))
async def resume_folder(callback: CallbackQuery, state: FSMContext):
    picked = await _pick(callback, state, "fldr_")
    if not picked:
        return
    names, idx = picked
    user_id = user_id_from_callback(callback)
    changed = await asyncio.to_thread(set_folder_paused, user_id, names[idx], False)
    # Как при «Возобновить все»: сдвигаем расписание, чтобы выпуск не ушёл мгновенно.
    for period in _PERIOD_TITLES:
        await asyncio.to_thread(reschedule_user_period, user_id, period)
    await _render_folder(callback, state, idx, names)
    await callback.answer(f"▶️ Возобновлено: {changed}")


@router.callback_query(F.data.startswith("fldd_"))
async def confirm_delete_folder(callback: CallbackQuery, state: FSMContext):
    picked = await _pick(callback, state, "fldd_")
    if not picked:
        return
    names, idx = picked
    subs = await asyncio.to_thread(get_folder_subscriptions, user_id_from_callback(callback), names[idx])
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"🗑 Да, удалить ({len(subs)})", callback_data=f"flddy_{idx}")],
            [InlineKeyboardButton(text="⬅️ Отмена", callback_data=f"fld_{idx}")],
        ]
    )
    await edit_or_answer(
        callback,
        f"⚠️ <b>Удалить все каналы папки «{_title(names[idx])}»?</b>\n"
        f"Будет удалено подписок: {len(subs)}. Действие необратимо.",
        reply_markup=keyboard,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("flddy_"))
async def delete_folder(callback: CallbackQuery, state: FSMContext):
    picked = await _pick(callback, state, "flddy_")
    if not picked:
        return
    names, idx = picked
    removed = await asyncio.to_thread(delete_folder_subscriptions, user_id_from_callback(callback), names[idx])
    await _render_menu(callback, state)
    await callback.answer(f"🗑 Удалено подписок: {removed}")


@router.callback_query(F.data.startswith("fldm_"))
async def open_move_to_folder(callback: CallbackQuery, state: FSMContext):
    picked = await _pick(callback, state, "fldm_")
    if not picked:
        return
    names, idx = picked
    rows: list[list[InlineKeyboardButton]] = []
    for target_idx, target in enumerate(names):
        if target_idx == idx:
            continue
        label = (target or NO_FOLDER)[:28]
        rows.append([InlineKeyboardButton(text=f"📁 {label}", callback_data=f"fldmt_{idx}_{target_idx}")])
    if names[idx] is not None and None not in names:
        rows.append([InlineKeyboardButton(text=f"📁 {NO_FOLDER}", callback_data=f"fldmo_{idx}")])
    rows.append([InlineKeyboardButton(text="✍️ Новая папка", callback_data=f"fldmn_{idx}")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"fld_{idx}")])
    await edit_or_answer(
        callback,
        f"📂 Куда перенести все каналы папки «{_title(names[idx])}»?\n"
        "Если выбрать существующую папку, каналы объединятся с её содержимым.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


async def _finish_folder_move(state: FSMContext, user_id: int, source, target) -> int:
    moved = await asyncio.to_thread(move_folder_subscriptions, user_id, source, target)
    await state.update_data(fld_names=None)
    return moved


@router.callback_query(F.data.startswith("fldmt_"))
async def move_folder_to_existing(callback: CallbackQuery, state: FSMContext):
    payload = parse_callback_strip_prefix(callback.data, "fldmt_")
    try:
        idx_str, target_str = (payload or "").split("_", 1)
        idx, target_idx = int(idx_str), int(target_str)
    except ValueError:
        await callback.answer("❌ Некорректные данные.", show_alert=True)
        return
    user_id = user_id_from_callback(callback)
    names = await _load_names(state, user_id)
    if idx >= len(names) or target_idx >= len(names):
        await callback.answer("❌ Список папок обновился, открой папку заново.", show_alert=True)
        await _render_menu(callback, state)
        return
    moved = await _finish_folder_move(state, user_id, names[idx], names[target_idx])
    await _render_menu(callback, state)
    await callback.answer(f"📂 Перенесено в «{names[target_idx] or NO_FOLDER}»: {moved}")


@router.callback_query(F.data.startswith("fldmo_"))
async def move_folder_to_none(callback: CallbackQuery, state: FSMContext):
    picked = await _pick(callback, state, "fldmo_")
    if not picked:
        return
    names, idx = picked
    moved = await _finish_folder_move(state, user_id_from_callback(callback), names[idx], None)
    await _render_menu(callback, state)
    await callback.answer(f"📂 Убрано из папки: {moved}")


@router.callback_query(F.data.startswith("fldmn_"))
async def prompt_target_folder_name(callback: CallbackQuery, state: FSMContext):
    picked = await _pick(callback, state, "fldmn_")
    if not picked:
        return
    names, idx = picked
    await state.update_data(fld_source=names[idx])
    await state.set_state(FolderState.waiting_for_target_name)
    await edit_or_answer(
        callback,
        f"✍️ Пришли название новой папки (до {MAX_FOLDER_NAME} символов). "
        f"В неё переедут все каналы папки «{_title(names[idx])}».",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отмена", callback_data=f"fld_{idx}")]]
        ),
    )
    await callback.answer()


@router.message(FolderState.waiting_for_target_name)
async def process_target_folder_name(message: types.Message, state: FSMContext):
    name = (message.text or "").strip().lstrip("#").strip()
    if not name:
        await message.answer("❌ Название папки не должно быть пустым. Попробуй ещё раз.")
        return
    if len(name) > MAX_FOLDER_NAME:
        await message.answer(f"❌ Слишком длинное название (максимум {MAX_FOLDER_NAME} символов).")
        return
    user_id = user_id_from_message(message)
    data = await state.get_data()
    source = data.get("fld_source")
    await state.set_state(None)
    moved = await _finish_folder_move(state, user_id, source, name)
    await message.answer(
        f"📂 Перенесено каналов в папку <b>{html.escape(name)}</b>: {moved}.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[_back_to_folders()]),
    )


@router.callback_query(F.data.startswith("fldw_"))
async def open_move_to_period(callback: CallbackQuery, state: FSMContext):
    picked = await _pick(callback, state, "fldw_")
    if not picked:
        return
    names, idx = picked
    rows = [
        [InlineKeyboardButton(text=f"↪️ {title}", callback_data=f"fldwp_{idx}_{period}")]
        for period, title in _PERIOD_TITLES.items()
    ]
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"fld_{idx}")])
    await edit_or_answer(
        callback,
        f"🔁 В какой выпуск перенести все каналы папки «{_title(names[idx])}»?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("fldwp_"))
async def move_folder_to_period(callback: CallbackQuery, state: FSMContext):
    payload = parse_callback_strip_prefix(callback.data, "fldwp_")
    try:
        idx_str, period = (payload or "").split("_", 1)
        idx = int(idx_str)
    except ValueError:
        await callback.answer("❌ Некорректные данные.", show_alert=True)
        return
    if period not in _PERIOD_TITLES:
        await callback.answer("❌ Некорректные данные.", show_alert=True)
        return
    user_id = user_id_from_callback(callback)
    names = await _load_names(state, user_id)
    if idx >= len(names):
        await callback.answer("❌ Список папок обновился, открой папку заново.", show_alert=True)
        await _render_menu(callback, state)
        return
    subs = await asyncio.to_thread(get_folder_subscriptions, user_id, names[idx])
    settings = resolve_digest_settings(user_id, period)
    next_send_at = serialize_datetime(get_next_digest_time(period, local_now(), settings))
    moved = failed = 0
    for sub in subs:
        if sub["period"] == period:
            continue
        try:
            new_id = await asyncio.to_thread(
                add_subscription,
                user_id,
                sub["channel_username"],
                sub["channel_title"],
                period,
                sub["last_scraped_at"] or serialize_datetime(utc_now()),
                next_send_at,
                sub["tag"],
            )
        except ValueError:
            failed += 1
            continue
        if new_id != sub["id"]:
            await asyncio.to_thread(delete_subscription, user_id, sub["id"])
        moved += 1
    await _render_folder(callback, state, idx, names)
    note = f"↪️ В {_PERIOD_TITLES[period].lower()} выпуск: {moved}"
    if failed:
        note += f", не удалось: {failed}"
    await callback.answer(note, show_alert=bool(failed))


@router.callback_query(F.data.startswith("fldf_"))
async def open_move_to_feed(callback: CallbackQuery, state: FSMContext):
    picked = await _pick(callback, state, "fldf_")
    if not picked:
        return
    names, idx = picked
    rows = [
        [InlineKeyboardButton(text="📰 Перенести (убрать из дайджеста)", callback_data=f"fldfm_{idx}")],
        [InlineKeyboardButton(text="📑 Скопировать (оставить и там)", callback_data=f"fldfc_{idx}")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"fld_{idx}")],
    ]
    tag_note = (
        f"Тег «{html.escape(names[idx][:MAX_FOLDER_NAME])}» сохранится как раздел Ленты."
        if names[idx]
        else "У каналов нет папки, поэтому и раздела в Ленте не будет."
    )
    await edit_or_answer(
        callback,
        f"📰 Добавить каналы папки «{_title(names[idx])}» в Ленту?\n{tag_note}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


async def _folder_to_feed(callback: CallbackQuery, state: FSMContext, prefix: str, remove_from_digest: bool):
    picked = await _pick(callback, state, prefix)
    if not picked:
        return
    names, idx = picked
    user_id = user_id_from_callback(callback)
    # Импорт внутри функции: feed тяжёлый и сам зависит от настроек дайджеста.
    from data.database import add_feed_subscription
    from feed import schedule_channel_backfill

    subs = await asyncio.to_thread(get_folder_subscriptions, user_id, names[idx])
    added = 0
    limit_error: str | None = None
    for sub in subs:
        try:
            await asyncio.to_thread(
                add_feed_subscription, user_id, sub["channel_username"], sub["channel_title"], sub["tag"]
            )
        except ValueError as exc:
            limit_error = str(exc)
            break
        schedule_channel_backfill(sub["channel_username"])
        added += 1
        if remove_from_digest:
            await asyncio.to_thread(delete_subscription, user_id, sub["id"])
    if remove_from_digest:
        await _render_menu(callback, state)
    else:
        await _render_folder(callback, state, idx, names)
    note = f"📰 В Ленте: {added}"
    if limit_error:
        note += f"\n❌ {limit_error}"
    await callback.answer(note, show_alert=bool(limit_error))


@router.callback_query(F.data.startswith("fldfm_"))
async def move_folder_to_feed(callback: CallbackQuery, state: FSMContext):
    await _folder_to_feed(callback, state, "fldfm_", remove_from_digest=True)


@router.callback_query(F.data.startswith("fldfc_"))
async def copy_folder_to_feed(callback: CallbackQuery, state: FSMContext):
    await _folder_to_feed(callback, state, "fldfc_", remove_from_digest=False)


async def _shift_folder(callback: CallbackQuery, state: FSMContext, prefix: str, delta: int) -> None:
    picked = await _pick(callback, state, prefix)
    if not picked:
        return
    names, idx = picked
    user_id = user_id_from_callback(callback)
    named = [name for name in names if name]
    name = names[idx]
    if name is None:
        await callback.answer("Эта группа всегда идёт последней.")
        return
    pos = named.index(name)
    new_pos = pos + delta
    if not 0 <= new_pos < len(named):
        await callback.answer("Уже крайняя." if delta < 0 else "Уже последняя.")
        return
    named[pos], named[new_pos] = named[new_pos], named[pos]
    # Порядок хранится целиком: после первой перестановки он фиксируется для всех папок.
    await asyncio.to_thread(set_folder_order, user_id, named)
    names = await _load_names(state, user_id, refresh=True)
    await _render_folder(callback, state, names.index(name), names)
    await callback.answer(f"Теперь №{new_pos + 1} в дайджесте.")


@router.callback_query(F.data.startswith("fldu_"))
async def move_folder_up(callback: CallbackQuery, state: FSMContext):
    await _shift_folder(callback, state, "fldu_", -1)


@router.callback_query(F.data.startswith("fldv_"))
async def move_folder_down(callback: CallbackQuery, state: FSMContext):
    await _shift_folder(callback, state, "fldv_", 1)

