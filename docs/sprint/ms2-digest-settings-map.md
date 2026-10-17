# Карта кластера «Настройки и расписание дайджеста» в main.py (MS2)

Документ фиксирует границы, зависимости, порядок регистрации и критерии выноса кластера «настройки и расписание дайджеста» (экраны и колбэки) перед рефакторингом монолита `main.py` в отдельный модуль `digest_settings.py` с aiogram `Router`.
Это «ворота G2» перед UI очереди 4 (T23, `docs/sprint/queue4-design.md` §7).

---

## 1. Состав кластера настроек и расписания дайджеста

### 1.1 FSM-состояния (StatesGroup)
| Состояние | Исходные строки в `main.py` | Описание | Зависимости | Решение по выносу |
|---|---|---|---|---|
| `SubTagState.waiting_for_folder` | 335–336 | Ожидание ввода названия новой папки при привязке канала к папке | `aiogram.fsm.state.State`, `StatesGroup` | **Выносится в `digest_settings.py`**, реэкспорт в `main` |

> [!NOTE]
> В aiogram 3 строковое представление состояния формируется как `f"{GroupClass.__name__}:{StateName}"` (`SubTagState:waiting_for_folder`). Перенос класса сохраняет совместимость с активными FSM-сессиями в Redis/MemoryStorage.

---

### 1.2 Константы и словари локализации
| Константа | Исходные строки | Описание | Вынос |
|---|---|---|---|
| `_PERIOD_RU` | 321 | Словарь локализации периодов в винительном/предложном падеже | Да |
| `_PERIOD_TITLES` | 322 | Заголовки периодов с заглавной буквы («Ежедневный» и т.д.) | Да |
| `_PERIOD_PLURAL` | 323 | Множественное число периодов («ежедневные» и т.д.) | Да |
| `_WEEKDAY_NOM_RU` | 324 | Дни недели в именительном падеже (0 — «понедельник») | Да |
| `_WEEKDAY_ACC_RU` | 325 | Дни недели в винительном падеже (0 — «понедельник», 2 — «среду») | Да |
| `_WEEKDAY_SHORT_RU` | 326 | Короткие названия дней недели (0 — «Пн», …, 6 — «Вс») | Да |

---

### 1.3 Хелперы расписания и настроек
| Функция | Исходные строки | Назначение | Зависимости | Решение по выносу |
|---|---|---|---|---|
| `get_default_digest_settings` | 367–376 | Формирует дефолтные настройки дайджеста (7:00, текущий день недели/месяца) | `time_utils.local_now` | Да |
| `resolve_digest_settings` | 378–406 | Считывает настройки из БД или подставляет дефолтные с валидацией границ | `data.database.get_digest_settings`, `get_default_digest_settings` | Да |
| `_build_digest_candidate` | 409–410 | Создаёт `datetime` с заданными часом и минутой | чистая функция | Да |
| `_nth_weekday_of_month` | 413–423 | Вычисляет дату n-го вхождения дня недели в месяце | `calendar`, `time_utils.TZ` | Да |
| `get_next_digest_time` | 426–471 | Вычисляет следующее время отправки дайджеста для daily/weekly/monthly | `calendar`, `time_utils.local_now`, `time_utils.TZ` | Да |
| `format_digest_schedule` | 473–486 | Форматирует читаемую строку расписания для UI («каждый день в 07:00») | `_WEEKDAY_ACC_RU`, `_WEEKDAY_NOM_RU` | Да |
| `reschedule_user_period` | 795–800 | Пересчитывает `next_send_at` для подписок пользователя при смене расписания | `resolve_digest_settings`, `get_next_digest_time`, `data.database.update_subscriptions_next_send_at` | Да |

---

