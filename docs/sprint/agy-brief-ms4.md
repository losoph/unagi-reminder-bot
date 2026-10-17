# MS4 — распил main.py, шаг 4: мастер оформления подписки на дайджест в Router

Вынеси мастер оформления подписки (setup_digest: FSM-состояния выбора канала/периода/папки/времени, его колбэки и хелперы, используемые только им) из main.py в новый модуль digest_setup.py с Router(name="digest_setup"), по образцу bookmarks.py (MS1), digest_settings.py (MS2), data_transfer.py (MS3). Поведение и тексты не менять.
Обязательная проверка: до правок `~/.venvs/unagi/bin/python tools/handler_order.py dump . > /tmp/ms4-before.tsv`, после — dump в /tmp/ms4-after.tsv и `compare` → «ИТОГ: порядок не конфликтует», набор хендлеров тот же (150). Результат compare — в docs/sprint/progress/MS4.md. Тест tests/test_digest_setup.py: роутер подключён, ключевые хендлеры на месте.
Если мастер тесно связан с фоновыми циклами или render_screen — не тяни их, оставь импортом из main и опиши в отчёте, что мешает.
Не трогай: фоновые циклы доставки, обработчики технических алертов, функции запуска/завершения бота (их меняет параллельный трек OPS3).
ОБЩЕЕ: тесты и линтер только из venv: ~/.venvs/unagi/bin/ruff check . и ~/.venvs/unagi/bin/python -m unittest discover -s tests — всё зелёное; без pip install. Прогресс — docs/sprint/progress/MS4.md. Коммит локально, push запрещён; первая строка сообщения коммита заканчивается маркером (MS4).
