Задача T26 (хвосты напоминаний, docs/roadmap.md §1) — ветка feat/t26-undo-tail, этот worktree. git push запрещён — только
локальный коммит. Никаких pip install; тесты и линтер — только ~/.venvs/unagi/bin/python и ~/.venvs/unagi/bin/ruff; не
запускай команды, ждущие stdin; файлы пиши инструментом. Секреты, сеть и прод не трогать.

Файлы: data/database.py (replace_sent_reminder_with_pending, save_reschedule_undo, undo_followup_reschedule,
cleanup_old_records, таблица reschedule_undo_history), reminders_ux.py (около строк 660–685: перенос текстом после
доставки), main.py (место вызова cleanup_old_records около строки 5424 — как логируется результат).

Сделай две вещи, каждую с тестами (временная SQLite: `database.DB_PATH = tmp; database.init_db()`, как в соседних
tests/test_*.py; никаких абсолютных дат, сравниваемых с реальным «сейчас»):

1. Очистка `reschedule_undo_history`. В cleanup_old_records удалять записи старше суток (`created_timestamp` — UTC epoch
   секунды, REAL; `now_str` — строка UTC того же формата, что у остальных условий функции) и вернуть число в новом ключе
   `reschedule_undo_deleted`. Строки без created_timestamp не удалять молча — решение запиши в progress.md. Если main.py
   логирует результат поштучно по ключам — добавь новый ключ туда же. Тест: старая запись удалена, свежая осталась.

2. Атомарный откат. Сейчас `replace_sent_reminder_with_pending` (транзакция) и `save_reschedule_undo` (отдельное
   соединение) вызываются раздельно: если процесс упадёт между ними, напоминание перенесено, а откат сохранить не
   удалось. Добавь в replace_sent_reminder_with_pending необязательный именованный аргумент `undo: dict | None = None`
   (ключи `original_text`, `original_message_id`): при его наличии снимок старой строки (читается ВНУТРИ той же
   транзакции до DELETE) записывается в reschedule_undo_history до commit. Любая ошибка снимка откатывает всё: старая
   строка остаётся, нового pending нет. Путь в reminders_ux.py (follow-up перенос текстом) переведи на новый вызов и
   убери отдельный save_reschedule_undo оттуда; функцию save_reschedule_undo оставь (на неё могут ссылаться тесты). Остальные
   вызовы replace_sent_reminder_with_pending без undo работают как раньше. Тесты: снимок появился вместе с переносом и
   откат работает через undo_followup_reschedule; при сбое записи снимка (например, подменой SQL/таблицы) старая строка
   на месте и нового pending нет; вызов без undo не меняет поведение.

Не трогай feed*.py, tools/, ops/, .github/. Добавь блок «T26» в docs/sprint/progress.md по
docs/sprint/agy-progress-protocol.md (в том же коммите; 3–5 строк суждений, не список файлов). Из docs/roadmap.md §1
удали два выполненных пункта («Очистка reschedule_undo_history» и «Сохранение отката атомарно с переносом»).
Перед коммитом: `~/.venvs/unagi/bin/ruff check .` и `~/.venvs/unagi/bin/python -m unittest discover -s tests` зелёные.
Коммит: «feat: очистка и атомарное сохранение отката переноса (T26)». ИТОГ: хеш и список файлов.
