"""User preferences for timezone and morning/day/evening hours (T15)."""

from datetime import datetime, tzinfo
import logging

from aiogram import F, Router, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from data.database import get_user_prefs, upsert_user_prefs
from time_utils import get_app_timezone, parse_timezone

logger = logging.getLogger(__name__)

router = Router(name="user_prefs")

DEFAULT_MORNING_HOUR = 9
DEFAULT_DAY_HOUR = 14
DEFAULT_EVENING_HOUR = 20

POPULAR_CITIES = [
    ("Калининград (UTC+2)", "Europe/Kaliningrad"),
    ("Москва (UTC+3)", "Europe/Moscow"),
    ("Самара (UTC+4)", "Europe/Samara"),
    ("Екатеринбург (UTC+5)", "Europe/Yekaterinburg"),
    ("Омск (UTC+6)", "Europe/Omsk"),
    ("Новосибирск (UTC+7)", "Europe/Novosibirsk"),
    ("Красноярск (UTC+7)", "Europe/Krasnoyarsk"),
    ("Иркутск (UTC+8)", "Europe/Irkutsk"),
    ("Якутск (UTC+9)", "Europe/Yakutsk"),
    ("Владивосток (UTC+10)", "Europe/Vladivostok"),
    ("Магадан (UTC+11)", "Europe/Magadan"),
    ("Камчатка (UTC+12)", "Asia/Kamchatka"),
]

CITY_TIMEZONE_MAP = {
    "калининград": "Europe/Kaliningrad",
    "москва": "Europe/Moscow",
    "самара": "Europe/Samara",
    "екатеринбург": "Europe/Yekaterinburg",
    "омск": "Europe/Omsk",
    "новосибирск": "Europe/Novosibirsk",
    "красноярск": "Europe/Krasnoyarsk",
    "иркутск": "Europe/Irkutsk",
    "якутск": "Europe/Yakutsk",
    "владивосток": "Europe/Vladivostok",
    "магадан": "Europe/Magadan",
    "камчатка": "Asia/Kamchatka",
    "петербург": "Europe/Moscow",
    "спб": "Europe/Moscow",
}


class UserPrefsState(StatesGroup):
    waiting_for_tz_text = State()


def user_tz(user_id: int) -> tzinfo:
    """Returns the user's configured timezone or default APP_TIMEZONE."""
    prefs = get_user_prefs(user_id)
    if prefs and prefs.get("timezone"):
        tz_obj, _ = parse_timezone(prefs["timezone"])
        if tz_obj:
            return tz_obj
    return get_app_timezone()


def user_now(user_id: int) -> datetime:
    """Current moment in user's timezone."""
    return datetime.now(user_tz(user_id))


def day_part_hours(user_id: int) -> dict[str, int]:
    """Returns {'morning': int, 'day': int, 'evening': int} for the user."""
    prefs = get_user_prefs(user_id)
    if prefs:
        return {
            "morning": prefs.get("morning_hour") or DEFAULT_MORNING_HOUR,
            "day": prefs.get("day_hour") or DEFAULT_DAY_HOUR,
            "evening": prefs.get("evening_hour") or DEFAULT_EVENING_HOUR,
        }
    return {
        "morning": DEFAULT_MORNING_HOUR,
        "day": DEFAULT_DAY_HOUR,
        "evening": DEFAULT_EVENING_HOUR,
    }


def validate_hours(morning: int, day: int, evening: int) -> tuple[bool, str]:
    """Validates that morning < day < evening and values in allowed ranges."""
    if not (5 <= morning <= 11):
        return False, "Час утра должен быть от 5 до 11."
    if not (12 <= day <= 16):
        return False, "Час дня должен быть от 12 до 16."
    if not (17 <= evening <= 23):
        return False, "Час вечера должен быть от 17 до 23."
    if not (morning < day < evening):
        return False, f"Должно соблюдаться: утро ({morning}) < день ({day}) < вечер ({evening})."
    return True, ""


