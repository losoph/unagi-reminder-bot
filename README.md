# Unagi Reminder Bot

Telegram-бот для отложенных напоминаний и дайджестов по Telegram-каналам.

## Возможности

- **Напоминания**: пересланное сообщение → отложить; быстрый перенос (`/morning`, `/day`, `/evening`, `/later`, `/at`).
- **База знаний (закладки)**: теги-папки, страницы, перенос между категориями, превращение в напоминание.
- **Дайджесты публичных каналов**: подписка пересылкой поста, периодичность daily/weekly/monthly с настройкой времени.
  - Группировка подписок по папкам, маркеры статуса (⏸ пауза / ⚠️ ошибка чтения).
  - Пауза/возобновление как по одному каналу, так и массово; отписаться от всех.
  - Большие дайджесты (по умолчанию 4+ постов) публикуются в **Telegraph** одной ссылкой.
  - `/check` — проверка доступности всех каналов.
- **Анализ каналов (DeepSeek)**: по каждому каналу — частота постинга, оценка времени чтения, рекомендация по периодичности дайджеста и саммари постов за 30 дней. Вопросы заготовленные (кнопки), без свободного ввода. Работает при заданном `DEEPSEEK_API_KEY`; без него остаётся расчётная часть (частота/время чтения).
- **Экспорт/импорт (JSON)**: `/export` выгружает напоминания, закладки, подписки и настройки дайджеста; `/import` восстанавливает их из присланного JSON-файла.

## Требования

- Python 3.12+
- Telegram bot token

## Локальный запуск

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
mkdir -p storage
python main.py
```

Если использовать `.env.example` без изменений, SQLite-файл будет создан в `storage/bot_data.db`.
Если `DB_PATH` не задан, приложение использует путь по умолчанию `bot_data.db` в корне проекта.

## Переменные окружения

- `BOT_TOKEN` - токен Telegram-бота, обязателен
- `APP_TIMEZONE` - часовой пояс приложения, по умолчанию `Europe/Moscow`
- `DB_PATH` - путь к SQLite-файлу
- `MESSAGE_RETENTION_DAYS` - срок хранения обработанных напоминаний
- `SUBSCRIPTION_FAILURE_RETENTION_DAYS` - срок хранения permanently failed подписок
- `DIGEST_POST_RETENTION_DAYS` - срок хранения постов в базе знаний для ИИ-анализа (по умолчанию 30)
- `MAX_ERROR_TEXT_LENGTH` - лимит сохраненного текста ошибки
- `DIGEST_TELEGRAPH_THRESHOLD` - с какого числа постов дайджест публикуется в Telegraph (по умолчанию 4)
- `TELEGRAPH_TOKEN` - необязательный токен Telegraph (иначе создаётся автоматически)
- `DEEPSEEK_API_KEY` - ключ DeepSeek; без него ИИ-функции отключены
- `DEEPSEEK_BASE_URL` - базовый URL API DeepSeek (по умолчанию `https://api.deepseek.com`)
- `DEEPSEEK_MODEL` - модель DeepSeek (по умолчанию `deepseek-chat`)
- `AI_DAILY_LIMIT` - лимит ИИ-запросов в сутки на пользователя (0 = без лимита)
- `MAX_IMPORT_SUBSCRIPTIONS` / `MAX_IMPORT_BOOKMARKS` / `MAX_IMPORT_REMINDERS` / `MAX_IMPORT_FILE_BYTES` - лимиты импорта JSON

## Перспективные доработки

- **Импорт OPML**: помимо JSON-бэкапа поддержать импорт списков каналов в формате OPML
  (`<outline xmlUrl="https://t.me/s/channel">`), чтобы переносить подписки из RSS-читалок и
  других Telegram-RSS-ботов. Период при этом ставится по умолчанию.

## Запуск на VDS

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
mkdir -p storage
python main.py
```

Для systemd в качестве рабочей директории используй каталог проекта, чтобы `python main.py` корректно находил модуль `data.database`.

Пример `ExecStart`:

```ini
ExecStart=/opt/unagi-reminder-bot/.venv/bin/python /opt/unagi-reminder-bot/main.py
WorkingDirectory=/opt/unagi-reminder-bot
```

## Запуск в Docker

```bash
cp .env.example .env
docker compose up --build -d
```

В контейнере база по умолчанию хранится в `/app/storage/bot_data.db`, это значение уже задано в `.env.example`.

