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
#   agents.sh orchestra [validate]                     # зафиксированная конфигурация оркестра (tools/agents/orchestra.json): роли, лестницы, лимиты, правила
#   agents.sh gate <PR|A..B> [вопрос]                  # предслиянная сверка (A..B — локальный диапазон интеграционной ветки): факты среды (orchestra.json) + дифф PR → короткая сверка (agy → dsh → opencode)
#   agents.sh launch <agent> <worktree> <brief.md> <метка> [kind] [--exec]
#                                                       # проверки перед запуском (бриф закоммичен, метка не в истории) и готовые команды для вкладки и монитора
# kind: executor-complex | executor-ux | review | adversarial-review | docs | merge-audit | short-check | strategy (ручной: Argon + Claude)
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
. "$SCRIPT_DIR/agent_env.sh"
agent_env_load OPENROUTER_API_KEY TYPESAFE_API_KEY
. "$SCRIPT_DIR/dsh_bin.sh"
PY="${PYTHON:-python3}"

usage() { sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; }

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
  fi
  local work; work="$(mktemp -d "${TMPDIR:-/tmp}/agents-check.XXXXXX")"; trap 'rm -rf "$work"' RETURN
  {
    echo "ЭТО КОРОТКАЯ СВЕРКА. Не используй инструменты, не читай файлы, не запускай команды, ничего не меняй: весь нужный контекст ниже."
    echo "Ответь по-русски, не длиннее 200 слов: только подтверждаемые контекстом дефекты в формате «файл:строка (или функция/раздел, если номеров строк в контексте нет) — суть — сценарий воспроизведения»,"
    echo "затем одной строкой вердикт: OK или ЕСТЬ ЗАМЕЧАНИЯ. Если контекста не хватает — скажи, чего именно, не гадай."
    echo; echo "ВОПРОС: $question"; echo; echo "КОНТЕКСТ (обрезан до ${OC_CHECK_MAX_BYTES:-24000} байт):"
    head -c "${OC_CHECK_MAX_BYTES:-24000}" "$context"
  } > "$work/brief.md"
  mkdir -p "$(dirname "$log")"
  if [ "$agent" = agy ]; then
    # agy — бесплатный и первый в лестнице: сверка идёт через agy_watch.sh run (ответ печатает `result`); журнал — во временной папке
    local model="${AGY_MODEL:-$("$PY" "$SCRIPT_DIR/orchestra.py" get models.agy.default 2>/dev/null || echo gemini-3.8-flash-medium)}"
    local proxy="${AGY_PROXY:-$("$PY" "$SCRIPT_DIR/orchestra.py" get models.agy.proxy 2>/dev/null || echo http://127.0.0.1:8095)}"
    mkdir -p "$work/agy"
    ( cd "${AGENTS_CHECK_WORKTREE:-$PWD}" && AGY_LOG_DIR="$work/agy" AGY_MODEL="$model" AGY_PROXY="$proxy" "$SCRIPT_DIR/agy_watch.sh" run "$work/brief.md" )
    return $?
  fi
  # не exec: нужен trap для очистки временной папки
  # opencode: текст сверки идёт в сообщении (--inline): gpt-6.1-sol не получает содержимое вложения; dsh флаг не нужен
  local extra=(); [ "$agent" != opencode ] || extra=(--inline)
  "$0" run "$agent" short-check "${AGENTS_CHECK_WORKTREE:-$PWD}" "$work/brief.md" "$log" "check-$title" --review ${extra[@]+"${extra[@]}"}
}

watch_agent() {
  [ $# -eq 2 ] || { usage >&2; return 2; }
  local agent="$1" log="$2"
  # период дайджеста монитора — из orchestra.json (limits.monitor_digest_sec), PERIOD из окружения важнее
  PERIOD="${PERIOD:-$("$PY" "$SCRIPT_DIR/orchestra.py" get limits.monitor_digest_sec 2>/dev/null || echo 300)}"
  case "$agent" in
    dsh) touch "$log"; tail -n +1 -F "$log" 2>/dev/null | PERIOD="$PERIOD" "$PY" -u "$SCRIPT_DIR/dsh_digest.py" ;;
    opencode) touch "$log"; tail -n +1 -F "$log" 2>/dev/null | PERIOD="$PERIOD" "$PY" -u "$SCRIPT_DIR/oc_digest.py" ;;
    agy) AGY_LOG_DIR="$(dirname "$log")" "$SCRIPT_DIR/agy_watch.sh" digest "$PERIOD" ;;
    *) echo "неизвестный агент: $agent" >&2; return 2 ;;
  esac
}

