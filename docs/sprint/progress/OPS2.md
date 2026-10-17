# OPS2 — CI без пропусков, пиннинг checkout, подготовка пользователя деплоя

## Контекст и проблемы
1. **Пропуск тестов в CI:** Базовый образ `python:3.12-slim`, используемый для регрессионного прогона тестов в контейнере, не содержит утилит `git` и `jq`. Из-за этого модули `tests/test_deploy_sync.py` и `tests/test_orchestra.py` (а также `tests/test_dep2_deploy_verify.py` в части проверок с Docker) пропускались через `@unittest.skipUnless(shutil.which(...))` и фактически не проверялись в CI при пушах и PR.
2. **Безопасность supply chain:** Шаг `actions/checkout@v7` в `.github/workflows/deploy.yml` использовал плавающий тег версии, что создавало уязвимость к подмене кода экшена в цепочке поставок (аналогично ранее зафиксированному по SHA экшену `appleboy/ssh-action`).
3. **Привилегии при деплое:** Деплой через GitHub Actions выполнялся по SSH под суперпользователем `root`. Требовалось подготовить инфраструктуру для перехода на выделенного непривилегированного пользователя `deploy` (член группы docker, владелец каталога проекта, без пароля, без sudo), исследовать шаги деплоя, требующие `root` (установка cron в `/etc/cron.d`), и разработать регламент переключения и отката.

## Решение

### 1. Тесты хостового окружения в CI
- В `.github/workflows/deploy.yml` в job `test` добавлен отдельный шаг перед сборкой Docker-образа:
  ```yaml
  # Тесты, требующие утилит хоста (git, docker): в образе python:3.12-slim их нет, поэтому в образе они пропускаются (skipUnless).
  - name: Run host environment regression tests (git/docker)
    run: python3 -m unittest tests/test_deploy_sync.py tests/test_orchestra.py tests/test_dep2_deploy_verify.py -v
  ```
- Шаг выполняется напрямую на виртуальной машине runner (`ubuntu-latest`), где гарантированно присутствуют `git`, `docker` и `jq`.
- Проверена автономность модулей `test_deploy_sync.py`, `test_orchestra.py`, `test_dep2_deploy_verify.py`: они используют исключительно стандартную библиотеку Python (`stdlib`) и внутренние скрипты репозитория (`tools/agents/orchestra.py`, `tools/agents/progress_index.py`), не требуя зависимостей проекта из `requirements.txt`.
- Анализ других модулей с `skipUnless(shutil.which(...))`:
  - `tests/test_export_db.py`: содержит `skipUnless(shutil.which("age") and shutil.which("age-keygen"))` для одного сквозного теста реальных бинарников. Утилита `age` не предустановлена ни в slim-образе, ни на runner `ubuntu-latest`. Все остальные юнит-тесты модуля используют встроенный фейковый эмулятор и штатно выполняются внутри контейнера в шаге `Run regression tests`. Модуль оставлен в контейнере.
  - `tests/test_feed_ranking.py`: использует флаг переменной окружения `FEED_PERF_TEST=1` (ручной бенчмарк).
- Любое падение тестов на runner немедленно проваливает job `test` и блокирует CI/деплой.

### 2. Закрепление actions/checkout по SHA коммита
- В `.github/workflows/deploy.yml` плавающий тег `v7` заменён на точный SHA коммита с комментарием версии:
  ```yaml
  - name: Check out repository
    uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
  ```

