#!/usr/bin/env bash
# Супервизор agy с ретраями до маркера готовности в git history.
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(git -C "$PWD" rev-parse --show-toplevel)"
cd "$PROJECT_ROOT"

TASK="${1:?нужен файл задачи}"
DONE_MARK="${2:?нужен маркер готовности, например (T13)}"
MAX="${3:-6}"
LOG_DIR="${AGY_LOG_DIR:-.agy}"; mkdir -p "$LOG_DIR"
SUP="$LOG_DIR/supervise.log"
say() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$SUP"; }

_size() { wc -c < "$LOG_DIR/session.ndjson" 2>/dev/null || echo 0; }
prev="$(_size)"; sleep 20
while [ "$(_size)" != "$prev" ]; do prev="$(_size)"; sleep 20; done

for attempt in $(seq 1 "$MAX"); do
  if git log --oneline -40 | grep -F -- "$DONE_MARK" >/dev/null; then
    say "готово: маркер '$DONE_MARK' найден в истории: $(git log --oneline -40 | grep -F -m1 -- "$DONE_MARK")"
    # 17.10: метка попала в сообщение коммита с брифом — агент не запускался, а супервизор рапортовал «готово». Показываем это явно.
    [ "$attempt" != 1 ] || say "⚠ метка найдена ДО первого запуска agy: если это коммит брифа или чужой коммит, агент не запускался — перепишите сообщение без метки (git commit --amend)"
    exit 0
  fi
  # Внимание: без AGY_KEEP_TREE супервизор сохраняет diff, затем git clean удаляет
  # все неотслеживаемые файлы, кроме каталога логов. Не храните там важные данные.
  if [ "$attempt" = 1 ] && [ "${AGY_KEEP_TREE:-0}" = 1 ]; then
    say "AGY_KEEP_TREE=1: незакоммиченные правки оставляю"
  elif ! git diff --quiet || [ -n "$(git ls-files --others --exclude-standard)" ]; then
    ts=$(date +%Y%m%d-%H%M%S); bak="$LOG_DIR/partial-$ts"; mkdir -p "$bak"
    git diff > "$bak/partial.diff" 2>/dev/null
    while IFS= read -r -d '' f; do cp --parents "$f" "$bak/" 2>/dev/null || true; done < <(git ls-files --others --exclude-standard -z)
    say "обрывки сохранены в $bak, дерево возвращаю к HEAD"
    git checkout -- . 2>/dev/null
    git clean -fdq -e "${LOG_DIR%/}" 2>/dev/null
  fi
  mode=cont
  [ "$attempt" = 1 ] && [ "${AGY_RESUME:-0}" != 1 ] && mode=run
  say "попытка $attempt/$MAX: agy $mode (модель ${AGY_MODEL:-gemini-3.8-flash-high})"
  "$SCRIPT_DIR/agy_watch.sh" "$mode" "$TASK" >/dev/null 2>&1
  st="$("$SCRIPT_DIR/agy_watch.sh" status 2>/dev/null | head -1)"
  say "итог попытки: $st"
  case "$st" in
    *SUCCESS*) : ;;
    *) say "ошибка прогона, пауза 30с"; sleep 30 ;;
  esac
done

git log --oneline -40 | grep -- "$DONE_MARK" >/dev/null \
  && { say "готово после ретраев"; exit 0; } \
  || { say "ИСЧЕРПАНЫ попытки, маркер '$DONE_MARK' не найден"; exit 1; }
