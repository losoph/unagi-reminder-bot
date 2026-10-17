# Карта кластера «Закладки» в main.py (MS1)

Документ фиксирует границы, зависимости, порядок регистрации и критерии выноса кластера «закладки» (База знаний) перед рефакторингом монолита `main.py` в отдельный модуль с aiogram `Router`.

---

## 1. Состав кластера закладок

### 1.1 FSM-состояния (StatesGroup)
| Состояние | Исходные строки в `main.py` | Описание | Зависимости | Вынос |
|---|---|---|---|---|
| `SaveState.waiting_for_tag` | 310–311 | Ожидание ввода тега при сохранении сообщения в закладки | `aiogram.fsm.state.State` | Да, в модуль закладок |
| `SaveState.waiting_for_new_tag` | 312 | Ожидание ввода нового тега при редактировании категории закладки | `aiogram.fsm.state.State` | Да, в модуль закладок |
| `SaveState.waiting_for_reminder_tag` | 313 | Исторически объявлен в `SaveState`, но используется при смене тега **напоминания** (`render_rem_move_screen` / `process_reminder_tag`) | `aiogram.fsm.state.State` | Реэкспорт из `bookmarks` в `main.py` для сохранения обратной совместимости |

> [!NOTE]
> В aiogram 3 строковое представление состояния формируется как `f"{GroupClass.__name__}:{StateName}"`. Поэтому перенос класса `SaveState` в новый модуль сохраняет идентичность состояния (`SaveState:waiting_for_tag`, `SaveState:waiting_for_new_tag`, `SaveState:waiting_for_reminder_tag`) в хранилище FSM.

---

### 1.2 Константы и лимиты
| Константа | Значение / Источник | Использование |
|---|---|---|
| `MAX_USER_SAVED_MESSAGES` | 2000 (`data.database`) | Лимит числа закладок на пользователя (проверяется в `add_saved_message`) |
| `items_per_page` | 5 (строка 3776) | Количество закладок на странице в `render_kb_list_screen` |
| `max_len` | 3000 (строка 3860) | Лимит отображаемого текста в карточке закладки `render_kb_detail_screen` |
| `emoji_nums` | `["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"]` (строки 3794, 3808) | Цифровые эмодзи для кнопок выбора закладок на странице |

---

### 1.3 Хелперы рендеринга экранов
| Функция | Строки в `main.py` | Назначение | Зависимости от БД / утилит | Зависимости от `main.py` | Вынос |
|---|---|---|---|---|---|
| `render_kb_tags_screen` | 3707–3753 | Главный экран Базы знаний (список категорий с числом закладок) | `get_saved_messages` (`data.database`) | Нет | Да |
| `render_kb_list_screen` | 3755–3836 | Список закладок категории с пагинацией по 5 шт. и кнопками выбора | `get_saved_messages` (`data.database`), `html.escape` | Нет | Да |
| `render_kb_detail_screen` | 3838–3892 | Детальная карточка закладки с кнопками управления | `get_saved_message_by_id` (`data.database`), `display_db_datetime` (`time_utils`), `html.escape` | Нет | Да |
| `render_kb_move_screen` | 3894–3946 | Экран выбора/ввода новой категории для закладки | `get_saved_message_by_id`, `get_user_tags` (`data.database`), `SaveState.waiting_for_new_tag`, `html.escape` | Нет | Да |
| `start_save_flow` | 1671–1702 | Запуск сценария сохранения (запрос тега с подсказкой прошлых тегов) | `get_user_tags` (`data.database`), `SaveState.waiting_for_tag` | `user_id_from_message` (чистый хелпер) | Да |

---

