#!/usr/bin/env bash
# Супервизор для agy: доводит задачу до конца без участия наблюдателя.
#
# Зачем: прогон падает на сетевых ошибках (retryable), обрываясь посреди правки
# и оставляя сломанное рабочее дерево. Тогда требуется человек: откатить
# обрывки и перезапустить. Этот скрипт делает и то, и другое сам.
#
#   ./scripts/agy_supervise.sh <task-file> <маркер-готовности> [попыток]
#
# <маркер-готовности> — подстрока в git log, по которой видно, что всё сделано
# (например "(T13)"). Пока её нет и попытки не исчерпаны — возобновляем.
set -uo pipefail
cd "$(dirname "$0")/.."

TASK="${1:?нужен файл задачи}"
DONE_MARK="${2:?нужен маркер готовности, например (T13)}"
MAX="${3:-6}"
LOG_DIR="${AGY_LOG_DIR:-.agy}"; mkdir -p "$LOG_DIR"
SUP="$LOG_DIR/supervise.log"

say() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$SUP"; }

# Активность определяем по РОСТУ лога, а не через pgrep: pgrep -f "stream-json"
# матчит зависшие процессы без читаемой cmdline, и цикл ожидания не кончается
# никогда (проверено — супервизор так залип и не запустил ни одной попытки).
_size() { wc -c < "$LOG_DIR/session.ndjson" 2>/dev/null || echo 0; }
prev="$(_size)"; sleep 20
while [ "$(_size)" != "$prev" ]; do prev="$(_size)"; sleep 20; done

for attempt in $(seq 1 "$MAX"); do
  if git log --oneline -40 | grep -q -- "$DONE_MARK"; then
    say "готово: маркер '$DONE_MARK' найден в истории"; exit 0
  fi

  # Обрывки прошлой попытки: незакоммиченное после краха доверия не заслуживает.
  if ! git diff --quiet || [ -n "$(git ls-files --others --exclude-standard)" ]; then
    ts=$(date +%Y%m%d-%H%M%S); bak="$LOG_DIR/partial-$ts"; mkdir -p "$bak"
    git diff > "$bak/partial.diff" 2>/dev/null
    for f in $(git ls-files --others --exclude-standard); do cp "$f" "$bak/" 2>/dev/null; done
    say "обрывки сохранены в $bak, дерево возвращаю к HEAD"
    git checkout -- . 2>/dev/null
    git clean -fdq -e .agy 2>/dev/null
  fi

  say "попытка $attempt/$MAX: возобновляю agy"
  ./scripts/agy_watch.sh cont "$TASK" >/dev/null 2>&1

  st="$(./scripts/agy_watch.sh status 2>/dev/null | head -1)"
  say "итог попытки: $st"
  case "$st" in
    *SUCCESS*) : ;;                    # прогон дошёл до конца — проверим маркер на следующем витке
    *) say "ошибка прогона, пауза 30с"; sleep 30 ;;
  esac
done

git log --oneline -40 | grep -q -- "$DONE_MARK" \
  && { say "готово после ретраев"; exit 0; } \
  || { say "ИСЧЕРПАНЫ попытки, маркер '$DONE_MARK' не найден"; exit 1; }