### 1.4 Хелперы рендеринга экранов и клавиатур
| Функция | Исходные строки | Назначение | Зависимости | Решение по выносу |
|---|---|---|---|---|
| `build_digest_channel_actions` | 495–524 | Клавиатура карточки канала (время, пауза, папка, в ленту, перенос, отписка) | `InlineKeyboardMarkup` | Да |
| `_subscription_status_marker` | 526–532 | Эмодзи-маркер статуса канала (⏸, ⚠️, 📰) | чистая функция | Да |
| `build_post_subscribe_keyboard` | 534–550 | Клавиатура быстрых действий сразу после подписки на канал | `InlineKeyboardMarkup` | Да |
| `_sort_subscriptions_for_display` | 553–560 | Сортировка подписок для экрана (по папкам, затем по имени) | чистая функция | Да |
| `build_digest_settings_keyboard` | 563–614 | Главная клавиатура настроек (расписания, каналы, тех. алерты, экспорт/импорт) | `data.database`, `email_digest` | Да |
| `render_digest_settings_text` | 616–676 | Текст главного экрана настроек дайджеста со списком каналов по папкам | `data.database`, `html.escape` | Да |
| `build_digest_schedule_keyboard` | 678–691 | Клавиатура выбора параметров расписания (время, день недели, дата, режим) | `InlineKeyboardMarkup` | Да |
| `render_digest_schedule_text` | 693–705 | Текст экрана расписания периода с описанием текущих параметров | `resolve_digest_settings`, `format_digest_schedule` | Да |
| `build_digest_time_hours_keyboard` | 707–718 | Сетка часов (00..23) для выбора времени | `InlineKeyboardMarkup` | Да |
| `build_digest_time_minutes_keyboard` | 720–732 | Выбор минут (00, 15, 30, 45) | `InlineKeyboardMarkup` | Да |
| `build_digest_weekday_keyboard` | 734–750 | Выбор дня недели для еженедельного дайджеста | `InlineKeyboardMarkup` | Да |
| `build_digest_monthday_keyboard` | 752–763 | Сетка чисел месяца (1..31) для ежемесячного дайджеста | `InlineKeyboardMarkup` | Да |
| `build_digest_monthly_mode_keyboard` | 765–777 | Переключатель режима месяца («По дате» / «По дню недели») | `InlineKeyboardMarkup` | Да |
| `edit_or_answer` | 779–793 | Безопасное редактирование сообщения или fallback на новое сообщение | `TelegramBadRequest` | Да |
| `_refresh_digest_settings` | 2492–2499 | Перерисовка главного экрана настроек | `render_digest_settings_text`, `build_digest_settings_keyboard` | Да |
| `render_subscription_actions` | 2501–2524 | Рендеринг детальной карточки подписки канала | `data.database.get_subscription_by_id`, `build_digest_channel_actions` | Да |
| `build_subscription_folder_keyboard` | 2790–2805 | Клавиатура выбора/создания папки канала | `InlineKeyboardMarkup` | Да |

---

