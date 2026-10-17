# DOC1-fix — Исправление замечаний независимого ревью (dsh) — (DOC1-fix)

## Контекст
Независимое ревью (dsh) документации DOC1 выявило 9 замечаний по расхождению проектных документов с актуальным состоянием `origin/main` и кодом утилит:
1. Фиксация устаревшей политики очистки кеша сборщика `docker builder prune --filter until=48h` и роста диска «78% → 82%», тогда как в `origin/main` действует `until=6h` после достижения диском 86% на проде;
2. Описание `digest_settings.py` (MS2) как уже выделенного и слитого модуля, хотя он находится на ревью в PR 28 (ветка `feat/ms2-digest-router`);
3. Описание `tools/handler_order.py` как существующего в `main` инструмента статического анализа, тогда как файл находится в PR 28 и работает через интроспекцию дерева `dp` при импорте;
4. Разнобой в конвенциях журнала решений: монолитный `progress.md` vs пофайловый `docs/sprint/progress/<МЕТКА>.md` (разрешено слиянием PRG1 в `main`);
5. Несуществующая на `main` ссылка на `docs/sprint/owner-decisions-2026-10-17.md` (файл был изолирован в ветке `origin/docs/owner-decisions-2026-10-17`);
6. Устаревший baseline тестов («570 тестов, skipped=2») без учёта роста набора до 658 тестов и различий пропусков в CI от root;
7. Неточное описание проверки харденинга в `prod_check.sh`: утверждалась проверка `Uid: 10001`, хотя скрипт проверяет не-root, `CapEff: 0` и `NoNewPrivs: 1`;
8. Отсутствие `paths-ignore` для документации в `.github/workflows/deploy.yml`, что вызывало лишний деплой на проде и риск переполнения диска при docs-only коммитах;
9. Отсутствие валидации числа таблиц в `ops/aiqu/backup_db.py`: снимок пустой базы с 0 таблиц проходил проверку и возвращал код 0.

---

## Анализ и исправление каждого пункта

### 1. Политика очистки кеша сборщика (`until=6h`)
- **Дефект:** В `ops/aiqu/README.md:77`, `docs/HANDOFF.md:66` и `docs/architecture.md:251` была зафиксирована устаревшая команда `docker builder prune -f --filter until=48h` и динамика диска 78% → 82%, тогда как в `main` уже действовало сокращённое окно `until=6h` после достижения диском 86% (коммиты `1fcb1c3`, `f808976`).
- **Исправление:** В документации обновлена команда на `until=6h`, описан контекст 86% занятости диска при пороге ALERT 85% и политика удаления `:rollback` при переполнении.
- **Регрессионный тест:** `Doc1ReviewRegressionTests.test_item1_builder_prune_policy_is_6h` проверяет наличие `until=6h` и отсутствие `until=48h` во всех документах и в `deploy.yml`.

### 2. Статус MS2 (`digest_settings.py`)
- **Дефект:** В `docs/architecture.md:230–236`, `README.md:134` и `docs/roadmap.md:134` модуль `digest_settings.py` был описан как уже выделенный в `main` роутер, хотя он существует только в PR 28 (ветка `feat/ms2-digest-router`).
- **Исправление:** Статус MS2 явно зафиксирован как «на ревью в PR 28, ветка `feat/ms2-digest-router`»; в дорожной карте отражено, что в `main` слит только MS1 (`bookmarks.py`), а MS2 подготовлен и проходит ревью.
- **Регрессионный тест:** `Doc1ReviewRegressionTests.test_item2_ms2_described_as_in_review`.

### 3. Статус и принцип работы `tools/handler_order.py`
- **Дефект:** `tools/handler_order.py` упоминался как готовый инструмент в `main` и ошибочно назывался утилитой «статического анализа», хотя он импортирует `main` и обходит дерево роутеров `dp` в рантайме.
- **Исправление:** В `docs/architecture.md`, `README.md` и `docs/HANDOFF.md` указано, что инструмент входит в PR 28 (`feat/ms2-digest-router`) и работает методом динамической интроспекции зарегистрированного дерева роутеров `dp`.
- **Регрессионный тест:** `Doc1ReviewRegressionTests.test_item3_handler_order_described_accurately`.