def render_prefs_main_text(user_id: int) -> str:
    tz = user_tz(user_id)
    now = datetime.now(tz)
    hours = day_part_hours(user_id)
    prefs = get_user_prefs(user_id)
    tz_name = prefs.get("timezone") if (prefs and prefs.get("timezone")) else str(get_app_timezone())
    offset_str = now.strftime("%z")
    if len(offset_str) == 5:
        offset_formatted = f"UTC{offset_str[:3]}:{offset_str[3:]}"
    else:
        offset_formatted = offset_str

    return (
        f"⚙️ <b>Время и часовой пояс</b>\n\n"
        f"🌍 Часовой пояс: <b>{tz_name}</b> ({offset_formatted})\n"
        f"🕒 Текущее время: <b>{now.strftime('%H:%M')}</b>\n\n"
        f"🌅 Утро: <b>{hours['morning']:02d}:00</b> (5–11)\n"
        f"☀️ День: <b>{hours['day']:02d}:00</b> (12–16)\n"
        f"🌙 Вечер: <b>{hours['evening']:02d}:00</b> (17–23)\n\n"
        f"<i>Используются для напоминаний и быстрых кнопок.</i>"
    )


def build_prefs_main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🌍 Сменить часовой пояс", callback_data="prefs_tz_menu")],
            [
                InlineKeyboardButton(text="🌅 Утро", callback_data="prefs_hour_menu:morning"),
                InlineKeyboardButton(text="☀️ День", callback_data="prefs_hour_menu:day"),
                InlineKeyboardButton(text="🌙 Вечер", callback_data="prefs_hour_menu:evening"),
            ],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="flow_home")],
        ]
    )


def build_prefs_tz_keyboard() -> InlineKeyboardMarkup:
    buttons = []
    row = []
    for idx, (label, _) in enumerate(POPULAR_CITIES):
        city_name = label.split(" (")[0]
        offset = label.split("(")[1].rstrip(")")
        row.append(InlineKeyboardButton(text=f"{city_name} ({offset})", callback_data=f"prefs_set_city:{idx}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton(text="✍️ Ввести текстом (IANA / UTC+N)", callback_data="prefs_tz_text_prompt")])
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="prefs_main")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def build_prefs_hour_keyboard(part: str, current_hours: dict[str, int]) -> InlineKeyboardMarkup:
    if part == "morning":
        hour_range = range(5, 12)
    elif part == "day":
        hour_range = range(12, 17)
    else:
        hour_range = range(17, 24)

    buttons = []
    row = []
    for h in hour_range:
        is_cur = " •" if current_hours.get(part) == h else ""
        row.append(InlineKeyboardButton(text=f"{h:02d}:00{is_cur}", callback_data=f"prefs_set_hour:{part}:{h}"))
        if len(row) == 4:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="prefs_main")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@router.message(Command("prefs", "timezone", "time"))
async def cmd_user_prefs(message: types.Message, state: FSMContext):
    await state.clear()
    text = render_prefs_main_text(message.chat.id)
    await message.answer(text, parse_mode="HTML", reply_markup=build_prefs_main_keyboard())


@router.callback_query(F.data.in_({"prefs_menu", "prefs_main"}))
async def cb_user_prefs_main(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    text = render_prefs_main_text(callback.message.chat.id)
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=build_prefs_main_keyboard())
    except Exception:
        await callback.message.answer(text, parse_mode="HTML", reply_markup=build_prefs_main_keyboard())
    await callback.answer()


@router.callback_query(F.data == "prefs_tz_menu")
async def cb_prefs_tz_menu(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    text = "🌍 <b>Выбор часового пояса</b>\n\nВыбери свой город из списка или пришли название IANA / UTC+N текстом:"
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=build_prefs_tz_keyboard())
    except Exception:
        await callback.message.answer(text, parse_mode="HTML", reply_markup=build_prefs_tz_keyboard())
    await callback.answer()


@router.callback_query(F.data.startswith("prefs_set_city:"))
async def cb_prefs_set_city(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    data = callback.data or ""
    try:
        idx = int(data.split(":")[-1])
        label, iana_name = POPULAR_CITIES[idx]
    except (IndexError, ValueError):
        await callback.answer("❌ Ошибка выбора города.")
        return

    upsert_user_prefs(callback.message.chat.id, timezone=iana_name)
    await callback.answer(f"✅ Часовой пояс: {label}")
    text = render_prefs_main_text(callback.message.chat.id)
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=build_prefs_main_keyboard())
    except Exception:
        pass