### 1.5 Обработчики callback-запросов (23 шт.)
| Хендлер | Исходные строки | Фильтр / Префикс | Назначение | Решение по выносу |
|---|---|---|---|---|
| `open_digest_settings` | 2650–2655 | `F.data.in_({"digest_settings", "ds"})` | Открытие экрана настроек дайджеста | **Выносится в роутер** |
| `open_digest_subscription_actions` | 2657–2665 | `F.data.startswith("dsub_")` | Открытие карточки подписки канала | **Выносится в роутер** |
| `move_digest_subscription` | 2667–2709 | `F.data.startswith("dmove_")` | Перенос канала в другой период дайджеста | **Выносится в роутер** |
| `pause_subscription` | 2716–2725 | `F.data.startswith("dpause_")` | Поставить подписку на паузу | **Выносится в роутер** |
| `resume_subscription` | 2727–2743 | `F.data.startswith("dresume_")` | Возобновить подписку с пересчётом времени | **Выносится в роутер** |
| `pause_all_subscriptions` | 2745–2750 | `F.data == "dpauseall"` | Массовая пауза всех подписок | **Выносится в роутер** |
| `resume_all_subscriptions` | 2752–2761 | `F.data == "dresumeall"` | Массовое возобновление всех подписок | **Выносится в роутер** |
| `confirm_unsubscribe_all` | 2763–2781 | `F.data == "dunsuball"` | Запрос подтверждения массовой отписки | **Выносится в роутер** |
| `unsubscribe_all_confirmed` | 2783–2788 | `F.data == "dunsuball_yes"` | Подтверждение удаления всех подписок | **Выносится в роутер** |
| `open_subscription_folder_menu` | 2807–2829 | `F.data.startswith("dtag_")` | Меню папок для канала | **Выносится в роутер** |
| `set_subscription_folder` | 2831–2852 | `F.data.startswith("dtagset_")` | Привязка канала к выбранной папке | **Выносится в роутер** |
| `clear_subscription_folder` | 2854–2863 | `F.data.startswith("dtagclear_")` | Снятие папки с канала | **Выносится в роутер** |
| `prompt_new_subscription_folder` | 2865–2881 | `F.data.startswith("dtagnew_")` | Запрос ввода названия новой папки в FSM | **Выносится в роутер** |
| `open_digest_schedule` | 2911–2923 | `F.data.startswith("dsch_")` | Экран настройки расписания периода | **Выносится в роутер** |
| `open_digest_time_picker` | 2925–2937 | `F.data.startswith("dtime_")` | Пикер часов отправки | **Выносится в роутер** |
| `open_digest_minute_picker` | 2939–2957 | `F.data.startswith("dth_")` | Пикер минут отправки | **Выносится в роутер** |
| `save_digest_time` | 2959–2984 | `F.data.startswith("dtm_")` | Сохранение времени и пересчёт расписания | **Выносится в роутер** |
| `open_digest_weekday_picker` | 2986–2998 | `F.data.startswith("dweekday_")` | Пикер дня недели | **Выносится в роутер** |
| `save_digest_weekday` | 3000–3020 | `F.data.startswith("dwd_")` | Сохранение дня недели и пересчёт | **Выносится в роутер** |
| `open_digest_monthday_picker` | 3022–3030 | `F.data == "dmday_monthly"` | Пикер числа месяца | **Выносится в роутер** |
| `save_digest_monthday` | 3032–3046 | `F.data.startswith("dmd_monthly_")` | Сохранение числа месяца и пересчёт | **Выносится в роутер** |
| `open_digest_monthly_mode_picker` | 3048–3057 | `F.data == "dmode_monthly"` | Пикер режима месяца (дата/день недели) | **Выносится в роутер** |
| `save_digest_monthly_mode` | 3059–3073 | `F.data.startswith("dmm_monthly_")` | Сохранение режима месяца и пересчёт | **Выносится в роутер** |

---

### 1.6 Обработчики сообщений (1 шт.)
| Хендлер | Исходные строки | Фильтр / Состояние | Назначение | Решение по выносу |
|---|---|---|---|---|
| `process_new_subscription_folder` | 2883–2909 | `@dp.message(SubTagState.waiting_for_folder)` | Текстовый ввод названия новой папки | **Выносится в роутер** |

---

## 2. Что НЕ выносится и почему остаётся в `main.py`

| Компонент / Хендлер | Исходные строки | Причина сохранения в `main.py` |
|---|---|---|
| `cmd_list_digest` | 2240–2249 | Корневая команда бота `/list_digest`. Явно исключена в брифе задачи ("НЕ ВЫНОСИТЬ: ... /list_digest"). Точка входа в настройки из командного интерфейса. |
| `handle_unsub` | 2462–2490 | Колбэк `unsub_{id}` исторически расположен до строки 2490 перед кластером настроек; покрыт точечными тестами безопасности `tests/test_security_sec1.py`. |
| `handle_alert_*` (`handle_alert_retry`, `handle_alert_pause_24h`, `handle_alert_pause_7d`, `handle_alert_disable`, `handle_alert_toggle_enable`) | 2526–2649 | Блок управления техническими алертами сбоев сборщика дайджеста (T9–T11). Завязан на `digest_executions`, фоновые циклы и клавиатуру `build_digest_alert_keyboard` из `digest_alerts.py`. Оставлен в `main.py` для чистоты архитектурных границ (настройки дайджеста vs обработка сбоев пайплайна). |
| Доставка и циклы (`send_digest_chunks`, `deliver_digest`, `send_telegram_digest`, `send_digest_email`, `run_digest_cycle`, `check_digests`) | 1087–1390, 4700–4850 | Фоновые сервисные функции отправки и проверки дайджестов. Явно исключены в брифе задачи. |
| Создание подписки (`setup_digest`, `save_subscription`, `ScheduleState`) | 4850–4920 | Мастер добавления подписки на канал с пошаговым выбором времени. Явно исключён в брифе. |
| ИИ-экраны дайджеста (`open_ai_menu` / `digest_ai`, `open_ai_channel`, `ai_adjust_digest`, `ai_summary`) | 3140–3240 | Экспериментальные функции ИИ-анализа каналов. Явно исключены в брифе. |
| `/test_digest`, `/email`, экспорт/импорт, диплинки | 3250–3700 | Специализированные подсистемы и служебные команды. Явно исключены в брифе. |
| `cmd_check` (`/check`) | 3080–3133 | Команда сетевой проверки каналов через `channel_source.fetch_semaphore`. |

