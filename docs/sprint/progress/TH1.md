# TH1 + HP1: Вынос синхронных вызовов SQLite в to_thread и пинг DeepSeek

## TH1. Вынос тяжелых синхронных вызовов SQLite в `asyncio.to_thread`

### Контекст и архитектурное решение
В фоновых циклах доставки (дайджесты, Лента, напоминания, алерты) и aiogram-хендлерах часть обращений к SQLite выполняла блокирующие операции чтения/записи множества строк в event loop. Согласно задаче TH1, такие вызовы обернуты в `await asyncio.to_thread(...)` в местах вызова без изменения сигнатур функций в `data/database.py`. Короткие точечные запросы (одна строка по id) оставлены без изменений.

### Аудит потокобезопасности SQLite соединений (`get_connection`)
- Проверена реализация `get_connection()` в `data/database.py:46-63`:
  ```python
  @contextmanager
  def get_connection():
      if DB_PATH != ":memory:":
          Path(DB_PATH).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
      conn = sqlite3.connect(DB_PATH)
      conn.row_factory = sqlite3.Row
      try:
          conn.execute("PRAGMA busy_timeout = 5000")
          conn.execute("PRAGMA synchronous = NORMAL")
          conn.execute("PRAGMA temp_store = MEMORY")
          conn.execute("PRAGMA cache_size = -16000")
          _secure_db_files_once()
          yield conn
      finally:
          conn.close()
  ```
- **Вывод**: `get_connection()` создаёт отдельное новое соединение `sqlite3.connect` на каждый контекстный вход и гарантированно закрывает его в блоке `finally`. Соединения не кешируются и не шарятся глобально между потоками, что делает вызовы функций БД через рабочий пул `asyncio.to_thread` полностью потокобезопасными (в режиме базы WAL).

### Места вызовов, переведенные на `asyncio.to_thread`
1. **Фоновый цикл дайджестов (`main.py`)**:
   - `run_digest_cycle`: выборка пачки due-подписок `get_due_subscriptions` переведена на `to_thread`.
   - `run_digest_cycle`: планирование выпусков и групповое откладывание `plan_due_editions` (`get_user_subscriptions_due_until`, `defer_subscriptions`) переведено на `to_thread`.
   - `prefetch_due_channels`: массовое сохранение постов каналов `upsert_channel_posts` переведено на `to_thread`.
   - `fetch_subscription_posts`: сохранение постов `upsert_channel_posts` и выборка истории канала `get_channel_posts_since` переведены на `to_thread`.
   - `process_user_edition`: пакетное сохранение постов в базу знаний `add_digest_posts` переведено на `to_thread`.
2. **Фоновый цикл напоминаний (`main.py`)**:
   - `check_messages`: выборка наступивших напоминаний `get_pending_messages` переведена на `to_thread`.
3. **Фоновый цикл технических алертов (`digest_alerts.py`)**:
   - `process_pending_digest_alerts`: выборка упавших выпусков `get_failed_digest_executions_pending_alert` переведена на `to_thread`.
   - `process_pending_recovery_alerts`: выборка восстановившихся выпусков `get_delivered_executions_pending_recovery_alert` переведена на `to_thread`.
4. **Фоновый цикл и хендлеры Ленты (`feed.py`)**:
   - В цикле автодоставки (`process_feed_autodeliveries`, `build_feed`) вызовы `list_all_feed_autodeliveries`, `disable_feed_autodelivery`, `list_feed_subscriptions` и `get_feed_candidate_posts` уже были в `to_thread`.
   - В хендлерах `render_personal_channels`, `cb_feed_channels`, `cb_feed_del` выборка подписок `list_feed_subscriptions` переведена на `to_thread`.
   - В `cb_feed_tag_menu` выборка тегов `list_feed_tags` переведена на `to_thread`.
5. **Хендлеры закладок и базы знаний (`bookmarks.py`)**:
   - `render_kb_tags_screen` и `render_kb_list_screen`: выборка всех сохраненных сообщений `get_saved_messages` переведена на `to_thread`.
   - `render_kb_move_screen` и `start_save_flow`: выборка тегов `get_user_tags` переведена на `to_thread`.