@router.callback_query(F.data == "prefs_tz_text_prompt")
async def cb_prefs_tz_prompt(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(UserPrefsState.waiting_for_tz_text)
    text = (
        "✍️ Пришли название часового пояса (например, <code>Europe/Berlin</code>, <code>Asia/Almaty</code>) "
        "или смещение (например, <code>UTC+5</code>, <code>+4</code>, <code>UTC-3</code>):"
    )
    markup = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отмена", callback_data="prefs_tz_cancel")]]
    )
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=markup)
    except Exception:
        await callback.message.answer(text, parse_mode="HTML", reply_markup=markup)
    await callback.answer()


@router.callback_query(F.data == "prefs_tz_cancel")
async def cb_prefs_tz_cancel(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    text = render_prefs_main_text(callback.message.chat.id)
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=build_prefs_main_keyboard())
    except Exception:
        pass
    await callback.answer("Ввод отменён.")


@router.message(UserPrefsState.waiting_for_tz_text)
async def process_tz_text_input(message: types.Message, state: FSMContext):
    raw_text = (message.text or "").strip()
    norm_city = raw_text.lower().strip()
    if norm_city in CITY_TIMEZONE_MAP:
        tz_str = CITY_TIMEZONE_MAP[norm_city]
    else:
        tz_str = raw_text

    tz_obj, canonical = parse_timezone(tz_str)
    if not tz_obj or not canonical:
        await message.answer(
            "❌ Не удалось распознать часовой пояс. Примеры:\n"
            "• <code>Europe/Moscow</code>\n"
            "• <code>Asia/Novosibirsk</code>\n"
            "• <code>UTC+5</code> или <code>+7</code>\n\n"
            "Попробуй ещё раз или нажми /prefs:",
            parse_mode="HTML",
        )
        return

    upsert_user_prefs(message.chat.id, timezone=canonical)
    await state.clear()
    await message.answer(f"✅ Часовой пояс успешно сохранён: <b>{canonical}</b>", parse_mode="HTML")
    text = render_prefs_main_text(message.chat.id)
    await message.answer(text, parse_mode="HTML", reply_markup=build_prefs_main_keyboard())


@router.callback_query(F.data.startswith("prefs_hour_menu:"))
async def cb_prefs_hour_menu(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    data = callback.data or ""
    part = data.split(":")[-1]
    titles = {"morning": "утра (5–11)", "day": "дня (12–16)", "evening": "вечера (17–23)"}
    title = titles.get(part, part)
    hours = day_part_hours(callback.message.chat.id)
    text = f"⏰ <b>Выбери час {title}:</b>\n\nТекущий выбор отмечен точкой."
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=build_prefs_hour_keyboard(part, hours))
    except Exception:
        await callback.message.answer(text, parse_mode="HTML", reply_markup=build_prefs_hour_keyboard(part, hours))
    await callback.answer()


@router.callback_query(F.data.startswith("prefs_set_hour:"))
async def cb_prefs_set_hour(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    parts = (callback.data or "").split(":")
    if len(parts) < 3:
        await callback.answer("❌ Ошибка.")
        return
    part = parts[1]
    try:
        hour = int(parts[2])
    except ValueError:
        await callback.answer("❌ Некорректный час.")
        return

    current = day_part_hours(callback.message.chat.id)
    new_m = hour if part == "morning" else current["morning"]
    new_d = hour if part == "day" else current["day"]
    new_e = hour if part == "evening" else current["evening"]

    valid, err_msg = validate_hours(new_m, new_d, new_e)
    if not valid:
        await callback.answer(f"❌ {err_msg}", show_alert=True)
        return

    if part == "morning":
        upsert_user_prefs(callback.message.chat.id, morning_hour=hour)
    elif part == "day":
        upsert_user_prefs(callback.message.chat.id, day_hour=hour)
    elif part == "evening":
        upsert_user_prefs(callback.message.chat.id, evening_hour=hour)

    part_names = {"morning": "Утро", "day": "День", "evening": "Вечер"}
    await callback.answer(f"✅ {part_names.get(part, part)}: {hour:02d}:00")

    text = render_prefs_main_text(callback.message.chat.id)
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=build_prefs_main_keyboard())
    except Exception:
        pass
