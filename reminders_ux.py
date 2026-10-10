"""UX helpers and handlers for reminders (notes, snooze, follow-ups)."""

import logging

from aiogram import F, Router, types
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from data.database import update_reminder_note

logger = logging.getLogger(__name__)

router = Router(name="reminders_ux")


class ReminderNoteState(StatesGroup):
    waiting_for_note = State()


def build_reschedule_note_keyboard(reminder_id: int) -> InlineKeyboardMarkup:
    """Button attached to confirmation message allowing the user to attach a note."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📝 Примечание", callback_data=f"rem_note:{reminder_id}")]
        ]
    )


@router.callback_query(F.data.startswith("rem_note:"))
async def cb_rem_note(callback: types.CallbackQuery, state: FSMContext):
    data = callback.data or ""
    parts = data.split(":")
    if len(parts) < 2:
        await callback.answer("❌ Ошибка.")
        return
    try:
        rem_id = int(parts[1])
    except ValueError:
        await callback.answer("❌ Некорректный ID.")
        return

    await state.set_state(ReminderNoteState.waiting_for_note)
    await state.update_data(note_rem_id=rem_id)
    await callback.answer()
    if callback.message:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await callback.message.reply(
            "📝 Введи примечание к напоминанию (до 200 символов):",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отмена", callback_data="rem_note_cancel")]]
            ),
        )


@router.callback_query(F.data == "rem_note_cancel")
async def cb_rem_note_cancel(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        if callback.message:
            await callback.message.delete()
    except Exception:
        pass
    await callback.answer("Ввод примечания отменён.")


@router.message(ReminderNoteState.waiting_for_note)
async def process_reminder_note(message: types.Message, state: FSMContext):
    text = (message.text or "").strip()
    if not text:
        await message.answer("❌ Примечание не должно быть пустым. Введи текст (до 200 символов):")
        return
    if len(text) > 200:
        await message.answer(f"❌ Примечание слишком длинное ({len(text)}/200 симв.). Попробуй покороче:")
        return

    data = await state.get_data()
    rem_id = data.get("note_rem_id")
    if not rem_id:
        await message.answer("❌ Ошибка: не найден ID напоминания.")
        await state.clear()
        return

    updated = update_reminder_note(message.chat.id, int(rem_id), text)
    await state.clear()
    if updated:
        await message.answer("✅ Примечание сохранено!")
    else:
        await message.answer("❌ Не удалось сохранить примечание (напоминание не найдено или уже отправлено).")
