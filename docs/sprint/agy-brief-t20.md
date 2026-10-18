# T20 — схема папок/выпусков и строгая миграция (очередь 4, модель «папка = одна рассылка»)

Источники правды (читай, не пересказывай по памяти): docs/sprint/queue4-design.md (§3 DDL, §4 миграция, §6 конфликты, §8 T20), раздел «Ответы владельца (18.10.2026)» в docs/sprint/owner-decisions-2026-10-17.md, ops/migrate_schedules_dryrun.py и tests/test_migrate_schedules_dryrun.py (логику анализа переиспользуй через import analyze_migration, не копируй).

ЧТО СДЕЛАТЬ
1. data/database.py, init_db(): идемпотентно создать таблицы folders, digest_editions, digest_edition_items (DDL из §3.1 дословно) и колонки/индексы из §3.2: subscriptions.folder_id, user_prefs.digest_scheduler_version DEFAULT 'v1', digest_executions.folder_id, digest_alerts.folder_id, частичные уникальные индексы. Колонки добавляй тем же идиомом, что остальные ALTER в init_db (повтор не должен падать). PRAGMA foreign_keys глобально НЕ включай: сейчас FK в database.py не включены нигде, включение изменит поведение планировщика v1. Включай только в соединении мигратора и в тестах схемы; решение запиши в журнал.
2. ops/migrate_schedules.py: CLI `migrate_schedules.py DB_PATH [--apply] [--email-folders]`.
   - Без --apply — только отчёт (через analyze_migration), БД не пишется.
   - Есть конфликты (отчёт has_conflicts) → код 3, в БД ничего не меняется. АВТОМАТИЧЕСКОГО РАЗРЕШЕНИЯ КОНФЛИКТОВ НЕТ (решение владельца 18.10): ни слияния папок, ни выбора «старшего периода», ни отключения подписок.
   - Нет конфликтов и --apply → одна транзакция BEGIN IMMEDIATE, busy_timeout=5000, шаги §4.1 п.4–6, ON CONFLICT DO NOTHING, повторный запуск ничего не меняет. Курсор subscriptions.last_post_id не трогать, digest_scheduler_version остаётся 'v1', планировщик v1 продолжает работать.
   - Почта: в решениях владельца 18.10 («Почта» — legacy-путь period, без миграции) и в §4.1 проекта (delivery='email') есть расхождение. Поэтому по умолчанию все папки получают delivery='telegram', user_emails/email_periods не читаются для записи; перевод email-пользователей в delivery='email' — только с флагом --email-folders. Расхождение опиши в журнале как открытый вопрос владельцу.
   - Исключения/ошибки → откат транзакции, код 1.
3. Тесты (только tmp-БД, реальную и bot_data.db из корня репо не использовать): tests/test_q4_t20_schema.py (таблицы, колонки, индексы; повторный init_db; старые таблицы и данные целы) и tests/test_migrate_schedules.py (чистая БД; конфликт → код 3 и БД неизменна по хэшу файла; идемпотентность; --apply на однородных данных; last_post_id и digest_scheduler_version не меняются; откат при сбое посреди транзакции; флаг --email-folders).
4. Журнал решений: docs/sprint/progress/Q4T20.md — выбор и компромиссы. В нём же — порядок запуска владельцем на проде (только описание, ты ничего не запускаешь на проде): backup_db.py --tag pre-q4s → dry-run на копии (ворота G1: код 0) → --apply на остановленном боте → откат через restore_db.py.

НЕ ТРОГАЙ: main.py, digest_*.py, email_digest.py, handlers, планировщик v1, ai_assistant.py, digest_alerts.py, config.py.

ОБЩЕЕ: ветка worktree от origin/main. Тесты и линтер только из venv: ~/.venvs/unagi/bin/ruff check . и ~/.venvs/unagi/bin/python -m unittest discover -s tests — всё зелёное, существующие тесты не ослаблять; никаких pip install и новых зависимостей. Секреты не печатать и не коммитить. Коммит локально, git push запрещён; первая строка сообщения коммита заканчивается маркером задачи. Маркер: (Q4T20).