### 1.4 Обработчики сообщений (Message Handlers)
| Хендлер | Строки в `main.py` | Фильтр / Триггер | Описание | Зависимости от `bot` / `main.py` | Решение по выносу |
|---|---|---|---|---|---|
| `cmd_saved` | 3948–3953 | `@dp.message(Command("saved"))` | Открытие Базы знаний по команде `/saved` | `push_screen`, `render_kb_tags_screen`, `user_id_from_message` | **Выносится в роутер закладок** |
| `process_new_tag` | 4050–4083 | `@dp.message(SaveState.waiting_for_new_tag)` | Обработка ввода нового тега для существующей закладки | `update_saved_message_tag`, `pop_screen`, `render_kb_detail_screen`, `message.bot.delete_message` | **Выносится в роутер закладок** |
| `process_tag` | 4445–4495 | `@dp.message(SaveState.waiting_for_tag)` | Обработка ввода тега при сохранении новой закладки в БД | `add_saved_message`, `delete_message`, `message.bot.delete_message` | **Выносится в роутер закладок** |
| `cmd_save_reply` | 1800–1821 | `@dp.message(Command("save", "s"))` | Ответ `/save` или `/s` на доставленное напоминание | `get_replied_sent_schedule`, `get_message_full_text`, `start_save_flow` | **Выносится в роутер закладок** (через хелперы без импорта `main`) |

---

### 1.5 Обработчики callback-запросов (Callback Query Handlers)
| Хендлер | Строки в `main.py` | Фильтр / Префикс | Описание | Зависимости | Решение по выносу |
|---|---|---|---|---|---|
| `callback_menu_bookmarks` | 1659–1664 | `F.data == "menu_kb"` | Кнопка «📁 База знаний» в главном меню | `push_screen`, `render_kb_tags_screen` | **Выносится в роутер закладок** |
| `handle_kb_tag_selection` | 3955–3966 | `F.data.startswith("kb_tag_idx_")` | Выбор категории из списка тегов | `push_screen`, `render_kb_list_screen` | **Выносится в роутер закладок** |
| `handle_kb_tag_all` | 3968–3972 | `F.data == "kb_tag_all"` | Показать все закладки без фильтра | `push_screen`, `render_kb_list_screen` | **Выносится в роутер закладок** |
| `handle_kb_view_tags` | 3974–3978 | `F.data == "kb_view_tags"` | Возврат к списку категорий | `push_screen`, `render_kb_tags_screen` | **Выносится в роутер закладок** |
| `handle_kb_page_prev` | 3980–3991 | `F.data == "kb_page_prev"` | Пагинация назад | `render_kb_list_screen` | **Выносится в роутер закладок** |
| `handle_kb_page_next` | 3993–4004 | `F.data == "kb_page_next"` | Пагинация вперёд | `render_kb_list_screen` | **Выносится в роутер закладок** |
| `handle_kb_sel_idx` | 4006–4017 | `F.data.startswith("kb_sel_idx_")` | Выбор закладки на странице (1–5) | `push_screen`, `render_kb_detail_screen` | **Выносится в роутер закладок** |
| `handle_kb_ignore` | 4019–4022 | `F.data == "kb_ignore"` | Клик по разделителю пагинатора | Нет | **Выносится в роутер закладок** |
| `handle_bkmv_init` | 4024–4029 | `F.data.startswith("bkmv_init_")` | Кнопка «🏷 Изменить тег» в карточке | `push_screen`, `render_kb_move_screen` | **Выносится в роутер закладок** |
| `handle_kb_mv_idx` | 4031–4048 | `F.data.startswith("kb_mv_idx_")` | Выбор существующей категории | `update_saved_message_tag`, `pop_screen`, `render_kb_detail_screen` | **Выносится в роутер закладок** |
| `handle_bkdel` | 4085–4099 | `F.data.startswith("bkdel_")` | Кнопка «🗑 Удалить закладку» | `delete_saved_message`, `pop_screen`, `render_screen` / `render_kb_tags_screen` | **Выносится в роутер закладок** (навигация назад через стек экранов) |
| `setup_bookmark` | 4924–4945 | `F.data == "bookmark_setup"` | Кнопка «📁 В закладки» под сообщением | `get_callback_message`, `get_message_full_text`, `get_message_preview`, `start_save_flow` | **Выносится в роутер закладок** |
| `save_sent_reminder` | 1959–1983 | `F.data == "sent_save"` | Кнопка сохранения доставленного напоминания | `get_scheduled_message_by_delivered_message_id`, `start_save_flow` | **Выносится в роутер закладок** |

