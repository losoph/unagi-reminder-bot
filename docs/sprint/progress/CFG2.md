# CFG2: Перевод main.py и data/database.py на config.py

## Журнал решений

## CFG2 Перевод main.py и data/database.py на config.py
- Решение: Выделил три предметные области настроек: DatabaseConfig (квоты хранения, лимиты бэклога и путь SQLite bot_data.db), BotConfig (токен, юзернейм, прокси, кулдаун дорогих команд, превью и маркер готовности) и DigestConfig (размер пачки due-рассылок, ретраи бэклога, интервал цикла и порог Telegraph), а также дополнил EmailConfig суточными лимитами писем (EMAIL_DAILY_LIMIT_PER_USER/ADDRESS). Модульные константы в main.py и data/database.py сохранены с инициализацией через settings(), чтобы не ломать прямые мутации (db.DB_PATH = ...) и mock.patch в существующих тестах.
- Компромисс: Прямые присваивания в тестах вида `db.DB_PATH = ...` и `db.MAX_DIGEST_SUBSCRIPTIONS = ...` меняют переменные уровня модуля database.py, но не трогают закэшированный Settings. Для полной чистоты в будущем тесты можно перевести на settings(env=...) или reset_settings(), однако для 100% обратной совместимости сохранена работа обоих подходов.
- Для следующей очереди: В кодовой базе остались только ai_assistant.py и digest_alerts.py с разрозненными os.getenv — их перенос завершит полную унификацию конфигурации проекта.

---

## Таблица перенесённых переменных конфигурации

| Переменная | Область / Dataclass | Модуль-источник | Умолчание | Тип | Правило разбора / ограничения |
|---|---|---|---|---|---|
| `DB_PATH` | База данных (`DatabaseConfig`) | `data/database.py` | `bot_data.db` (в корне) | `str` | `env.get("DB_PATH") or str(BASE_DIR / "bot_data.db")` |
| `MESSAGE_RETENTION_DAYS` | База данных (`DatabaseConfig`) | `data/database.py` | `30` | `int` | `int(val or 30)` |
| `SUBSCRIPTION_FAILURE_RETENTION_DAYS` | База данных (`DatabaseConfig`) | `data/database.py` | `90` | `int` | `int(val or 90)` |
| `DIGEST_POST_RETENTION_DAYS` | База данных (`DatabaseConfig`) | `data/database.py` | `30` | `int` | `int(val or 30)` |
| `MAX_ERROR_TEXT_LENGTH` | База данных (`DatabaseConfig`) | `data/database.py` | `800` | `int` | `int(val or 800)` |
| `CHANNEL_BACKLOG_MAX` | База данных (`DatabaseConfig`) | `data/database.py` | `500` | `int` | `int(val or 500)` |
| `CHANNEL_DELIVERY_BATCH_SIZE` | База данных (`DatabaseConfig`) | `data/database.py` | `50` | `int` | `int(val or 50)` |
| `MAX_FEED_CHANNELS` | База данных (`DatabaseConfig`) | `data/database.py` | `100` | `int` | `int(val or 100)` |
| `MAX_DIGEST_SUBSCRIPTIONS` | База данных (`DatabaseConfig`) | `data/database.py` | `300` | `int` | `int(val or 300)` |
| `MAX_USER_REMINDERS` | База данных (`DatabaseConfig`) | `data/database.py` | `1000` | `int` | `int(val or 1000)` |
| `MAX_USER_SAVED_MESSAGES` | База данных (`DatabaseConfig`) | `data/database.py` | `2000` | `int` | `int(val or 2000)` |
| `BOT_TOKEN` | Бот / Core (`BotConfig`) | `main.py` | `None` | `str \| None` | сырая строка или `None` |
| `BOT_USERNAME` | Бот / Core (`BotConfig`) | `main.py` | `""` | `str` | `.lstrip("@")` |
| `TELEGRAM_PROXY` / `PROXY_URL` | Бот / Core (`BotConfig`) | `main.py` | `None` | `str \| None` | `TELEGRAM_PROXY` or `PROXY_URL` or `None` |
| `READY_FILE_PATH` | Бот / Core (`BotConfig`) | `main.py` | `"/run/unagi/ready"` | `str` | строка пути |
| `EXPENSIVE_COMMAND_COOLDOWN_SECONDS` | Бот / Core (`BotConfig`) | `main.py` | `60` | `int` | `int(val or 60)` |
| `TELEGRAM_PREVIEW_CHARS` | Бот / Core (`BotConfig`) | `main.py` | `300` | `int` | `int(val or 300)` |
| `DIGEST_DUE_BATCH_SIZE` | Дайджесты (`DigestConfig`) | `main.py` | `100` | `int` | `max(1, int(val or 100))` |
| `DIGEST_BACKLOG_RETRY_SECONDS` | Дайджесты (`DigestConfig`) | `main.py` | `5` | `int` | `max(1, int(val or 5))` |
| `DIGEST_CHECK_INTERVAL_SECONDS` | Дайджесты (`DigestConfig`) | `main.py` | `1800` | `int` | `int(val or 1800)` |
| `DIGEST_TELEGRAPH_THRESHOLD` | Дайджесты (`DigestConfig`) | `main.py` | `4` | `int` | `int(val or 4)` |
| `RESCHEDULE_FOLLOWUP_WINDOW_MINUTES` | Время (`TimeConfig`) | `main.py`, `reminders_ux.py` | `10` | `int` | `int(val or 10)` |
| `EMAIL_DAILY_LIMIT_PER_USER` | Email (`EmailConfig`) | `main.py` | `5` | `int` | `int(val or 5)` |
| `EMAIL_DAILY_LIMIT_PER_ADDRESS` | Email (`EmailConfig`) | `main.py` | `5` | `int` | `int(val or 5)` |
