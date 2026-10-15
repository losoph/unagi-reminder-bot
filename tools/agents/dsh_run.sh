#!/usr/bin/env bash
# Запускает одно задание DeepSeek Harness (dsh, профиль headless): живая лента в терминал + NDJSON в журнал + бюджет токенов.
#
#   dsh_run.sh <worktree> <brief.md> <log.ndjson> <title> [--review] [--model deepseek-flash|deepseek-v4-pro] [--effort off|low|high|max]
#
# Модель и глубина рассуждений задаются одноразовым патчем профиля (--patch), без правки ~/.dsh. Без --model/--effort — настройки профиля
# (deepseek-flash, рассуждение high). Для выбора модели через Jev используйте `agents.sh run dsh …` (route.sh → этот скрипт).
# --review добавляет к заданию запрет на правки; технической изоляции нет — ревью запускайте в отдельном detached worktree.
# Бюджет: oc_budget.py --tool dsh (budget.json): нет остатка — код 3; при превышении лимита прогона — поток обрывается, dsh останавливается.
# Код выхода: код dsh; 2 — неверный вызов; 3 — бюджет; 4 — dsh не найден.
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
. "$SCRIPT_DIR/dsh_bin.sh"
if [ "$#" -lt 4 ]; then
  echo "использование: dsh_run.sh <worktree> <brief.md> <log.ndjson> <title> [--review] [--model M] [--effort off|low|high|max]" >&2
  exit 2
fi
WORKTREE="$1" BRIEF="$2" LOG="$3" TITLE="$4"; shift 4
REVIEW=0 MODEL="" EFFORT=""
while [ $# -gt 0 ]; do
  case "$1" in
    --review) REVIEW=1; shift ;;
    --model) MODEL="${2:?нужна модель}"; shift 2 ;;
    --effort) EFFORT="${2:?нужен уровень}"; shift 2 ;;
    *) echo "неизвестный аргумент: $1" >&2; exit 2 ;;
  esac
done
case "$MODEL" in ""|deepseek-flash|deepseek-v4-pro) ;; *) echo "неизвестная модель dsh: $MODEL (deepseek-flash | deepseek-v4-pro)" >&2; exit 2 ;; esac
case "$EFFORT" in ""|off|low|high|max) ;; *) echo "неверный уровень рассуждений: $EFFORT (off|low|high|max)" >&2; exit 2 ;; esac
[[ "$WORKTREE" = /* ]] || WORKTREE="$PWD/$WORKTREE"
[[ "$BRIEF" = /* ]] || BRIEF="$PWD/$BRIEF"
[[ "$LOG" = /* ]] || LOG="$PWD/$LOG"
[ -d "$WORKTREE" ] || { echo "worktree не найден: $WORKTREE" >&2; exit 2; }
[ -r "$BRIEF" ] || { echo "бриф не читается: $BRIEF" >&2; exit 2; }
DSH="$(dsh_find)" || { echo "dsh не найден: задайте DSH_BIN или установите (npx dsh web)" >&2; exit 4; }
mkdir -p "$(dirname "$LOG")"
if [ "${DSH_BUDGET:-on}" != "off" ]; then python3 "$SCRIPT_DIR/oc_budget.py" --tool dsh check || exit 3; fi

WORK="$(mktemp -d "${TMPDIR:-/tmp}/dsh-run.XXXXXX")"; trap 'rm -rf "$WORK"' EXIT
PATCH=()
if [ -n "$MODEL" ] || [ -n "$EFFORT" ]; then
  : > "$WORK/overlay.yml"
  [ -z "$MODEL" ] || printf -- '- id: agent-default-model\n  config:\n    provider: deepseek-official\n    model: %s\n' "$MODEL" >> "$WORK/overlay.yml"
  [ -z "$EFFORT" ] || printf -- '- id: llm-deepseek\n  config:\n    reasoningEffort: "%s"\n' "$EFFORT" >> "$WORK/overlay.yml"
  PATCH=(--patch "$WORK/overlay.yml")  # флаг лаунчера: обязан стоять до --json
fi
{
  [ "$REVIEW" = 1 ] && printf '%s\n\n' "ТОЛЬКО ЧТЕНИЕ: не меняй файлы, не создавай коммиты и не запускай git push. Анализируй и отвечай."
  cat "$BRIEF"
} > "$WORK/task.md"

cd "$WORKTREE" || exit 1
# Предохранитель — прямой потомок этого скрипта (не подоболочка), чтобы остановить соседа `dsh` по родителю.
GUARD=(python3 -u "$SCRIPT_DIR/oc_budget.py" --tool dsh guard --title "$TITLE")
[ "${DSH_BUDGET:-on}" != "off" ] || GUARD=(cat)
set +e
"$DSH" --profile headless ${PATCH[@]+"${PATCH[@]}"} --json - < "$WORK/task.md" \
  | "${GUARD[@]}" \
  | tee -a "$LOG" | python3 -u "$SCRIPT_DIR/dsh_pretty.py"
codes=("${PIPESTATUS[@]}")
set -e
code="${codes[0]}"
[ "${codes[1]:-0}" = 3 ] && { code=3; echo "⛔ бюджет токенов dsh исчерпан — прогон остановлен" >&2; }
python3 - "$LOG" "$TITLE" "$code" "$MODEL" "$EFFORT" <<'PY'
import json, sys
with open(sys.argv[1], "a", encoding="utf-8") as log:
    log.write(json.dumps({"type": "run_end", "tool": "dsh", "title": sys.argv[2], "code": int(sys.argv[3]), "model": sys.argv[4] or "profile-default", "effort": sys.argv[5] or "profile-default"}, ensure_ascii=False) + "\n")
PY
exit "$code"
