"""Welcome text, full /help reference, main and settings menus, Telegram command list.

Texts and keyboards only: handlers stay in main.py, because the menu buttons open
screens that live there.
"""
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

WELCOME_TEXT = (
    "👋 Я Unagi: откладываю сообщения, храню закладки и собираю дайджесты Telegram-каналов.\n\n"
    "• Перешли сообщение или напиши текст — предложу отложить его или сохранить в закладки.\n"
    "• Пришли пост или ссылку на открытый канал (t.me/канал, @канал) — подпишу на дайджест.\n"
    "• Доставленное напоминание переносится кнопкой или ответом: «завтра 11», «через 2 дня».\n\n"
    "Полная справка по всем командам — /help."
)

HELP_TEXT = (
    "ℹ️ <b>Справка</b>\n\n"
    "<b>Как начать</b>\n"
    "• Перешли сообщение или напиши текст — бот предложит отложить его (напоминание) "
    "или сохранить в закладки.\n"
    "• Перешли пост из открытого канала или пришли ссылку на него (t.me/канал, t.me/s/канал, "
    "@канал) — бот подпишет на дайджест.\n\n"
    "📅 <b>Напоминания</b>\n"
    "/list — все напоминания по дням; в карточке: перенос, примечание, тег, переход к оригиналу\n"
    "На доставленном напоминании: <b>+3 ч</b>, <b>Вечером</b>, <b>Завтра утром</b>, "
    "⏰ Другое время, ✅ Готово, 📁 Сохранить, 🗑 Удалить.\n"
    "Перенос текстом — ответом на напоминание: <i>завтра 11</i>, <i>24/09 в 9</i>, "
    "<i>через 2 дня</i>, <i>в пятницу в 10</i>. В первые 10 минут после доставки можно "
    "и без ответа — просто напиши время.\n"
    "Команды ответом на напоминание:\n"
    "• /morning, /day, /evening — на ближайшее утро, день или вечер (по твоим часам)\n"
    "• /later (/l) — на 3 часа\n"
    "• <code>/at 24.09 18:30</code> — на точное время\n"
    "• /save (/s) — в закладки · /delete (/d) — удалить\n\n"
    "📁 <b>Закладки</b>\n"
    "/saved — база знаний по папкам: перенос между папками, превращение в напоминание, удаление\n\n"
    "📡 <b>Дайджесты каналов</b>\n"
    "/list_digest — подписки и расписание: время ежедневного, еженедельного и ежемесячного "
    "выпуска, папки, пауза, перенос между выпусками, отписка, ИИ-анализ каналов, тех. алерты\n"
    "/test_digest — собрать выпуск за последние 24 часа прямо сейчас\n"
    "/check — проверить, что все каналы читаются\n"
    "/email — получать выбранные выпуски на почту вместо Telegram\n"
    "• В день, когда совпадают ежедневный и еженедельный (или ежемесячный) выпуски, приходит "
    "<b>один общий выпуск</b> — во время старшего; посты за сутки идут первой группой.\n"
    "• Выпуск от 4 постов приходит одной ссылкой на Telegraph.\n"
    "• В выпуске сообщениями и в письме под каждым каналом есть «Отписаться» и «Перенести» "
    "в другой выпуск.\n\n"
    "⚙️ <b>Настройки и данные</b>\n"
    "/settings — все настройки в одном меню\n"
    "/prefs (/timezone) — часовой пояс и часы утра, дня и вечера\n"
    "/export — выгрузить напоминания, закладки и подписки в JSON\n"
    "/import — восстановить данные из JSON\n\n"
    "🧭 <b>Навигация</b>\n"
    "/start — главное меню · /help — эта справка\n"
    "/cancel (/home) — сбросить текущее действие · /back — шаг назад"
)

SETTINGS_TEXT = (
    "⚙️ <b>Настройки</b>\n\n"
    "🕒 <b>Время и пояс</b> — часовой пояс и часы утра, дня и вечера для напоминаний.\n"
    "📡 <b>Дайджесты</b> — расписание выпусков, подписки, папки, тех. алерты.\n"
    "📧 <b>Почта</b> — какие выпуски получать письмом вместо Telegram.\n"
    "💾 <b>Данные</b> — экспорт и импорт в JSON."
)

# (command, description) — the menu Telegram shows next to the input field.
BOT_COMMANDS: list[tuple[str, str]] = [
    ("start", "🏠 Главное меню"),
    ("help", "ℹ️ Полная справка"),
    ("list", "📅 Мои напоминания"),
    ("saved", "📁 Закладки"),
    ("list_digest", "📡 Подписки и расписание дайджестов"),
    ("test_digest", "⏳ Выпуск за 24 ч прямо сейчас"),
    ("check", "🔎 Проверить каналы"),
    ("settings", "⚙️ Все настройки"),
    ("prefs", "🕒 Время и пояс"),
    ("email", "📧 Дайджест на почту"),
    ("export", "📤 Экспорт данных (JSON)"),
    ("import", "📥 Импорт данных (JSON)"),
    ("cancel", "✖️ Отменить текущее действие"),
    ("back", "🔙 Шаг назад"),
]


def build_main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📅 Напоминания", callback_data="menu_rem"),
                InlineKeyboardButton(text="📁 Закладки", callback_data="menu_kb"),
            ],
            [
                InlineKeyboardButton(text="📡 Дайджесты", callback_data="digest_settings"),
                InlineKeyboardButton(text="⚙️ Настройки", callback_data="settings_menu"),
            ],
            [InlineKeyboardButton(text="ℹ️ Полная справка", callback_data="help_full")],
        ]
    )


def build_settings_keyboard(email_enabled: bool) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="🕒 Время и пояс", callback_data="prefs_menu")],
        [InlineKeyboardButton(text="📡 Дайджесты", callback_data="digest_settings")],
    ]
    if email_enabled:
        rows.append([InlineKeyboardButton(text="📧 Почта", callback_data="email_menu")])
    rows.append(
        [
            InlineKeyboardButton(text="📤 Экспорт", callback_data="data_export"),
            InlineKeyboardButton(text="📥 Импорт", callback_data="data_import"),
        ]
    )
    rows.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="flow_home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_help_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="⚙️ Настройки", callback_data="settings_menu"),
                InlineKeyboardButton(text="🏠 Главное меню", callback_data="flow_home"),
            ]
        ]
    )
