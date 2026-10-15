#!/usr/bin/env bash
# Запускает одно задание opencode, одновременно показывая понятную ленту и сохраняя NDJSON.
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ "$#" -lt 5 ] || [ "$#" -gt 6 ]; then
  echo "использование: oc_run.sh <worktree> <model> <brief.md> <log.ndjson> <title> [--review]" >&2
  exit 2
fi
WORKTREE="$1" MODEL="$2" BRIEF="$3" LOG="$4" TITLE="$5" MODE="${6:-}"
if [ -n "$MODE" ] && [ "$MODE" != "--review" ]; then echo "неизвестный режим: $MODE" >&2; exit 2; fi
[[ "$WORKTREE" = /* ]] || WORKTREE="$PWD/$WORKTREE"
[[ "$BRIEF" = /* ]] || BRIEF="$PWD/$BRIEF"
[[ "$LOG" = /* ]] || LOG="$PWD/$LOG"
[ -d "$WORKTREE" ] || { echo "worktree не найден: $WORKTREE" >&2; exit 2; }
[ -r "$BRIEF" ] || { echo "бриф не читается: $BRIEF" >&2; exit 2; }
mkdir -p "$(dirname "$LOG")"
# Режим экономии токенов: нет остатка бюджета спринта — не запускаем (OC_BUDGET=off отключает проверку осознанно).
if [ "${OC_BUDGET:-on}" != "off" ]; then python3 "$SCRIPT_DIR/oc_budget.py" check || exit 3; fi
cd "$WORKTREE" || exit 1

# Текст команды фиксированным ASCII: он одинаково безопасно проходит аргументы CLI и терминальные кодировки.
MESSAGE="Read the task brief, complete it, run the relevant checks, and commit the changes locally."
if [ "$MODE" = "--review" ]; then
  MESSAGE="Read the task brief and review the current changes. Do not modify any files or create commits."
fi

# --standalone не использует фоновый сервис opencode: сервис может не видеть ключ провайдера текущего shell и отвечать 401.
# Предохранитель — прямой потомок этого скрипта (не подоболочка), чтобы остановить соседа `opencode run` по родителю.
GUARD=(python3 -u "$SCRIPT_DIR/oc_budget.py" guard --title "$TITLE")
[ "${OC_BUDGET:-on}" != "off" ] || GUARD=(cat)
set +e
opencode run --standalone --auto --thinking --format json -m "$MODEL" --title "$TITLE" -f "$BRIEF" "$MESSAGE" \
  | "${GUARD[@]}" \
  | tee -a "$LOG" | python3 -u "$SCRIPT_DIR/oc_pretty.py"
codes=("${PIPESTATUS[@]}")
set -e
code="${codes[0]}"
# Предохранитель оборвал поток (код 3): opencode получает SIGPIPE, фиксируем причину, а не «ошибку модели».
[ "${codes[1]:-0}" = 3 ] && { code=3; echo "⛔ бюджет токенов opencode исчерпан — прогон остановлен" >&2; }
python3 - "$LOG" "$TITLE" "$code" <<'PY'
import json, sys
with open(sys.argv[1], "a", encoding="utf-8") as log:
    log.write(json.dumps({"type": "run_end", "title": sys.argv[2], "code": int(sys.argv[3])}, ensure_ascii=False) + "\n")
PY
exit "$code"
