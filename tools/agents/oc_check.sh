#!/usr/bin/env bash
# Короткая сверка с opencode: один вопрос + небольшой контекст, без чтения файлов и инструментов. Экономит токены.
#
#   oc_check.sh <title> "<вопрос>" <файл-контекст> [модель]
#   git -C <worktree> diff HEAD~1 -- feed.py > /tmp/ctx.diff && oc_check.sh T24c "Есть ли гонка в process_feed_autodeliveries?" /tmp/ctx.diff
#
# Контекст обрезается до OC_CHECK_MAX_BYTES (24000). Прогон ограничен бюджетом (oc_budget.py): лимит одного прогона —
# OC_RUN_TOKEN_LIMIT (по умолчанию 40000 из budget.json), остаток спринта проверяется до запуска. Модель по умолчанию — из route.sh (review).
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
[ $# -ge 3 ] || { echo "использование: oc_check.sh <title> \"<вопрос>\" <файл-контекст> [модель]" >&2; exit 2; }
TITLE="$1" QUESTION="$2" CONTEXT="$3"
[ -r "$CONTEXT" ] || { echo "контекст не читается: $CONTEXT" >&2; exit 2; }
MODEL="${4:-}"
if [ -z "$MODEL" ]; then
  MODEL="$(AGENT_ROUTE_LOG="${AGENT_ROUTE_LOG:-.agents/routes.ndjson}" "$SCRIPT_DIR/route.sh" opencode review "short check: $TITLE" | sed -n '1s/^model=\([^ ]*\) .*/\1/p')"
fi
WORK="$(mktemp -d "${TMPDIR:-/tmp}/oc-check.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
MAX="${OC_CHECK_MAX_BYTES:-24000}"
{
  echo "ЭТО КОРОТКАЯ СВЕРКА. Не используй инструменты, не читай файлы, не запускай команды, ничего не меняй: весь нужный контекст ниже."
  echo "Ответь по-русски, не длиннее 200 слов: только подтверждаемые контекстом дефекты в формате «файл:строка — суть — сценарий воспроизведения»,"
  echo "затем одной строкой вердикт: OK или ЕСТЬ ЗАМЕЧАНИЯ. Если контекста не хватает для вывода — скажи, чего именно, не гадай."
  echo
  echo "ВОПРОС: $QUESTION"
  echo
  echo "КОНТЕКСТ (обрезан до $MAX байт):"
  head -c "$MAX" "$CONTEXT"
} > "$WORK/brief.md"
mkdir -p "$(dirname "${OC_CHECK_LOG:-.agents/checks.ndjson}")"
exec "$SCRIPT_DIR/oc_run.sh" "${OC_CHECK_WORKTREE:-$PWD}" "$MODEL" "$WORK/brief.md" "${OC_CHECK_LOG:-.agents/checks.ndjson}" "check-$TITLE" --review
