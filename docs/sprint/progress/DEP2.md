# DEP2 — Контроль версии контейнера, проверка живости и быстрый откат

## Контекст и проблема
17.10.2026 дважды произошёл сбой:
- Деплой упал при шаге `up`, контейнер остался на старом образе;
- `prod_check.sh` показывал «версия OK», так как сверял только `git rev-parse HEAD` в репозитории на сервере, а не реальный код, работающий внутри контейнера;
- Не было удобного способа быстро откатить контейнер к заведомо рабочему образу без повторной долгой пересборки.

## Решение

### 1. Метка версии контейнера
- В `docker-compose.yml` добавлена метка сервиса bot:
  ```yaml
  labels: {org.unagi.git_sha: "${GIT_SHA:-unknown}"}
  ```
- В `.github/workflows/deploy.yml` перед запуском `docker compose up -d --build` экспортируется хеш коммита:
  ```bash
  export GIT_SHA="$(git rev-parse --short HEAD)"
  ```

### 2. Сверка версии в prod_check.sh
- В функцию `remote()` добавлено чтение метки контейнера:
  ```bash
  echo "container_sha=$(docker inspect -f '{{ index .Config.Labels "org.unagi.git_sha" }}' "$2" 2>/dev/null || true)"
  ```
- Логика проверки:
  - Если метка не равна `rev` на сервере: `ALERT "контейнер на старом коде: метка X, HEAD Y"` (код выхода 1);
  - Если метка пустая, `unknown` или `<no value>`: `WARN "контейнер: метка версии unknown/пустая (первый деплой после внедрения)"` (код выхода 0);
  - Если метка совпадает: `OK "контейнер: код X (HEAD X)"`.
- Добавлена поддержка переменной `PROD_CHECK_OUT` для детерминированного тестирования скрипта без SSH-подключения.

### 3. Откатный тег образа
- В `.github/workflows/deploy.yml` перед сборкой нового образа сохраняется тег предыдущей версии:
  ```bash
  docker tag unagi-reminder-bot-bot:latest unagi-reminder-bot-bot:rollback 2>/dev/null || true
  ```
- Команда `docker image prune -f` удаляет исключительно dangling (безымянные `<none>:<none>`) образы и не трогает образ с тегом `:rollback`.
- В `ops/aiqu/README.md` задокументирован процесс быстрого отката:
  ```bash
  docker tag unagi-reminder-bot-bot:rollback unagi-reminder-bot-bot:latest
  docker compose up -d
  git checkout <sha>
  ```

### 4. Проверка живости в CI (deploy.yml)
- Сразу после `docker compose up -d --build` выполняется блок `# --- liveness`:
  - Ожидание стабилизации (`sleep "${DEPLOY_WAIT_SECS:-20}"`);
  - Проверка через `docker inspect`: `State.Status == running` и `RestartCount == 0`;
  - При сбое (контейнер упал, цикличный рестарт, ошибка инспекции) выводятся последние 30 строк логов (`docker logs --tail 30 unagi-bot`) и деплой падает с кодом 1.

## Тестирование
- `tests/test_container_hardening.py`: проверка наличия метки `org.unagi.git_sha` в `docker-compose.yml`.
- `tests/test_dep2_deploy_verify.py`:
  - Проверка наличия и порядка `export GIT_SHA`, `docker tag ...:rollback` и `docker image prune -f` без флага `--all`.
  - Запуск блока `# --- liveness` во временной среде с фейковым `docker` в `PATH`:
    - Успех при `running` и 0 рестартов;
    - Падение при статусе `exited`, `restarting`, ненулевом `RestartCount` или сбое inspect;
    - Контроль вызова `docker logs --tail 30 unagi-bot`.
  - Проверка всех веток `prod_check.sh` (OK, ALERT, WARN unknown, WARN empty, WARN `<no value>`) и разбор скрипта без SSH и без внешних утилит.

## Что осталось неидеальным
1. **Ручной запуск отката:** При падении проверки живости CI останавливается с ошибкой, но не выполняет автоматический `docker tag ...:rollback ...:latest && docker compose up -d`. Откат остаётся действием оператора (через SSH).
2. **Связка с миграциями БД:** Хотя перед сборкой создаётся снимок БД (`backup_db.py`), быстрый откат образа контейнера на старый код не выполняет автоматическое восстановление БД, если применились несовместимые изменения схемы (в текущем спринте все миграции аддитивны).
3. **Фиксированный интервал проверки (20 с):** Контейнер проверяется на интервале до 20 секунд после запуска. Ошибки, возникающие позже (например, сбой при первом срабатывании планировщика через 30-45 минут или утечки памяти), ловятся только внешним мониторингом `prod_check.sh` и Telegram-алертами.