### 3. Скрипт подготовки пользователя деплоя (`setup_deploy_user.sh`)
- Создан идемпотентный исполняемый скрипт [`ops/aiqu/setup_deploy_user.sh`](../../ops/aiqu/setup_deploy_user.sh):
  - Принимает путь к файлу с публичным SSH-ключом и опциональный каталог проекта:
    `./ops/aiqu/setup_deploy_user.sh <файл_с_публичным_ключом> [каталог_проекта]`
  - Проверяет запуск от `root` (`id -u == 0`);
  - Создаёт пользователя `deploy` с оболочкой `/bin/bash` (если ещё не существует);
  - Блокирует пароль (`passwd -l deploy` или `usermod -p '*'`), разрешая аутентификацию исключительно по SSH-ключам;
  - Обеспечивает наличие группы `docker` и добавляет в неё пользователя `deploy` (`usermod -aG docker deploy`);
  - Гарантирует отсутствие привилегий `sudo`: удаляет из групп `sudo`, `wheel`, `admin` и удаляет любые файлы из `/etc/sudoers.d/`;
  - Настраивает `~deploy/.ssh/authorized_keys` (права `0700` на директорию, `0600` на файл): считывает публичные ключи из переданного файла и добавляет их идемпотентно (`grep -Fxq`), исключая дублирование строк при повторных запусках;
  - Создаёт и передаёт во владение пользователю `deploy` каталог проекта (`chown -R deploy:deploy "$PROJECT_DIR"`), по умолчанию `/home/deploy/projects/unagi-reminder-bot`.

### 4. Раздел в `ops/aiqu/README.md` и аудит root-операций
- В `ops/aiqu/README.md` добавлен раздел «Пользователь деплоя (deploy): подготовка и миграция (OPS2)».
- Задокументированы действия при переключении:
  - Смена секрета `USERNAME` в GitHub с `root` на `deploy`;
  - Проверка соответствия секрета `SSH_KEY` публичному ключу в `~deploy/.ssh/authorized_keys`;
  - Автоматическое разрешение `~/projects/unagi-reminder-bot` в `/home/deploy/projects/unagi-reminder-bot`.
- Выявлен шаг деплоя, требующий прав `root`:
  - Строка 104 в `.github/workflows/deploy.yml`:
    `for f in ops/aiqu/cron.d/*; do [ -f "$f" ] && install -m 0644 -o root -g root "$f" "/etc/cron.d/$(basename "$f")" || true; done`
    (каталог `/etc/cron.d` принадлежит root:root 0755, непривилегированный пользователь не имеет туда записи).
- Предложены варианты решения:
  1. *Вариант А (рекомендуемый):* cron-файлы из репозитория ставит независимый root-скрипт/systemd-таймер на хосте, деплой не касается системных каталогов.
  2. *Вариант Б:* точечное правило в `/etc/sudoers.d/deploy-cron` без пароля исключительно на `/usr/bin/install -m 0644 -o root -g root ... /etc/cron.d/*`.
- Описана процедура быстрого отката: возврат `USERNAME=root` в секретах GitHub восстанавливает работу деплоя от root без изменений в кодовой базе.
- Пользователь в `deploy.yml` **НЕ** переключался — выполнена только подготовка окружения и скрипта.

## Тестирование
- `tests/test_setup_deploy_user.py`:
  - `bash -n ops/aiqu/setup_deploy_user.sh` — проверка синтаксиса bash;
  - Проверка бита исполнения `chmod +x`;
  - Текстовые проверки структуры скрипта (id -u, useradd, passwd -l, usermod -aG docker, no sudo, authorized_keys 0600/0700, chown проекта);
  - Валидация входных аргументов (отсутствие файла, несуществующий путь, пустой ключ);
  - Поведенческий тест идемпотентности в изолированной среде: эмуляция системных утилит через fake-binaries в `PATH`, повторный запуск скрипта подтверждает добавление ключа ровно 1 раз без дублирования.
- `tests/test_deploy_sync.py`, `tests/test_orchestra.py`, `tests/test_dep2_deploy_verify.py`: успешно выполнены и проверены на совместимость с runner.
- Общий прогон линтера и тестов:
  - `~/.venvs/unagi/bin/ruff check .` — без замечаний.
  - `~/.venvs/unagi/bin/python -m unittest discover -s tests` — 100% зелёное (694 теста, 0 ошибок, 0 падений).
- Проверка индексатора журнала решений:
  - `~/.venvs/unagi/bin/python tools/agents/progress_index.py` — файл `OPS2.md` корректно включён в индекс.

## Компромиссы и точки для следующих треков
- **Переключение секретов:** Переключение `USERNAME` в секретах GitHub и добавление sudoers/таймера cron отложено до выполнения `setup_deploy_user.sh` владельцем непосредственно на сервере aiqu.
