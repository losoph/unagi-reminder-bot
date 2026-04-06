# Unagi Reminder Bot

Telegram-бот для отложенных напоминаний и дайджестов по Telegram-каналам.

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
- `MAX_ERROR_TEXT_LENGTH` - лимит сохраненного текста ошибки

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
