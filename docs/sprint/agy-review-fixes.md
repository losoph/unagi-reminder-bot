Ревью наблюдателя по T14 (55333c8) и T18 (06c3675). Сделай исправления ОДНИМ коммитом
`fix: ревью T14/T18 — импорты, роутинг, экранирование, примечание (T14b)` с тестами и
коротким блоком «T14b» в progress.md, затем продолжай по порядку (T15 → T16 → T17).

0. КРИТИЧНО (T18) — `reminders_ux.py` делает `from main import ...` внутри функций
   (display_db_datetime, bot, render_rem_detail_screen — 6 мест). На проде бот запускается
   как `python main.py`, модуль называется `__main__`, поэтому `import main` загружает ВТОРУЮ
   копию main.py, она снова делает `dp.include_router(reminders_ux.router)` и падает:
   `RuntimeError: Router is already attached` (проверено симуляцией). Тесты это не ловят,
   потому что импортируют `main` как обычный модуль.
   Исправление: в `reminders_ux.py` не импортировать `main` вообще. Чистые помощники времени
   (`display_db_datetime`, TZ) вынести в отдельный модуль без побочных эффектов (например
   `time_utils.py`) и импортировать оттуда в обоих местах; `bot` и `render_rem_detail_screen`
   передавать через `reminders_ux.configure(bot=..., render_detail=...)`, который main вызывает
   один раз после их определения (или брать `bot` из `callback.bot`/`message.bot`).
   Тест-страж: в исходнике reminders_ux.py нет `from main import` / `import main`; плюс
   subprocess-тест `python -c "import runpy, sys; sys.argv=['main.py']; ..."` не обязателен.
Правила брифа в силе: без push, без --amend, без git add -a/-A.

1. КРИТИЧНО — ввод примечания никогда не срабатывает. В aiogram 3 сначала проверяются
   обработчики самого Dispatcher, и только потом подключённые роутеры
   (`Router._propagate_event`). `catch_message` висит на `dp` без фильтров и делает
   `state.clear()`, поэтому `ReminderNoteState.waiting_for_note` в `reminders_ux.router`
   недостижим. Исправление: вынести `catch_message` в отдельный `fallback_router`
   и подключать его ПОСЛЕДНИМ (`dp.include_router(fallback_router)` после всех роутеров).
   Это же касается любых будущих message-обработчиков в `reminders_ux` (T15–T17), и того же
   правила: новые модули не импортируют `main`.
   Тест: прогнать сообщение через `dp.feed_update` (или `dp.propagate_event`) с FSM в
   состоянии `waiting_for_note` и замоканным ботом — должен вызваться обработчик примечания,
   а не `catch_message`.
2. `check_messages`: текст без `message_id` уходит с `parse_mode="HTML"` без экранирования —
   полный текст с `<`/`&` уронит отправку. Экранировать `content_text` (`html.escape`).
3. Кнопка «📝 Примечание» есть только после переноса доставленного напоминания, а по заданию
   она нужна в подтверждении «Отложил на …» и при первичном создании (быстрые кнопки и ручная
   дата). Для этого `add_message` уже возвращает id. Подтверждение с этой кнопкой не удалять
   через 3 секунды.