# Предслиянная сверка PR: факты среды из orchestra.json + дифф (≤ лимита) → короткая сверка независимым исполнителем.
gate_pr() {
  [ $# -ge 1 ] || { usage >&2; return 2; }
  local pr="$1" question="${2:-Найди дефекты, из-за которых слияние этого PR сломает деплой, прод или тесты в среде из «ФАКТЫ СРЕДЫ»: несовместимые значения, зависимость от инструментов, которых нет в CI-образе, пропущенные места использования.}"
  # 18.10: на больших PR (#28, #33) дифф обрезался по лимиту, и проверяющий принимал обрезку за «файла нет в PR». Теперь он знает,
  # что дифф может быть неполным, и обязан дочитать файлы из рабочего дерева, прежде чем заявлять о пропуске.
  question="$question Дифф ниже может быть обрезан по лимиту: прежде чем утверждать, что файла или места использования нет, прочитай его в рабочем дереве (git show <ветка>:<путь>); обрезка — не дефект."
  local work ctx title; work="$(mktemp -d "${TMPDIR:-/tmp}/agents-gate.XXXXXX")"; ctx="$work/context.txt"; trap 'rm -rf "$work"' RETURN
  { echo "ФАКТЫ СРЕДЫ:"; "$PY" -c 'import json,sys; [print("- "+f) for f in json.load(open(sys.argv[1])).get("facts", [])]' "$("$PY" "$SCRIPT_DIR/orchestra.py" path)"
    echo
    if [[ "$pr" == *..* ]]; then
      # Локальная интеграционная ветка (PR пачками, 18.10): диапазон коммитов; брифы и журналы решений не входят в дифф.
      title="range-${pr//[^A-Za-z0-9]/_}"
      echo "ДИАПАЗОН $pr ($(git log --oneline "$pr" | wc -l | tr -d ' ') коммитов):"; git log --oneline "$pr"; echo
      echo "ФАЙЛЫ:"; git diff --stat "$pr" -- . ':(exclude)docs/sprint/agy-brief*' ':(exclude)docs/sprint/progress*'; echo
      echo "ДИФФ:"; git diff "$pr" -- . ':(exclude)docs/sprint/agy-brief*' ':(exclude)docs/sprint/progress*'
    else
      command -v gh >/dev/null 2>&1 || { echo "gate: нужен gh" >&2; return 2; }
      title="$pr"
      echo "PR #$pr: $(gh pr view "$pr" --json title --jq .title)"; echo
      echo "ФАЙЛЫ:"; gh pr diff "$pr" --name-only; echo
      echo "ДИФФ:"; gh pr diff "$pr"
    fi; } > "$ctx" 2>/dev/null
  grep -q "^ДИФФ:" "$ctx" && [ "$(sed -n '/^ДИФФ:/,$p' "$ctx" | wc -l)" -gt 1 ] || { echo "gate: не удалось получить дифф $pr" >&2; return 1; }
  "$0" check auto "gate-$title" "$question" "$ctx" "${GATE_LOG:-.agents/gate.ndjson}"
}

# Проверки перед запуском трека и готовые команды для вкладки терминала и монитора (правила и лимиты — orchestra.json).
launch_agent() {
  [ $# -ge 4 ] || { usage >&2; return 2; }
  local agent="$1" worktree="$2" brief="$3" marker="$4" kind="executor-complex" doexec=0 arg
  shift 4
  for arg in "$@"; do case "$arg" in --exec) doexec=1 ;; *) kind="$arg" ;; esac; done
  case "$agent" in dsh|agy) ;; *) echo "launch: агент dsh или agy (opencode — agents.sh check, Argon — вручную через браузер)" >&2; return 2 ;; esac
  [[ "$worktree" = /* ]] || worktree="$PWD/$worktree"
  git -C "$worktree" rev-parse --show-toplevel >/dev/null 2>&1 || { echo "⛔ $worktree — не git-репозиторий/worktree" >&2; return 3; }
  git -C "$worktree" ls-files --error-unmatch -- "$brief" >/dev/null 2>&1 \
    || { echo "⛔ бриф $brief не закоммичен в $worktree: супервизор agy чистит неотслеживаемые файлы — закоммитьте бриф до запуска" >&2; return 3; }
  git -C "$worktree" diff --quiet HEAD -- "$brief" 2>/dev/null \
    || { echo "⛔ бриф $brief изменён после коммита — закоммитьте правки брифа" >&2; return 3; }
  local brief_path="$brief"
  [[ "$brief_path" = /* ]] || brief_path="$worktree/$brief"
  if [ -f "$brief_path" ]; then
    "$PY" "$SCRIPT_DIR/progress_index.py" --check-brief "$brief_path"
  fi
  local title="${marker#(}"; title="${title%)}"
  local depth hit; depth="$("$PY" "$SCRIPT_DIR/orchestra.py" get limits.marker_history_depth 2>/dev/null || echo 40)"
  hit="$(git -C "$worktree" log --oneline -"$depth" | grep -F -- "($title)" | head -n 1)"
  if [ -n "$hit" ]; then
    echo "⛔ метка ($title) уже есть в истории: $hit" >&2
    echo "   Супервизор сочтёт задачу выполненной и не запустит агента. Если это коммит с брифом — перепишите его сообщение без метки (git commit --amend)." >&2
    return 3
  fi
  local digest tabs; digest="$("$PY" "$SCRIPT_DIR/orchestra.py" get limits.monitor_digest_sec 2>/dev/null || echo 240)"
  tabs="$("$PY" "$SCRIPT_DIR/orchestra.py" get limits.terminal_tabs_max 2>/dev/null || echo 6)"
  local routed model effort; routed="$("$SCRIPT_DIR/route.sh" "$agent" "$kind" "$title" 2>&1)" || { printf '%s\n' "$routed" >&2; return 2; }
  model="$(routed_field "$routed" model)"; effort="$(routed_field "$routed" effort)"
  local term mon
  if [ "$agent" = agy ]; then
    lsof -nP -iTCP:8095 -sTCP:LISTEN >/dev/null 2>&1 || echo "⚠ прокси agy на :8095 не слушает (функция agy из ~/.zshrc поднимет его сама)" >&2
    local proxy attempts; proxy="$("$PY" "$SCRIPT_DIR/orchestra.py" get models.agy.proxy 2>/dev/null || echo http://127.0.0.1:8095)"
    attempts="$("$PY" "$SCRIPT_DIR/orchestra.py" get limits.supervise_attempts 2>/dev/null || echo 6)"
    term="cd $worktree && AGY_LOG_DIR=.agy AGY_MODEL=$model AGY_PROXY=$proxy tools/agents/agy_supervise.sh $brief '($title)' $attempts"
    mon="cd $worktree && AGY_LOG_DIR=.agy tools/agents/agy_watch.sh digest $digest"
  else
    term="cd $worktree && mkdir -p .agents && tools/agents/agents.sh run dsh $kind $worktree $brief .agents/$title.ndjson $title"
    mon="cd $worktree && tools/agents/agents.sh watch dsh .agents/$title.ndjson"
  fi
  echo "agent=$agent model=$model effort=$effort kind=$kind"
  echo "terminal: $term"
  echo "monitor : $mon"
  echo "(вкладок терминала не больше $tabs; монитор живёт 30 минут — перевзвести)"
  [ "$doexec" = 1 ] || return 0
  exec bash -c "$term"
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
  orchestra) if [ "${1:-}" = validate ]; then "$PY" "$SCRIPT_DIR/orchestra.py" validate; else "$PY" "$SCRIPT_DIR/orchestra.py" show; fi ;;
  launch) launch_agent "$@" ;;
  gate) gate_pr "$@" ;;
  help|-h|--help) usage ;;
  *) usage >&2; exit 2 ;;
esac