### 4. Конвенция журнала решений
- **Дефект:** Конфликт между старым монолитным `docs/sprint/progress.md` и пофайловым `docs/sprint/progress/<МЕТКА>.md`.
- **Исправление:** В ветку влит актуальный `origin/main` с PRG1. Документы приведены к единому стандарту: `docs/sprint/progress/<МЕТКА>.md` — канонический журнал на задачу, `docs/sprint/progress.md` — исторический архив, `tools/agents/progress_index.py` — генератор индекса.
- **Регрессионный тест:** `Doc1ReviewRegressionTests.test_item4_progress_per_task_convention_consistent`.

### 5. Ссылка на `docs/sprint/owner-decisions-2026-10-17.md`
- **Дефект:** Ссылки из `docs/HANDOFF.md:135` и `docs/roadmap.md:148` вели на несуществующий в `main` файл (он находился в отдельной ветке `origin/docs/owner-decisions-2026-10-17`).
- **Исправление:** Файл перенесён из ветки `origin/docs/owner-decisions-2026-10-17` в `docs/sprint/owner-decisions-2026-10-17.md`; в документации указан контекст подготовки решений владельца по Очереди 4 на 17.10.2026.
- **Регрессионный тест:** `Doc1ReviewRegressionTests.test_item5_owner_decisions_file_exists`.

### 6. Актуальный baseline тестов
- **Дефект:** В `docs/HANDOFF.md:45,58,133` и `docs/architecture.md:244` было зафиксировано устаревшее число тестов «570 тестов, skipped=2».
- **Исправление:** Baseline обновлён до фактического состояния (658 тестов); явно описаны причины пропусков: локально 3 пропуска (без docker daemon и 2 перф-теста `FEED_PERF_TEST`), в CI без git/docker и от root скипаются тесты харденинга (`test_container_hardening`) и git-зависимые тесты (`test_orchestra`, `test_deploy_sync`).
- **Регрессионный тест:** `Doc1ReviewRegressionTests.test_item6_test_baseline_accuracy`.

### 7. Точное описание проверок харденинга в `prod_check.sh`
- **Дефект:** В `ops/aiqu/README.md:66` и `docs/architecture.md:254` заявлялась проверка «Uid: 10001», тогда как `prod_check.sh` даёт OK для любого не-root uid при `CapEff: 0` и `NoNewPrivs: 1` (а при Uid 0 — WARN).
- **Исправление:** Формулировки в документации приведены в точное соответствие с кодом `prod_check.sh` (проверка не-root пользователя, обнуления Linux capabilities `CapEff: 0` и флага `NoNewPrivs: 1`).
- **Регрессионный тест:** `Doc1ReviewRegressionTests.test_item7_prod_check_hardening_description`.

### 8. `paths-ignore` для документации в `deploy.yml`
- **Дефект:** Любой push в `main` запускал сборку образа и SSH-деплой с пересборкой на сервере, даже если изменялась только документация. Это создавало лишнюю нагрузку на 1 vCPU и тратило диск на сервере с 86% заполнения.
- **Исправление:** В `.github/workflows/deploy.yml` добавлены правила `paths-ignore: ['docs/**', '*.md']` для событий `push` и `pull_request`.
- **Регрессионный тест:** `Doc1ReviewRegressionTests.test_item8_deploy_workflow_has_paths_ignore_for_docs`.

### 9. Проверка числа таблиц в `ops/aiqu/backup_db.py`
- **Дефект:** Документация в `docs/architecture.md:58` декларировала проверку «PRAGMA quick_check и числа таблиц», но `snapshot()` только делал `SELECT count(*)` и не сравнивал его с минимумом. Пустая БД с 0 таблиц проходила `quick_check` и бэкапилась с кодом 0.
- **Исправление:** В `snapshot()` добавлен параметр `min_tables: int = 1`, проверяющий `tables >= min_tables` с выбросом `RuntimeError`, а в CLI `main()` добавлен аргумент `--min-tables` (по умолчанию 1) с валидацией `min_tables >= 1`. Пустые БД аварийно завершают скрипт с кодом 1 без сохранения некорректного снимка.
- **Регрессионные тесты:** `Doc1ReviewRegressionTests.test_item9_backup_db_rejects_empty_database_with_zero_tables`, `BackupDbTests.test_empty_database_with_zero_tables_fails`, `BackupDbTests.test_bad_arguments_exit_2`.

---

## Результаты проверок
- `ruff check .` — All checks passed!
- `python -m unittest discover -s tests` — 680 тестов успешно (OK, skipped=4 локально: 1 без age, 1 без docker daemon, 2 перф-теста).
- Никакие секреты не выводятся и не коммитятся.
