# CFG1: Единый модуль настроек вместо разрозненного os.getenv

## Статус
Завершено

### Реализация
1. Создан модуль `config.py` с frozen dataclass-ами: `FeedRankingConfig`, `ChannelSourceConfig`, `EmailConfig`, `TelegraphConfig`, `WatchdogConfig`, `TimeConfig` и корневым `Settings`.
2. Функция-фабрика `settings(env=None)` кеширует экземпляр через `@lru_cache`, автоматически обновляется при смене переменных через `patch.dict(os.environ)` и поддерживает явный сброс через `reset_settings()`, `reset()` и `settings.cache_clear()`.
3. Все 12 целевых модулей успешно переведены на использование `config.py`: `feed.py`, `feed_ai.py`, `feed_ranking.py`, `ranking.py`, `feed_render.py`, `channel_source.py`, `email_digest.py`, `telegraph_publisher.py`, `watchdog.py`, `time_utils.py`, `reminders_ux.py`, `scraper.py`.
4. Модули `main.py`, `data/database.py`, `ai_assistant.py`, `digest_alerts.py` не модифицировались (их переменные задокументированы в разделе 2).
5. Тесты `tests/test_config.py` проверяют все умолчания, разбор кастомных значений, ограничения min/max/clamping, валидацию и работу кэша.
6. Полный прогон `ruff check .` и `python -m unittest discover -s tests` успешен (702 теста, 0 ошибок, всё зелёное).

## 1. Таблица переносимых переменных конфигурации

