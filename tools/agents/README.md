# Монитор агентов

Набор переносимых утилит для запуска и наблюдения исполнителей agy и opencode из любого git-проекта. Коммиты локальные; push выполняет только владелец после проверки.

## Роли и worktree

- **Наблюдатель** задаёт задачу, следит за логами, проверяет каждый коммит и принимает решение о переходе к следующему этапу.
- **Исполнители** работают каждый в своём worktree, проверяют результат и коммитят.
- **Ревьюеры** получают отдельное detached-дерево и не меняют исходники.

Worktree по умолчанию размещаются в `~/dev-worktrees`, вне Google Drive и iCloud. Синхронизирующие облачные папки переименовывают конфликтующие файлы в `«(1)»` и могут ломать сокеты fsmonitor. Для нового дерева отключается `core.fsmonitor`.

```sh
tools/agents/worktree.sh new impl feature-branch main
tools/agents/worktree.sh review reviewer HEAD
tools/agents/worktree.sh rm reviewer
```

Путь можно переопределить через `AGENT_WORKTREES`. При запуске `rm` учитывайте незакоммиченные изменения: Git откажется удалять грязное дерево.

## Вкладки терминала

Пути и параметры в примерах ASCII; замените placeholders на пути без кириллицы.

```sh
AGY_LOG_DIR=.agy AGY_MODEL=gemini-3.8-flash-high AGY_PROXY="$HTTPS_PROXY" tools/agents/agy_supervise.sh brief.md '(TASK-1)' 6
tools/agents/agy_watch.sh follow
tools/agents/agy_watch.sh digest 600
tools/agents/oc_run.sh ../dev-worktrees/impl Tokenn/gpt-6.1-sol brief.md .agents/impl.ndjson impl
tools/agents/oc_run.sh ../dev-worktrees/reviewer Tokenn/gpt-6.1-sol review.md .agents/review.ndjson review --review
PERIOD=600 python3 tools/agents/oc_digest.py < .agents/impl.ndjson
tools/agents/route.sh opencode executor-complex 'Implement the requested feature'
tools/agents/usage.sh
```

`AGY_LOG_DIR` задаёт журнал agy, `AGY_MODEL` — модель, `AGY_PROXY` при наличии экспортируется в дочерний процесс как `HTTP_PROXY`/`HTTPS_PROXY` и варианты в нижнем регистре. В отдельных вкладках окружение наблюдателя может отсутствовать, поэтому задавайте прокси явно.

`oc_run.sh` принимает `<worktree> <model> <brief.md> <log.ndjson> <title> [--review]`. Режим `--review` просит только анализировать и запрещает изменения. Команда передаёт краткое ASCII-сообщение: оно не зависит от кодировки оболочки и аргументов CLI. `--standalone` не подключает фоновый сервис opencode, который может не видеть ключ провайдера текущей оболочки и отвечать 401. После процесса в NDJSON записывается `run_end`; `oc_digest.py` печатает `OC RUN END`.

## Мониторы и протокол

- `agy_watch.sh follow` выводит завершённые шаги в реальном времени, чтения файлов группируются.
- `agy_watch.sh digest [SEC]` выдаёт агрегат действий и сразу подсвечивает риски и ошибки.
- `oc_digest.py` читает поток JSON/NDJSON, немедленно показывает ошибки, коммиты и конец прогона; `PERIOD` задаёт интервал сводки.
- `agy_watch.sh steps`, `reasoning`, `result`, `usage`, `id`, `status` дают детализацию последнего прогона.

Процесс: **коммит исполнителя → проверка наблюдателем → перекрёстное ревью → отдельный fix-коммит**. Не переходите к следующей задаче до оценки диффа. Наблюдение имеет три независимых слоя: git показывает **что**, дистиллят — **как**, журнал решений `docs/sprint/progress.md` — **почему**. Записи решений фиксируют выбор и компромисс, не дублируя список файлов и тестов.

Шаблоны заданий: `templates/review.md` и `templates/fix.md`. Перед запуском замените placeholders `<...>` конкретными значениями, перечислите проверяемые сценарии, ограничения на файлы и точные команды проверок. Не включайте секреты в бриф.

## Маршрутизация моделей и затраты

`routing.json` задаёт перечень кандидатов и политику для видов `executor-complex`, `executor-ux`, `review`, `adversarial-review`, `docs`, `merge-audit`. `route.sh <agy|opencode> <task-kind> [description]` выводит `model=... effort=... reason=...` и добавляет JSON-событие в `$AGENT_ROUTE_LOG` (по умолчанию `.agents/routes.ndjson`). Внешний `AGENT_ROUTER_CMD` получает JSON задачи через stdin и JSON-ответ; в окружении доступен `AGENT_ROUTE_CANDIDATES`. Модель ответа обязана входить в allowlist из `routing.json`; при ошибке или таймауте в 10 секунд выбирается локальная политика.

`usage.sh` суммирует доступные токены agy (`result.usage`) и opencode (`step_finish`), используя `$AGY_LOG_DIR/session.ndjson`, `$OC_LOG` и журнал маршрутов. Совпадение выбора модели с затратами выполняется по `title`; задавайте одинаковый title в маршрутизации и запуске, иначе выбранная модель будет `-`. Поддержка полей usage зависит от версии CLI и схемы его событий.

## Подводные камни

- Для agy используется `zsh -ic`, чтобы загрузить алиасы/параметры из `~/.zshrc`; скрипт не читает и не сохраняет секреты сам.
- Вкладки терминала не наследуют переменные наблюдателя. Явно задавайте `AGY_PROXY`; без прокси Gemini может ответить `User location is not supported`.
- Избегайте кириллицы в путях worktree и логов: отдельные CLI и shell-расширения некорректно обрабатывают такие пути.
- Супервизор без `AGY_KEEP_TREE=1` сохраняет tracked diff и очищает дерево: `git clean` удаляет все неотслеживаемые файлы, кроме каталога логов. Не держите там данные; включение `AGY_KEEP_TREE=1` оставляет правки первой попытки.
- Логи могут содержать содержимое задач и ответов; выбирайте расположение и права доступа осознанно.

## Окружение исполнителей

Исполнителям запрещено ставить пакеты (`pip install` и т. п.): тесты и линтер — только из venv
проекта тех же версий, что в CI. 14.10 opencode без спроса поставил ~28 пакетов в системный
Python и проверял ruff другой версии — это правило в шаблонах `templates/*.md`. Команды, ждущие
stdin (`cat > file` без here-doc), вешают прогон агента на десятки минут — файлы только инструментом.

## Бомбы замедленного действия в тестах

`tools/agents/time_shift_tests.sh` прогоняет набор тестов со сдвинутыми часами (libfaketime:
+1d, +7d, +30d, +365d, -30d). Тест, падающий только при сдвиге, сравнивает жёсткую дату с
реальным «сейчас» и однажды сломает CI сам по себе. Запускать перед слиянием веток и перед
деплоем: `PYTHON=~/.venvs/unagi/bin/python tools/agents/time_shift_tests.sh`.
