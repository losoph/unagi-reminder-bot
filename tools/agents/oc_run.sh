#!/usr/bin/env bash
# Запускает одно задание opencode, одновременно показывая понятную ленту и сохраняя NDJSON.
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ "$#" -lt 5 ]; then
  echo "использование: oc_run.sh <worktree> <model> <brief.md> <log.ndjson> <title> [--review] [--inline] [--effort low|medium|high]" >&2
  exit 2
fi
WORKTREE="$1" MODEL="$2" BRIEF="$3" LOG="$4" TITLE="$5"; shift 5
MODE="" EFFORT="" INLINE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --review) MODE="--review"; shift ;;
    --inline) INLINE=1; shift ;;
    --effort) EFFORT="${2:?нужен уровень}"; shift 2 ;;
    *) echo "неизвестный аргумент: $1" >&2; exit 2 ;;
  esac
done
case "$EFFORT" in ""|low|medium|high) ;; *) echo "неверный эффорт opencode: $EFFORT (low|medium|high)" >&2; exit 2 ;; esac
[[ "$WORKTREE" = /* ]] || WORKTREE="$PWD/$WORKTREE"
[[ "$BRIEF" = /* ]] || BRIEF="$PWD/$BRIEF"
[[ "$LOG" = /* ]] || LOG="$PWD/$LOG"
[ -d "$WORKTREE" ] || { echo "worktree не найден: $WORKTREE" >&2; exit 2; }
[ -r "$BRIEF" ] || { echo "бриф не читается: $BRIEF" >&2; exit 2; }
mkdir -p "$(dirname "$LOG")"
# Эффорт — вариант модели (`provider/model#variant`); без определённого варианта суффикс молча ничего не делает, поэтому отказываем.
if [ -n "$EFFORT" ]; then
  python3 "$SCRIPT_DIR/oc_variants.py" has "$MODEL" "$EFFORT" || { echo "у ${MODEL} нет варианта \"${EFFORT}\" в конфиге opencode: tools/agents/oc_variants.py apply" >&2; exit 5; }
  MODEL="$MODEL#$EFFORT"
fi
# Режим экономии токенов: нет остатка бюджета спринта — не запускаем (OC_BUDGET=off отключает проверку осознанно).
if [ "${OC_BUDGET:-on}" != "off" ]; then python3 "$SCRIPT_DIR/oc_budget.py" check || exit 3; fi
cd "$WORKTREE" || exit 1
GUARD_COUNT="$(mktemp "${TMPDIR:-/tmp}/oc-guard.XXXXXX")"; trap 'rm -f "$GUARD_COUNT"' EXIT
BEFORE=""; [ "${OC_BUDGET:-on}" = "off" ] || BEFORE="$(python3 "$SCRIPT_DIR/oc_budget.py" snapshot 2>/dev/null || true)"

# Текст команды фиксированным ASCII: он одинаково безопасно проходит аргументы CLI и терминальные кодировки.
MESSAGE="Read the task brief, complete it, run the relevant checks, and commit the changes locally."
if [ "$MODE" = "--review" ]; then
  MESSAGE="Read the task brief and review the current changes. Do not modify any files or create commits."
fi
# --inline: текст брифа идёт прямо в сообщение, без вложения `-f`. Нужен для коротких сверок без инструментов: gpt-6.1-sol
# содержимого вложения не получает (видит только имя brief.md), а бриф сверки запрещает читать файлы — модель отвечала «нет diff».
OC_ARGS=(run --standalone --auto --thinking --format json -m "$MODEL" --title "$TITLE")
if [ "$INLINE" = 1 ]; then
  if [ "$MODE" = "--review" ]; then
    MESSAGE="Review the task below. Do not modify any files or create commits."
  else
    MESSAGE="Complete the task below, run the relevant checks, and commit the changes locally."
  fi
  MESSAGE="$MESSAGE"$'\n\n'"$(cat "$BRIEF")"
else
  OC_ARGS+=(-f "$BRIEF")
fi

# --standalone не использует фоновый сервис opencode: сервис может не видеть ключ провайдера текущего shell и отвечать 401.
# Предохранитель — прямой потомок этого скрипта (не подоболочка), чтобы остановить соседа `opencode run` по родителю.
GUARD=(env OC_GUARD_COUNT_FILE="$GUARD_COUNT" python3 -u "$SCRIPT_DIR/oc_budget.py" guard --title "$TITLE")
[ "${OC_BUDGET:-on}" != "off" ] || GUARD=(cat)
set +e
# Предохранитель по времени: зависший шлюз (прогон без единого события) не должен держать наблюдателя бесконечно. alarm переживает exec.
perl -e 'alarm shift; exec @ARGV' "${OC_RUN_TIMEOUT:-900}" opencode "${OC_ARGS[@]}" "$MESSAGE" \
  | "${GUARD[@]}" \
  | tee -a "$LOG" | python3 -u "$SCRIPT_DIR/oc_pretty.py"
codes=("${PIPESTATUS[@]}")
set -e
code="${codes[0]}"
# Предохранитель оборвал поток (код 3): opencode получает SIGPIPE, фиксируем причину, а не «ошибку модели».
[ "${codes[1]:-0}" = 3 ] && { code=3; echo "⛔ бюджет токенов opencode исчерпан — прогон остановлен" >&2; }
[ "$code" = 142 ] && echo "⏱ opencode не уложился в ${OC_RUN_TIMEOUT:-900} с (OC_RUN_TIMEOUT) — прогон прерван" >&2
# Поток не содержит последний шаг прогона: доучитываем расход по `opencode stats` (источник истины).
[ "${OC_BUDGET:-on}" = "off" ] || python3 "$SCRIPT_DIR/oc_budget.py" settle --title "$TITLE" --before "$BEFORE" --guard-file "$GUARD_COUNT" || true
python3 - "$LOG" "$TITLE" "$code" <<'PY'
import json, sys
with open(sys.argv[1], "a", encoding="utf-8") as log:
    log.write(json.dumps({"type": "run_end", "title": sys.argv[2], "code": int(sys.argv[3])}, ensure_ascii=False) + "\n")
PY
exit "$code"