Таблица переменных окружения для 12 модулей, переводимых на `config.py`:
`feed.py`, `feed_ai.py`, `feed_ranking.py`, `ranking.py`, `feed_render.py`, `channel_source.py`, `email_digest.py`, `telegraph_publisher.py`, `watchdog.py`, `time_utils.py`, reminders_ux.py`, `scraper.py`.

| Переменная | Область / Dataclass | Модуль-источник | Умолчание | Тип | Правило разбора / ограничения |
|---|---|---|---|---|---|
| `FEED_CACHE_MINUTES` | Лента / ранжирование (`FeedRankingConfig`) | `feed.py` | `15` | `int` | `int(val or 15)` |
| `FEED_MIN_INTERVAL_SECONDS` | Лента / ранжирование (`FeedRankingConfig`) | `feed.py` | `60` | `int` | `int(val or 60)` |
| `FEED_BUILD_TIMEOUT_SECONDS` | Лента / ранжирование (`FeedRankingConfig`) | `feed.py` | `120` | `int` | `int(val or 120)` |
| `FEED_PUBLISH_TIMEOUT_SECONDS` | Лента / ранжирование (`FeedRankingConfig`) | `feed.py` | `30` | `int` | `int(val or 30)` |
| `FEED_AUTODELIVERY_CHECK_SECONDS` | Лента / ранжирование (`FeedRankingConfig`) | `feed.py` | `120` | `int` | `int(val or 120)` |
| `FEED_AUTODELIVERY_GRACE_MINUTES` | Лента / ранжирование (`FeedRankingConfig`) | `feed.py` | `90` | `int` | `int(val or 90)` |
| `DIGEST_FETCH_CONCURRENCY` | Источник каналов (`ChannelSourceConfig`) / Лента | `feed.py`, `channel_source.py` | `5` | `int` | `int(val or 5)` |
| `FEED_AI_TIMEOUT_SECONDS` | Лента / ранжирование (`FeedRankingConfig`) | `feed_ai.py` | `20.0` | `float` | `max(0.1, float(val or 20.0))` |
| `FEED_AI_SUMMARY` | Лента / ранжирование (`FeedRankingConfig`) | `feed_ai.py` | `"on"` (включено) | `bool` / `str` | `val.strip().lower() != "off"` (выкл. только при `"off"`) |
| `FEED_POST_CHARS` | Лента / ранжирование (`FeedRankingConfig`) | `feed_render.py` | `300` | `int` | `max(1, int(val or 300))` |
| `FEED_W_REACH` | Лента / ранжирование (`FeedRankingConfig`) | `feed_ranking.py` | `0.30` | `float` | `float(val or 0.30)` |
| `FEED_W_FWD` | Лента / ранжирование (`FeedRankingConfig`) | `feed_ranking.py` | `0.25` | `float` | `float(val or 0.25)` |
| `FEED_W_REACH_REL` | Лента / ранжирование (`FeedRankingConfig`) | `feed_ranking.py` | `0.15` | `float` | `float(val or 0.15)` |
| `FEED_W_FWD_REL` | Лента / ранжирование (`FeedRankingConfig`) | `feed_ranking.py` | `0.15` | `float` | `float(val or 0.15)` |
| `FEED_W_ENGAGEMENT` | Лента / ранжирование (`FeedRankingConfig`) | `feed_ranking.py` | `0.10` | `float` | `float(val or 0.10)` |
| `FEED_MAX_POSTS` | Лента / ранжирование (`FeedRankingConfig`) | `feed_ranking.py` | `50` | `int` | `max(0, int(val or 50))` |
| `FEED_AD_MIN_PERCENTILE` | Лента / ранжирование (`FeedRankingConfig`) | `feed_ranking.py` | `0.75` | `float` | `float(val or 0.75)` |
| `FEED_AD_MIN_POOL` | Лента / ранжирование (`FeedRankingConfig`) | `feed_ranking.py` | `4` | `int` | `max(1, int(val or 4))` |
| `FEED_CLUSTER_MIN_TEXT` | Лента / ранжирование (`FeedRankingConfig`) | `feed_ranking.py` | `60` | `int` | `max(0, int(val or 60))` |
| `PRIORITIZE_THRESHOLD_PER_DAY` | Лента / ранжирование (`FeedRankingConfig`) | `ranking.py` | `20` | `int` | `int(val or 20)` |
| `MAX_POSTS_PER_CHANNEL` | Лента / ранжирование (`FeedRankingConfig`) | `ranking.py` | `5` | `int` | `int(val or 5)` |
| `FORWARD_STANDOUT_RATIO` | Лента / ранжирование (`FeedRankingConfig`) | `ranking.py` | `1.25` | `float` | `float(val or 1.25)` |
| `FORWARD_STANDOUT_MAX_EXTRA` | Лента / ранжирование (`FeedRankingConfig`) | `ranking.py` | `3` | `int` | `int(val or 3)` |
| `SCORE_W_FWD` | Лента / ранжирование (`FeedRankingConfig`) | `ranking.py` | `0.40` | `float` | `float(val or 0.40)` |
| `SCORE_W_VIEWS` | Лента / ранжирование (`FeedRankingConfig`) | `ranking.py` | `0.25` | `float` | `float(val or 0.25)` |
| `SCORE_W_REACTIONS` | Лента / ранжирование (`FeedRankingConfig`) | `ranking.py` | `0.15` | `float` | `float(val or 0.15)` |
| `SCORE_W_REPLIES` | Лента / ранжирование (`FeedRankingConfig`) | `ranking.py` | `0.05` | `float` | `float(val or 0.05)` |
| `SCORE_W_FRESHNESS` | Лента / ранжирование (`FeedRankingConfig`) | `ranking.py` | `0.15` | `float` | `float(val or 0.15)` |
| `CHANNEL_SOURCE` | Источник каналов (`ChannelSourceConfig`) | `channel_source.py` | `"auto"` | `str` | `.strip().lower()`, валидные: `auto`, `mtproto`, `web` |
| `TELEGRAM_API_ID` | Источник каналов (`ChannelSourceConfig`) | `channel_source.py` | `None` | `str \| None` | сырая строка или `None` |
| `TELEGRAM_API_HASH` | Источник каналов (`ChannelSourceConfig`) | `channel_source.py` | `None` | `str \| None` | сырая строка или `None` |
| `TELEGRAM_USER_SESSION` | Источник каналов (`ChannelSourceConfig`) | `channel_source.py` | `None` | `str \| None` | сырая строка или `None` |
| `MTPROTO_WEB_FALLBACK` | Источник каналов (`ChannelSourceConfig`) | `channel_source.py` | `True` | `bool` | `.lower() in {"1", "true", "yes"}` |
| `MTPROTO_FETCH_LIMIT` | Источник каналов (`ChannelSourceConfig`) | `channel_source.py` | `1000` | `int` | `max(20, int(val or 1000))` |
| `WEB_FETCH_MAX_PAGES` | Источник каналов (`ChannelSourceConfig`) | `scraper.py` | `15` | `int` | `int(val or 15)` |
| `WEB_FETCH_PAGE_DELAY` | Источник каналов (`ChannelSourceConfig`) | `scraper.py` | `0.5` | `float` | `float(val or 0.5)` |
| `MSMTPRC_PATH` | Email (`EmailConfig`) | `email_digest.py` | `"/run/secrets/msmtprc"` | `str` | сырая строка пути |
| `SMTP_HOST` | Email (`EmailConfig`) | `email_digest.py` | `""` | `str` | строка хоста (fallback на msmtprc) |
| `SMTP_PORT` | Email (`EmailConfig`) | `email_digest.py` | `None` | `int \| None` | целое число или `None` (вычисляется по протоколу: 465/587/25) |
| `SMTP_SECURITY` | Email (`EmailConfig`) | `email_digest.py` | `""` | `str` | `.lower()`, валидные: `ssl`, `starttls`, `none` |
| `SMTP_USER` | Email (`EmailConfig`) | `email_digest.py` | `""` | `str` | строка пользователя |
| `SMTP_PASSWORD` | Email (`EmailConfig`) | `email_digest.py` | `""` | `str` | строка пароля |
| `SMTP_FROM` | Email (`EmailConfig`) | `email_digest.py` | `""` | `str` | строка адреса отправителя |
| `TELEGRAPH_TOKEN` | Telegraph (`TelegraphConfig`) | `telegraph_publisher.py` | `None` | `str \| None` | строка токена или `None` (fallback на `app_meta` / регистрацию) |
| `TELEGRAPH_POST_CHARS` | Telegraph (`TelegraphConfig`) | `telegraph_publisher.py` | `1500` | `int` | `int(val or 1500)` |
| `WATCHDOG_CHECK_INTERVAL_SECONDS` | Watchdog (`WatchdogConfig`) | `watchdog.py` | `300` | `int` | `max(10, int(val or 300))` |
| `WATCHDOG_CYCLE_STALL_SECONDS` | Watchdog (`WatchdogConfig`) | `watchdog.py` | `7200` | `int` | `max(60, int(val or 7200))` |
| `WATCHDOG_BACKLOG_GROWTH_CYCLES` | Watchdog (`WatchdogConfig`) | `watchdog.py` | `3` | `int` | `max(2, int(val or 3))` |
| `ADMIN_USER_ID` | Watchdog (`WatchdogConfig`) | `watchdog.py` | `""` | `str` | список ID через запятую |
| `ADMIN_CHAT_ID` | Watchdog (`WatchdogConfig`) | `watchdog.py` | `""` | `str` | список ID через запятую (fallback к `ADMIN_USER_ID`) |
| `APP_TIMEZONE` | Время (`TimeConfig`) | `time_utils.py` | `"Europe/Moscow"` | `str` | IANA-имя таймзоны (`ZoneInfo`) |
| `RESCHEDULE_FOLLOWUP_WINDOW_MINUTES` | Время (`TimeConfig`) / Reminders | `reminders_ux.py` | `10` | `int` | `int(val or 10)` |

Сверка значений по умолчанию: каждое значение по умолчанию и каждое правило разбора (ограничения `max()`, приведение к нижнему регистру, разбор булевых значений) в точности повторяют существующее поведение в исходных модулях.

---

## 2. Неизменяемые модули (список переменных окружения для других треков)

Следующие модули **не изменяются** в рамках CFG1 согласно заданию (их изменяют параллельные треки). Ниже приведён полный каталог переменных окружения, используемых в них, для включения в `config.py` в будущих треках:

### `main.py`
- `BOT_TOKEN`: `str | None` — токен Telegram-бота (без значения по умолчанию, `os.getenv("BOT_TOKEN")`).
- `BOT_USERNAME`: `str` — имя бота (`os.getenv("BOT_USERNAME", "").lstrip("@")`, по умолчанию `""`).
- `TELEGRAM_PROXY` / `PROXY_URL`: `str | None` — URL прокси для запросов к Telegram Bot API (`os.getenv("TELEGRAM_PROXY") or os.getenv("PROXY_URL")`).
- `DIGEST_DUE_BATCH_SIZE`: `int` — размер пакета дайджестов для обработки (`max(1, int(os.getenv("DIGEST_DUE_BATCH_SIZE", "100")))`, по умолчанию `100`).
- `DIGEST_BACKLOG_RETRY_SECONDS`: `int` — задержка повторной попытки при переполнении бэклога (`max(1, int(os.getenv("DIGEST_BACKLOG_RETRY_SECONDS", "5")))`, по умолчанию `5`).
- `DIGEST_CHECK_INTERVAL_SECONDS`: `int` — интервал проверки очереди дайджестов (`int(os.getenv("DIGEST_CHECK_INTERVAL_SECONDS", 1800))`, по умолчанию `1800` (30 минут)).
- `RESCHEDULE_FOLLOWUP_WINDOW_MINUTES`: `int` — окно перепланирования напоминания (`int(os.getenv("RESCHEDULE_FOLLOWUP_WINDOW_MINUTES", "10"))`, по умолчанию `10`).
- `DIGEST_TELEGRAPH_THRESHOLD`: `int` — порог числа постов для публикации дайджеста в Telegraph (`int(os.getenv("DIGEST_TELEGRAPH_THRESHOLD", "4"))`, по умолчанию `4`).
- `TELEGRAM_PREVIEW_CHARS`: `int` — лимит символов превью дайджеста в сообщении Telegram (`int(os.getenv("TELEGRAM_PREVIEW_CHARS", "300"))`, по умолчанию `300`).
- `MAX_IMPORT_SUBSCRIPTIONS`: `int` — макс. подписок при импорте (`int(os.getenv("MAX_IMPORT_SUBSCRIPTIONS", "300"))`, по умолчанию `300`).
- `MAX_IMPORT_FEED`: `int` — макс. каналов ленты при импорте (`int(os.getenv("MAX_IMPORT_FEED", "200"))`, по умолчанию `200`).
- `MAX_IMPORT_BOOKMARKS`: `int` — макс. закладок при импорте (`int(os.getenv("MAX_IMPORT_BOOKMARKS", "1000"))`, по умолчанию `1000`).
- `MAX_IMPORT_REMINDERS`: `int` — макс. напоминаний при импорте (`int(os.getenv("MAX_IMPORT_REMINDERS", "500"))`, по умолчанию `500`).
- `MAX_IMPORT_FILE_BYTES`: `int` — макс. размер файла импорта в байтах (`int(os.getenv("MAX_IMPORT_FILE_BYTES", str(2 * 1024 * 1024)))`, по умолчанию `2097152` (2 МБ)).
- `EXPENSIVE_COMMAND_COOLDOWN_SECONDS`: `int` — кулдаун для тяжёлых команд пользователя (`int(os.getenv("EXPENSIVE_COMMAND_COOLDOWN_SECONDS", "60"))`, по умолчанию `60`).
- `EMAIL_DAILY_LIMIT_PER_USER`: `int` — суточный лимит писем на одного пользователя (`int(os.getenv("EMAIL_DAILY_LIMIT_PER_USER", "5"))`, по умолчанию `5`).
- `EMAIL_DAILY_LIMIT_PER_ADDRESS`: `int` — суточный лимит писем на один адрес (`int(os.getenv("EMAIL_DAILY_LIMIT_PER_ADDRESS", "5"))`, по умолчанию `5`).

### `data/database.py`
- `MESSAGE_RETENTION_DAYS`: `int` — срок хранения сообщений (`int(os.getenv("MESSAGE_RETENTION_DAYS", "30"))`, по умолчанию `30`).
- `SUBSCRIPTION_FAILURE_RETENTION_DAYS`: `int` — срок хранения ошибок подписок (`int(os.getenv("SUBSCRIPTION_FAILURE_RETENTION_DAYS", "90"))`, по умолчанию `90`).
- `DIGEST_POST_RETENTION_DAYS`: `int` — срок хранения постов дайджестов (`int(os.getenv("DIGEST_POST_RETENTION_DAYS", "30"))`, по умолчанию `30`).
- `MAX_ERROR_TEXT_LENGTH`: `int` — макс. длина сохраняемого текста ошибки (`int(os.getenv("MAX_ERROR_TEXT_LENGTH", "800"))`, по умолчанию `800`).
- `CHANNEL_BACKLOG_MAX`: `int` — макс. размер бэклога канала (`int(os.getenv("CHANNEL_BACKLOG_MAX", "500"))`, по умолчанию `500`).
- `CHANNEL_DELIVERY_BATCH_SIZE`: `int` — размер пакета доставки постов (`int(os.getenv("CHANNEL_DELIVERY_BATCH_SIZE", "50"))`, по умолчанию `50`).
- `MAX_FEED_CHANNELS`: `int` — квота каналов ленты на пользователя (`int(os.getenv("MAX_FEED_CHANNELS", "100"))`, по умолчанию `100`).
- `MAX_DIGEST_SUBSCRIPTIONS`: `int` — квота подписок на пользователя (`int(os.getenv("MAX_DIGEST_SUBSCRIPTIONS", "300"))`, по умолчанию `300`).
- `MAX_USER_REMINDERS`: `int` — квота напоминаний на пользователя (`int(os.getenv("MAX_USER_REMINDERS", "1000"))`, по умолчанию `1000`).
- `MAX_USER_SAVED_MESSAGES`: `int` — квота закладок на пользователя (`int(os.getenv("MAX_USER_SAVED_MESSAGES", "2000"))`, по умолчанию `2000`).
- `DB_PATH`: `str` — путь к файлу БД SQLite (`os.getenv("DB_PATH", str(BASE_DIR / "bot_data.db"))`, по умолчанию `bot_data.db` в корне).

### `ai_assistant.py`
- `DEEPSEEK_API_KEY`: `str | None` — API-ключ DeepSeek (`os.getenv("DEEPSEEK_API_KEY")`, по умолчанию `None`).
- `DEEPSEEK_BASE_URL`: `str` — базовый URL API DeepSeek (`os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")`, по умолчанию `"https://api.deepseek.com"`).
- `DEEPSEEK_MODEL`: `str` — модель DeepSeek (`os.getenv("DEEPSEEK_MODEL", "deepseek-chat")`, по умолчанию `"deepseek-chat"`).
- `AI_DAILY_LIMIT`: `int` — дневной лимит обращений к ИИ на пользователя (`int(os.getenv("AI_DAILY_LIMIT", "10"))`, по умолчанию `10`).

### `digest_alerts.py`
- `DIGEST_ALERT_GRACE_PERIOD_SECONDS`: `int` — льготный период до отправки алерта (`max(60, int(os.getenv("DIGEST_ALERT_GRACE_PERIOD_SECONDS", "3600")))`, по умолчанию `3600`).
- `MAX_DIGEST_RETRIES`: `int` — максимальное количество попыток доставки дайджеста (`max(1, int(os.getenv("MAX_DIGEST_RETRIES", "5")))`, по умолчанию `5`).
- `ALERT_BASE_COOLDOWN_SECONDS`: `int` — базовый кулдаун между алертами (`max(60, int(os.getenv("ALERT_BASE_COOLDOWN_SECONDS", "14400")))`, по умолчанию `14400`).
- `ALERT_MAX_COOLDOWN_SECONDS`: `int` — максимальный экспоненциальный кулдаун алертов (`max(60, int(os.getenv("ALERT_MAX_COOLDOWN_SECONDS", "86400")))`, по умолчанию `86400`).
