# OPS3 — Надёжность деплоя и завершения по критике Argon (18.10)

## Контекст и проблемы
По результатам аудита Argon (18.10.2026) были выявлены критические уязвимости и точки отказа в процессах деплоя, проверки работоспособности и жизненного цикла бота:
1. **Затирание рабочего отката:** Откатный тег `unagi-reminder-bot-bot:rollback` обновлялся перед каждой сборкой без проверки состояния текущего контейнера. Если предыдущий деплой упал на этапе liveness, следующий деплой затирал заведомо рабочий образ сломанным.
2. **Отсутствие автоотката в CI:** При сбое проверки живости процесс деплоя просто падал (`exit 1`), оставляя упавший или сбойный контейнер на проде до ручного вмешательства администратора.
3. **Слепая пауза вместо контроля готовности:** Фиксированный `sleep 20` не гарантировал, что бот успел инициализироваться и готов обрабатывать сообщения, а также приводил к ложным падениям при единственном быстром перезапуске на старте либо не замечал циклических падений после 20-й секунды.
4. **Переполнение диска при сборке:** Очистка Docker (`docker image prune` и `docker builder prune`) запускалась только после сборки нового образа, из-за чего сборка могла упасть из-за нехватки диска (порог ALERT prod_check — 85%).
5. **Создание WAL/SHM файлов от root:** Скрипт создания снимка БД `backup_db.py` в `deploy.yml` и проверки `sqlite3` в `prod_check.sh` запускались из-под `root`. При открытии базы данных SQLite в режиме WAL создавались файлы `-wal` и `-shm`, принадлежащие `root:root`, из-за чего непривилегированный бот (`uid 10001`) получал ошибку `attempt to write a readonly database`.
6. **Отсутствие graceful shutdown и сброса WAL:** При остановке контейнера фоновые циклы обрывались аварийно, а WAL-лог не сбрасывался в основную БД. В `docker-compose.yml` не был настроен `stop_grace_period`.
7. **Устаревшие cron-файлы:** При удалении или переименовании задач в `ops/aiqu/cron.d` старые файлы в `/etc/cron.d` оставались на сервере навсегда, продолжая выполняться. Кроме того, отсутствовал контроль правил cron run-parts (запрет точек в имени файла и обязательный перевод строки в конце).

---

## Реализованные решения

### 1. Здоровый откатный тег (deploy.yml)
- В `.github/workflows/deploy.yml` тегирование `unagi-reminder-bot-bot:rollback` выполняется **только если текущий контейнер полностью здоров**:
  ```bash
  current_image="$(docker inspect -f '{{.Image}}' unagi-bot 2>/dev/null || true)"
  current_status="$(docker inspect -f '{{.State.Status}}' unagi-bot 2>/dev/null || echo missing)"
  current_restarts="$(docker inspect -f '{{.RestartCount}}' unagi-bot 2>/dev/null || echo -1)"
  if [ -n "$current_image" ] && [ "$current_status" = "running" ] && [ "$current_restarts" = "0" ]; then
    docker tag "$current_image" unagi-reminder-bot-bot:rollback 2>/dev/null || true
  fi
  ```
- Если предыдущий деплой упал или контейнер перезапускался, существующий тег `:rollback` не перезаписывается сломанным образом.

### 2. Автооткат при провале liveness (deploy.yml)
- Блок `# --- rollback` ... `# --- end rollback` встроен в обработку сбоя проверки живости перед `exit 1`:
  ```bash
  # --- rollback
  if docker inspect unagi-reminder-bot-bot:rollback >/dev/null 2>&1; then
    echo "откат: запуск контейнера из unagi-reminder-bot-bot:rollback"
    docker tag unagi-reminder-bot-bot:rollback unagi-reminder-bot-bot:latest
    docker compose up -d
    echo "откат выполнен"
  fi
  # --- end rollback
  exit 1
  ```
- При сбое контейнер автоматически перезапускается из сохранённого рабочего образа `:rollback`, обеспечивая доступность сервиса, при этом шаг CI завершается с ненулевым кодом выхода (`exit 1`), сигнализируя о провале деплоя.

### 3. Проверка готовности вместо фиксированного sleep
- В блоке `# --- liveness` реализован цикл динамического опроса каждые 2 с до `${DEPLOY_WAIT_SECS:-60}`:
  - Контейнер находится в статусе `running`;
  - Метка `org.unagi.git_sha` совпадает с целевым `GIT_SHA`;
  - Прикладной маркер готовности: `docker exec unagi-bot test -f /run/unagi/ready`;
  - Стабильность: `RestartCount` не растёт в течение как минимум 10 с. Допускается `RestartCount <= 1`, если после старта контейнер стабильно работает 10 с без повторных падений.
- В `main.py` добавлены функции `write_ready_marker` и `remove_ready_marker`:
  - Маркер `/run/unagi/ready` создаётся после завершения инициализации базы данных (`init_db()`), роутеров и фоновых задач;
  - Маркер удаляется при штатной остановке бота.

### 4. Очистка диска до сборки нового образа
- В `deploy.yml` перед `docker compose up -d --build` добавлена превентивная очистка кеша и висячих образов при заполнении диска >= 85%:
  ```bash
  disk_pre="$(df --output=pcent . 2>/dev/null | tail -n 1 | tr -dc '0-9')"
  if [ -n "$disk_pre" ] && [ "$disk_pre" -ge 85 ]; then
    echo "диск ${disk_pre}% >= 85% до сборки: очистка кеша сборки и висячих образов"
    docker builder prune -f || true
    docker image prune -f || true
  fi
  ```