---

### 1.6 Обработчики на стыке с напоминаниями (не выносимые целиком)
| Обработчик | Строки в `main.py` | Фильтр | Причина, почему остаётся в `main.py` |
|---|---|---|---|
| `handle_rem_to_bookmark` | 2442–2470 | `F.data.startswith("rem_tobk_")` | Кнопка «📁 В закладки» находится на карточке напоминания (`rem_detail`). При переносе она удаляет напоминание и вызывает `render_rem_list_screen`, глубоко связанный с напоминаниями в `main.py`. |
| `handle_bksched_init` | 4101–4131 | `F.data.startswith("bksched_init_")` | Кнопка «⏰ В напоминание» в карточке закладки. Запускает сценарий выбора времени для создания напоминания (`build_time_selection_keyboard`, `describe_quick_times`, `user_now`, `day_part_hours`). |
| `handle_bksched_choice` | 4133–4207 | `F.data.startswith("bksched_choice_")` | Создаёт напоминание через `add_message`, удаляет закладку, отправляет подтверждение с кнопкой примечания `reminders_ux.build_reschedule_note_keyboard`, вызывает `render_screen("rem_list")`. При выборе «Дата…» (`action == "custom"`) переводит FSM в `manual_date` для общего планировщика напоминаний. Покрыт точечным тестом `tests/test_review_t15b.py:120` с моками `main.render_screen` и `main.bot.send_message`. |
| `complete_manual_schedule` | 4209–4443 | Вызывается из пикера минут | Общий планировщик создания напоминаний. Ветка `if is_bksched:` удаляет закладку после создания напоминания. Это базовый механизм напоминаний, не относящийся к модулю закладок. |
| `process_reminder_tag` | 2416–2440 | `@dp.message(SaveState.waiting_for_reminder_tag)` | Относится к экрану `rem_move` (редактирование тега напоминания). |

---

## 2. Зависимости от модульных глобалов `main.py`

| Глобал в `main.py` | Нужен ли закладкам? | Альтернатива в новом модуле |
|---|---|---|
| `bot` (`Bot`) | Да (для `delete_message`) | Доступ через `message.bot` и `callback.bot` во всех aiogram-обработчиках; глобальный синглтон бота **не нужен**. |
| `BOT_USERNAME` | Нет | В кластере закладок не используется. |
| `channel_source` | Нет | В кластере закладок не используется. |
| `USER_FACING_ERROR` | Косвенно | Локальная константа `"❌ Произошла ошибка. Попробуйте позже."` |
| Хелперы FSM-экранов (`push_screen`, `pop_screen`) | Да | Могут быть автономно реализованы в `bookmarks` или переиспользованы (работают исключительно со словарём `state.get_data()["screen_history"]`). |
| `render_screen` | Для возврата после `bkdel_` | Навигация по стеку экранов `history[-1]` переходит на экраны `kb_list` или `kb_tags` (внутри закладок), поэтому `handle_bkdel` автономно восстанавливает контекст закладок. |

---

## 3. Порядок регистрации обработчиков и приоритет в aiogram

В aiogram 3 порядок разрешения событий следующий:
1. Обработчики, зарегистрированные непосредственно на корневом объекте `dp` (`dp.message.handlers`, `dp.callback_query.handlers`).
2. Вложенные роутеры `dp.sub_routers` в порядке вызова `dp.include_router(...)`.
3. Для каждого роутера — его собственные обработчики, затем его вложенные под-роутеры.
4. В самом конце — `fallback_router` с перехватчиком `catch_message`.