6. **Хендлеры настроек дайджеста (`digest_settings.py`, `main.py`)**:
   - `digest_settings.py`: `_refresh_digest_settings`, `cmd_digest_settings` (`get_user_subscriptions`).
   - `digest_settings.py`: массовая пауза/возобновление `set_all_subscriptions_paused`.
   - `digest_settings.py`: пакетное перепланирование времени `reschedule_user_period` (`update_subscriptions_next_send_at`).
   - `digest_settings.py`: массовая отписка `unsubscribe_all`.
   - `digest_settings.py`: выборка папок пользователя `get_subscription_tags`.
   - `main.py`: `handle_digest_deep_link`, `cb_delete_channel`, `cmd_list`, `cmd_list_digest`, `cmd_check`, `open_ai_menu`, `cmd_test_digest` (`get_user_subscriptions`).
   - `main.py`: `cb_preview_digest` (`get_digest_posts` за 30 дней).
   - `main.py`: `render_rem_tags_screen`, `render_rem_list_screen` (`get_user_messages`), `render_rem_move_screen` (`get_user_tags`).

### Исключения и точечные вызовы (сохранены без изменений)
- Точечные запросы по ключам/ID (`get_subscription_by_id`, `delete_subscription`, `update_subscription_time`, `update_subscription_schedule`, `mark_as_sent`, `mark_message_delivery_error`, `mark_subscription_delivery_error`, `get_app_meta`, `set_app_meta`, `record_digest_execution_start`, `record_digest_execution_finish`) выполняются за доли миллисекунды и оставлены синхронными во избежание лишних накладных расходов на переключение потоков.
- **Кластер `/export` / `/import`**: в соответствии с ТЗ оставлен без изменений (`export_user_data`, `_import_user_payload` в `main.py`), так как параллельно рефакторится треком MS3.

---

## HP1. Проверка ключа DeepSeek при старте и по расписанию

### Реализация
- В `ai_assistant.py`:
  - `check_api_key(session=None) -> bool` (и алиас `ping`): отправляет легкий запрос `GET /models` с таймаутом `REQUEST_TIMEOUT` без расхода токенов.
  - Обработка HTTP 401/403 (`_handle_auth_error`):
    - При первой ошибке пишется ровно одна строка уровня `ERROR`: `ключ DeepSeek недействителен (401)` (или `403`).
    - Трейсбек не выводится (`exc_info=None`).
    - Ключ `DEEPSEEK_API_KEY` в лог нигде не передается и не выводится.
    - Последующие вызовы при сохранении ошибки подавляются; повторное напоминание выводится уровнем `WARNING` не чаще одного раза в час (`AUTH_ERROR_COOLDOWN_SECONDS = 3600`).
    - При успехе (HTTP 200) состояние сбрасывается (`_handle_auth_success`).
  - Логика авторизационных ошибок интегрирована также в `ai_assistant.ask()`: при 401/403 вызывается `_handle_auth_error`, предотвращая дамп тела ответа провайдера в логи.
  - Фоновый цикл `run_key_check_loop(interval_seconds=3600)`: выполняет периодическую проверку ключа раз в час.
- В `main.py`:
  - При запуске бота (`main()`) при включенном DeepSeek выполняется стартовая проверка `await ai_assistant.check_api_key()`.
  - В фоновые задачи `background_tasks` добавляется задача `ai_assistant.run_key_check_loop()` с корректной отменой в `finally:`.

---

## Тестирование
- Создан тестовый модуль `tests/test_th1_hp1.py`:
  - `test_digest_cycle_calls_get_due_subscriptions_via_to_thread`
  - `test_digest_cycle_calls_plan_due_editions_via_to_thread`
  - `test_prefetch_calls_upsert_channel_posts_via_to_thread`
  - `test_fetch_subscription_posts_calls_db_via_to_thread`
  - `test_check_messages_calls_get_pending_messages_via_to_thread`
  - `test_digest_alerts_calls_db_via_to_thread`
  - `test_ping_401_logs_single_error_without_traceback_and_no_key`
  - `test_ping_403_logs_single_error_without_traceback`
  - `test_ping_throttles_subsequent_calls_within_one_hour`
  - `test_ping_logs_warning_after_one_hour`
  - `test_ping_success_resets_auth_error_state`
  - `test_ask_401_throttled_and_no_key_or_traceback`
  - `test_ping_disabled_when_no_api_key`
- Все тесты `tests/test_th1_hp1.py` зелёные (13 из 13).
- Линтер `~/.venvs/unagi/bin/ruff check .` — без замечаний.