### 5. Снимок БД и проверки не от root
- В `.github/workflows/deploy.yml`:
  - Каталог `storage/backups` создаётся и передаётся во владение `10001:10001`;
  - `backup_db.py` вызывается от имени `uid 10001` через `setpriv --reuid=10001 --regid=10001 --clear-groups`.
- В `ops/aiqu/prod_check.sh`:
  - В функции `remote()` вызовы `sqlite3 -readonly` обёрнуты в запуск через `setpriv --reuid=10001 --regid=10001 --clear-groups`.
- В `ops/docker/entrypoint.sh`:
  - Добавлено создание `$STORAGE/backups` и передача прав на каталог пользователю `BOT_UID:BOT_GID`;
  - Добавлен `chown` на директорию `$SMTP_DIR` (`/run/unagi`), что позволяет боту создавать маркер готовности `ready`.

### 6. Корректное завершение и PRAGMA wal_checkpoint(TRUNCATE)
- В `data/database.py` реализована функция `checkpoint_db()`, вызывающая `PRAGMA wal_checkpoint(TRUNCATE)`.
- В `main.py`:
  - Реализована функция `perform_shutdown()`:
    - Отменяет все фоновые задачи (`_background_tasks`) и ожидает их завершения через `asyncio.gather(..., return_exceptions=True)`;
    - Вызывает `feed.cancel_backfill_tasks()`;
    - Закрывает `channel_source` и HTTP-сессию бота;
    - Удаляет маркер готовности `/run/unagi/ready`;
    - Выполняет `checkpoint_db()` для полного сброса WAL-лога в файл базы данных и усечения `.db-wal` до 0 байт;
  - Обработчик зарегистрирован в aiogram через `@dp.shutdown()` и продублирован в секции `finally:` корутины `main()`.
- В `docker-compose.yml` сервису `bot` добавлен параметр `stop_grace_period: 30s`.

### 7. Очистка устаревших cron-файлов и валидация
- В `deploy.yml` перед копированием cron-файлов добавлен цикл удаления устаревших файлов:
  ```bash
  for installed in /etc/cron.d/unagi-*; do
    if [ -f "$installed" ] && [ ! -f "ops/aiqu/cron.d/$(basename "$installed")" ]; then
      echo "удаление устаревшего cron-файла: $installed"
      rm -f "$installed" || true
    fi
  done
  ```
- Создан тестовый модуль `tests/test_cron_files.py`:
  - Проверяет все файлы `ops/aiqu/cron.d/*`: префикс `unagi-`, отсутствие точек в имени, завершающий символ `\n`;
  - Тестирует логику очистки устаревших файлов на изолированной временной структуре каталогов.

---

## Регрессионные тесты

1. **`tests/test_dep2_deploy_verify.py`:**
   - `test_deploy_tags_rollback_only_when_container_healthy`: контроль обновления `:rollback` только при `State.Status=running` и `RestartCount=0`.
   - `test_deploy_cleans_disk_before_build`: проверка наличия очистки `docker builder prune -f` и `docker image prune -f` до сборки при `df >= 85%`.
   - `test_deploy_runs_backup_as_non_root`: проверка запуска `backup_db.py` под `setpriv --reuid=10001 --regid=10001`.
   - `test_prod_check_sqlite_runs_as_non_root`: проверка запуска `sqlite3 -readonly` под `setpriv` в `prod_check.sh`.
   - `test_liveness_fails_when_ready_marker_missing`: проверка падения liveness при отсутствии маркера `/run/unagi/ready`.
   - `test_liveness_allows_single_restart_if_stable`: подтверждение успеха при 1 быстром перезапуске и последующей 10-секундной стабильности.
   - `test_liveness_triggers_auto_rollback_on_failure`: тестирование срабатывания автоотката на образ `:rollback` при сбое liveness.
   - `test_liveness_skips_rollback_when_no_rollback_image`: проверка корректного пропуска отката, если образ `:rollback` отсутствует.
   - `test_rollback_block_isolated_execution`: изолированное исполнение блока `# --- rollback` с фейковым docker.

2. **`tests/test_shutdown_checkpoint.py`:**
   - `test_checkpoint_db_truncates_wal_file`: проверка, что `checkpoint_db()` выполняет чекпоинт и усекает WAL-файл до 0 байт на временной БД.
   - `test_perform_shutdown_calls_checkpoint_and_stops_tasks`: проверка остановки фоновых циклов, удаления маркера готовности и сброса WAL.
   - `test_ready_marker_create_and_remove`: тестирование создания и очистки файла готовности.

3. **`tests/test_cron_files.py`:**
   - `test_cron_directory_exists_and_not_empty`: наличие каталога `ops/aiqu/cron.d`.
   - `test_cron_files_naming_and_formatting`: строгая проверка префикса `unagi-`, отсутствия `.` и наличия trailing newline.
   - `test_stale_cron_cleanup_and_install_logic`: верификация удаления удалённых из репозитория cron-файлов и сохранения чужих/системных.

4. **`tests/test_container_hardening.py`:**
   - `test_stop_grace_period`: проверка наличия `stop_grace_period: 30s` в `docker-compose.yml`.

---

## Результаты проверок

- Линтер: `~/.venvs/unagi/bin/ruff check .` — без замечаний (All checks passed).
- Тесты: `~/.venvs/unagi/bin/python -m unittest discover -s tests` — 100% тестов пройдены успешно (зелёные).
