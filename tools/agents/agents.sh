#!/usr/bin/env bash
# Единая точка входа набора агентов: статус и бюджеты, лестница исполнителей, запуск с выбором модели через Jev, монитор, короткая сверка.
# Для постоянного использования из любых сессий:  <репозиторий>/tools/agents/agents.sh help
#
#   agents.sh status                                   # бюджеты opencode/dsh, ключи (без значений), dsh и веб-интерфейс, сводка Jev
#   agents.sh pick <kind>                              # кого запускать: лестница dsh → agy → (opencode, если разрешён видом задачи)
#   agents.sh run <agent> <kind> <worktree> <brief.md> <log.ndjson> <title> [--review]
#                                                       # route.sh (Jev) → dsh_run.sh | oc_run.sh; opencode — только с решением Jev
#   agents.sh check <agent|auto> <title> "<вопрос>" <файл-контекст> [log.ndjson]
#                                                       # короткая сверка: вопрос + ≤24 КБ контекста, без инструментов, ответ ≤200 слов
#   agents.sh watch <dsh|opencode|agy> <log>           # живой монитор (ошибки, коммиты, красные тесты, итог прогона, расход токенов)
#   agents.sh route <agent> <kind> "<описание>" [--dry-run]   # только выбор модели (Jev или статическая политика)
#   agents.sh report                                   # что решил Jev, откаты, стоимость решений и качество выдачи агентов
#   agents.sh rate <agent> <model> <title> <good|partial|bad> [заметка]   # оценка выдачи (журнал .agents/quality.ndjson)
# kind: executor-complex | executor-ux | review | adversarial-review | docs | merge-audit | short-check
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
. "$SCRIPT_DIR/agent_env.sh"
agent_env_load OPENROUTER_API_KEY TYPESAFE_API_KEY
. "$SCRIPT_DIR/dsh_bin.sh"
PY="${PYTHON:-python3}"

usage() { sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; }

status() {
  "$PY" "$SCRIPT_DIR/oc_budget.py" status --all
  local jev=unset ds=нет oc=нет web=down code dsh_path
  { [ -n "${OPENROUTER_API_KEY:-}${TYPESAFE_API_KEY:-}" ]; } && jev=set
  grep -qs "DEEPSEEK_API_KEY" "$HOME/.dsh/.credentials.yaml" && ds=да
  grep -qs "Tokenn" "$HOME/.config/opencode/opencode.json" && oc=да
  dsh_path="$(dsh_find 2>/dev/null || echo "не найден")"
  code="$(curl -s -m 3 -o /dev/null -w '%{http_code}' "http://127.0.0.1:${DSH_WEB_PORT:-3080}/" 2>/dev/null)"
  case "$code" in 200|401|403) web="up($code)" ;; esac
  echo "keys: jev=$jev deepseek-credentials=$ds opencode-provider=$oc"
  echo "dsh: $dsh_path web=$web"
  "$PY" "$SCRIPT_DIR/jev_report.py" --brief
}

# Результат route.sh: model=… effort=… ; строка fallback=… означает, что Jev не выбрал модель.
routed_field() {  # $1 — вывод route.sh, $2 — имя поля первой строки (без sed \b: в BSD sed его нет)
  local first word; first="$(head -n 1 <<<"$1")"
  for word in $first; do case "$word" in "$2="*) printf '%s\n' "${word#"$2="}"; return 0 ;; esac; done
  return 1
}

run_agent() {
  [ $# -ge 6 ] || { usage >&2; return 2; }
  local agent="$1" kind="$2" worktree="$3" brief="$4" log="$5" title="$6"; shift 6
  case "$agent" in dsh|opencode) ;; agy) echo "agy запускается через agy_supervise.sh (нужен маркер готовности); модель — route.sh agy $kind" >&2; return 2 ;; *) echo "неизвестный агент: $agent" >&2; return 2 ;; esac
  local routed model effort via_jev=1
  routed="$("$SCRIPT_DIR/route.sh" "$agent" "$kind" "$title" 2>&1)" || { printf '%s\n' "$routed" >&2; return 2; }
  printf '%s\n' "$routed" >&2
  # Решение Jev узнаём по строке meta= с router=jev (route.sh печатает её только для Jev); статика и откат её не имеют
  grep -q '^meta=.*"router":"jev"' <<<"$routed" || via_jev=0
  model="$(routed_field "$routed" model)"; effort="$(routed_field "$routed" effort)"
  if [ "$agent" = opencode ]; then
    if [ "$via_jev" = 0 ] && [ "${OC_JEV_REQUIRED:-on}" != off ]; then
      echo "⛔ opencode запускается только с решением Jev (нужен OPENROUTER_API_KEY в ~/.config/agent-tools/env или окружении); откат на статическую политику запрещён." >&2
      return 4
    fi
    # Уровень мышления Tokenn выставлен владельцем в панели (Min) и не регулируется (16.10): Jev выбирает только модель.
    # OC_EFFORT_FROM_JEV=on вернёт передачу эффорта вариантом модели (#low|#medium|#high).
    local effort_args=()
    [ "${OC_EFFORT_FROM_JEV:-off}" != on ] || effort_args=(--effort "$effort")
    exec "$SCRIPT_DIR/oc_run.sh" "$worktree" "$model" "$brief" "$log" "$title" ${effort_args[@]+"${effort_args[@]}"} "$@"
  fi
  # dsh: Jev выбирает модель (V4.1 Flash | V4 Pro) и эффорт off|low|high|max — те же значения принимает dsh_run.sh
  exec "$SCRIPT_DIR/dsh_run.sh" "$worktree" "$brief" "$log" "$title" --model "$model" --effort "$effort" "$@"
}