### До распила:
- Хендлеры `dp.message.handlers` (28 шт.):
  - `cmd_save_reply` (индекс 7)
  - `cmd_saved` (индекс 18)
  - `process_new_tag` (индекс 19)
  - `process_tag` (индекс 23)
- Под-роутеры:
  - 0: `reminders_ux`
  - 1: `user_prefs`
  - 2: `feed`
  - 3: `fallback` (`catch_message`)
- Хендлеры `dp.callback_query.handlers` (84 шт.):
  - `callback_menu_bookmarks` (индекс 5)
  - `save_sent_reminder` (индекс 8)
  - `handle_kb_tag_selection` … `handle_kb_mv_idx` (индексы 61–69)
  - `handle_bkdel` (индекс 70)
  - `handle_bksched_init`, `handle_bksched_choice` (индексы 71–72)
  - `setup_bookmark` (индекс 79)

### Анализ коллизий:
Все фильтры сообщений кластера закладок имеют строгие условия:
- команды: `Command("saved")`, `Command("save", "s")`;
- FSM-состояния: `SaveState.waiting_for_tag`, `SaveState.waiting_for_new_tag`.

Все callback-фильтры кластера имеют строго уникальные префиксы:
`menu_kb`, `sent_save`, `kb_tag_idx_`, `kb_tag_all`, `kb_view_tags`, `kb_page_prev`, `kb_page_next`, `kb_sel_idx_`, `kb_ignore`, `bkmv_init_`, `kb_mv_idx_`, `bkdel_`, `bookmark_setup`.

Ни один из этих фильтров не перекрывается с командами или колбэками других модулей.
Позиция `dp.include_router(bookmarks_router)`:
Подключается сразу после feature-роутеров (`reminders_ux`, `user_prefs`, `feed`) и **строго до** `fallback_router`.
Это гарантирует, что `catch_message` не перехватит ввод пользователя в состояниях `SaveState`.

---

## 4. План реализации (Шаг 1)

1. Создать модуль `bookmarks.py`:
   - `router = Router(name="bookmarks")`
   - `SaveState(StatesGroup)`
   - Чистые хелперы: `user_id_from_message`, `user_id_from_callback`, `get_callback_message`, `get_message_full_text`, `get_message_preview`, `build_back_home_keyboard`, `push_screen`, `pop_screen`
   - Экраны: `render_kb_tags_screen`, `render_kb_list_screen`, `render_kb_detail_screen`, `render_kb_move_screen`, `start_save_flow`
   - Обработчики сообщений и callback'ов, зарегистрированные на `router`
   - Отсутствие `import main`
   - Доступ к Bot через `message.bot` / `callback.bot`
2. В `main.py`:
   - Импортировать `import bookmarks`
   - Подключить `dp.include_router(bookmarks.router)` в блоке подключения роутеров (строки 154–157), до `fallback_router`
   - Реэкспортировать все перемещённые функции и классы:
     `SaveState`, `render_kb_tags_screen`, `render_kb_list_screen`, `render_kb_detail_screen`, `render_kb_move_screen`, `start_save_flow`, `cmd_saved`, `cmd_save_reply`, `process_tag`, `process_new_tag`, `callback_menu_bookmarks`, `handle_kb_*`, `handle_bkmv_init`, `handle_bkdel`, `setup_bookmark`, `save_sent_reminder`
   - В `render_screen(main.py)` вызывать `bookmarks.render_kb_*`
3. Написать тест `tests/test_router_order.py`, верифицирующий:
   - Отсутствие импорта `main` внутри `bookmarks.py`
   - Наличие роутера `bookmarks` в `dp.sub_routers` до `fallback_router`
   - Совпадение сигнатур и доступность символов через `main.<name>`
   - Корректность диспетчеризации FSM-состояний и команд