---

## 3. Зависимости от глобалов `main.py` и изоляция

| Глобал в `main.py` | Нужен ли настройкам дайджеста? | Решение в `digest_settings.py` |
|---|---|---|
| `bot` (`Bot`) | Нет | Все обработчики работают через контекстные `callback.bot` и `message.bot`. Синглтон бота не импортируется. |
| `dp` (`Dispatcher`) | Нет | Используется локальный роутер `router = Router(name="digest_settings")`. |
| `channel_source` | Нет | В настройках и расписании не используется. |
| `BOT_TOKEN`, `BOT_USERNAME` | Нет | В настройках не используются. |
| Хелперы работы с типами aiogram (`user_id_from_callback`, `user_id_from_message`, `get_callback_message`, `parse_callback_int_suffix`, `parse_callback_strip_prefix`) | Да | Автономно определены внутри `digest_settings.py` как чистые функции без импорта `main`. |
| `render_screen` | Нет | Все 24 хендлера настроек автономны и вызывают собственные функции рендеринга (`_refresh_digest_settings`, `render_subscription_actions`, `edit_or_answer`). |

> [!IMPORTANT]
> `digest_settings.py` **не содержит** `import main` или `from main import ...`. Двойной импорт `main` исключён.

---

## 4. Порядок регистрации обработчиков и анализ коллизий (tools/handler_order.py)

В `main.py` порядок подключения роутеров:
1. `dp.include_router(reminders_ux.router)`
2. `dp.include_router(user_prefs.router)`
3. `dp.include_router(feed.router)`
4. `dp.include_router(bookmarks.router)`
5. `dp.include_router(digest_settings.router)`  <-- новый роутер
6. ...
7. `dp.include_router(fallback_router)` (строка 5085, в самом конце перед запуском)

### Анализ пересечений фильтров:
- Хендлер сообщений `process_new_subscription_folder` имеет строгий фильтр состояния FSM `SubTagState.waiting_for_folder`. Никаких пересечений с командами или другими состояниями.
- Все 23 колбэка имеют префиксы и литералы, уникальные для дайджеста:
  `digest_settings`, `ds`, `dsub_`, `dmove_`, `dpause_`, `dresume_`, `dpauseall`, `dresumeall`, `dunsuball`, `dunsuball_yes`, `dtag_`, `dtagset_`, `dtagclear_`, `dtagnew_`, `dsch_`, `dtime_`, `dth_`, `dtm_`, `dweekday_`, `dwd_`, `dmday_monthly`, `dmd_monthly_`, `dmode_monthly`, `dmm_monthly_`.
- Колбэк `open_digest_settings` с литералами `['digest_settings', 'ds']` не конфликтует с `setup_digest` (`digest_setup`), так как префикс `ds` не совпадает с началом `di` слова `digest`.
- Подключение роутера до `fallback_router` гарантирует, что `catch_message` не перехватит ввод пользователя в состоянии `SubTagState:waiting_for_folder`.
- Инструмент `tools/handler_order.py compare` подтверждает 0 пар с риском изменения приоритета для всех 150 хендлеров.