check_agent() {
  [ $# -ge 4 ] || { usage >&2; return 2; }
  local agent="$1" title="$2" question="$3" context="$4" log="${5:-.agents/checks.ndjson}"
  [ -r "$context" ] || { echo "контекст не читается: $context" >&2; return 2; }
  if [ "$agent" = auto ]; then
    agent="$("$PY" "$SCRIPT_DIR/executor_policy.py" short-check | sed -n 's/^executor=//p')"
    [ -n "$agent" ] && [ "$agent" != none ] || { echo "нет доступного исполнителя для короткой сверки" >&2; return 1; }
    [ "$agent" != agy ] || { echo "для короткой сверки выбран agy: используйте agy_watch.sh run" >&2; return 2; }
  fi
  local work; work="$(mktemp -d "${TMPDIR:-/tmp}/agents-check.XXXXXX")"; trap 'rm -rf "$work"' RETURN
  {
    echo "ЭТО КОРОТКАЯ СВЕРКА. Не используй инструменты, не читай файлы, не запускай команды, ничего не меняй: весь нужный контекст ниже."
    echo "Ответь по-русски, не длиннее 200 слов: только подтверждаемые контекстом дефекты в формате «файл:строка — суть — сценарий воспроизведения»,"
    echo "затем одной строкой вердикт: OK или ЕСТЬ ЗАМЕЧАНИЯ. Если контекста не хватает — скажи, чего именно, не гадай."
    echo; echo "ВОПРОС: $question"; echo; echo "КОНТЕКСТ (обрезан до ${OC_CHECK_MAX_BYTES:-24000} байт):"
    head -c "${OC_CHECK_MAX_BYTES:-24000}" "$context"
  } > "$work/brief.md"
  mkdir -p "$(dirname "$log")"
  # не exec: нужен trap для очистки временной папки
  "$0" run "$agent" short-check "${AGENTS_CHECK_WORKTREE:-$PWD}" "$work/brief.md" "$log" "check-$title" --review
}

watch_agent() {
  [ $# -eq 2 ] || { usage >&2; return 2; }
  local agent="$1" log="$2"
  case "$agent" in
    dsh) touch "$log"; tail -n +1 -F "$log" 2>/dev/null | PERIOD="${PERIOD:-300}" "$PY" -u "$SCRIPT_DIR/dsh_digest.py" ;;
    opencode) touch "$log"; tail -n +1 -F "$log" 2>/dev/null | PERIOD="${PERIOD:-300}" "$PY" -u "$SCRIPT_DIR/oc_digest.py" ;;
    agy) AGY_LOG_DIR="$(dirname "$log")" "$SCRIPT_DIR/agy_watch.sh" digest "${PERIOD:-300}" ;;
    *) echo "неизвестный агент: $agent" >&2; return 2 ;;
  esac
}

cmd="${1:-help}"; [ $# -eq 0 ] || shift
case "$cmd" in
  status) status ;;
  pick) "$PY" "$SCRIPT_DIR/executor_policy.py" "$@" ;;
  route) "$SCRIPT_DIR/route.sh" "$@" ;;
  run) run_agent "$@" ;;
  check) check_agent "$@" ;;
  watch) watch_agent "$@" ;;
  report) "$PY" "$SCRIPT_DIR/jev_report.py" "$@"; "$PY" "$SCRIPT_DIR/quality.py" report ;;
  rate) "$PY" "$SCRIPT_DIR/quality.py" rate "$@" ;;
  help|-h|--help) usage ;;
  *) usage >&2; exit 2 ;;
esac
