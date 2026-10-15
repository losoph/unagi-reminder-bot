#!/usr/bin/env bash
# Выбирает модель по политике или подключённому внешнему маршрутизатору.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
POLICY="$SCRIPT_DIR/routing.json"
. "$SCRIPT_DIR/agent_env.sh"
agent_env_load OPENROUTER_API_KEY TYPESAFE_API_KEY
PYTHON="${PYTHON:-python3}"
[[ $# -ge 2 ]] || { echo "использование: route.sh <agy|opencode|claude> <task-kind> [описание] [--dry-run]" >&2; exit 2; }
agent="$1" kind="$2"; shift 2
dry_run=0
args=()
for arg in "$@"; do
  case "$arg" in
    --dry-run|--dry-run=true|--dry-run=1) dry_run=1 ;;
    --*) echo "неизвестный аргумент: $arg" >&2; exit 2 ;;
    *) args+=("$arg") ;;
  esac
done
description="${args[*]:-}"
case "$agent" in agy|opencode|claude) ;; *) echo "неподдерживаемый агент: $agent" >&2; exit 2;; esac
task="$("$PYTHON" - "$agent" "$kind" "$description" <<'PY'
import json, sys
print(json.dumps({"agent": sys.argv[1], "task_kind": sys.argv[2], "title": sys.argv[3], "description": sys.argv[3]}, ensure_ascii=False))
PY
)"
ROUTER_STATUS="static"
fallback_reason=""
meta='{}'
parsed=""
jev_enabled=0
jev_status=3
if [ "${AGENT_ROUTER:-}" != "static" ] && { [ -n "${OPENROUTER_API_KEY:-}" ] || [ -n "${TYPESAFE_API_KEY:-}" ]; }; then
  jev_enabled=1
fi
if [ "${AGENT_ROUTER:-}" != "static" ] && [ -z "${AGENT_ROUTER_CMD:-}" ]; then
  jev_status=0
  response=""
  jev_output="$(mktemp "${TMPDIR:-/tmp}/jev-route-output.XXXXXX")" || exit 1
  jev_error="$(mktemp "${TMPDIR:-/tmp}/jev-route-error.XXXXXX")" || { rm -f "$jev_output"; exit 1; }
  cleanup_jev_files() { rm -f "$jev_output" "$jev_error"; }
  trap cleanup_jev_files EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  printf '%s\n' "$task" | AGENT_ROUTING_POLICY="$POLICY" "$PYTHON" "$SCRIPT_DIR/routers/jev_decide.py" >"$jev_output" 2>"$jev_error" || jev_status=$?
  response="$(cat "$jev_output")"
  rm -f "$jev_output"
  if [ "$jev_status" = 0 ]; then
    parsed="$(printf '%s\n' "$response" | "$PYTHON" -c 'import json,sys; print(json.dumps({"model":sys.argv[1],"effort":sys.argv[2],"reason":"jev"}, ensure_ascii=False))' "$(sed -n '1s/^model=\([^ ]*\) effort=\([^ ]*\).*/\1/p' <<<"$response")" "$(sed -n '1s/^model=[^ ]* effort=\([^ ]*\).*/\1/p' <<<"$response")")"
    meta="$(sed -n 's/^meta=//p' <<<"$response" | tail -n 1)"
    ROUTER_STATUS="jev"
  fi
  if [ "$jev_status" -ne 0 ] && [ "$jev_status" -ne 3 ]; then fallback_reason="jev_exit_${jev_status}:$(tr '\n' ' ' <"$jev_error" | sed -E 's/(OPENROUTER_API_KEY|TYPESAFE_API_KEY|Bearer)[^ ]*/[REDACTED]/g' | cut -c1-240)"; fi
  if [ "$jev_status" = 3 ]; then fallback_reason="нет ключа OpenRouter"; fi
elif [ "${AGENT_ROUTER:-}" != "static" ] && [ -z "${AGENT_ROUTER_CMD:-}" ] && [ "$jev_enabled" = 0 ] && [ -z "${OPENROUTER_API_KEY:-}${TYPESAFE_API_KEY:-}" ]; then
  ROUTER_STATUS="fallback"
  fallback_reason="нет ключа OpenRouter"
  jev_status=3
fi
if [ -z "$parsed" ] && [ -n "${AGENT_ROUTER_CMD:-}" ]; then
  candidates="$("$PYTHON" -c 'import json,sys; print(json.dumps(json.load(open(sys.argv[1]))["candidates"]))' "$POLICY")"
  if command -v timeout >/dev/null 2>&1; then
    timeout_bin="$(command -v timeout)"
  elif command -v perl >/dev/null 2>&1; then
    timeout_bin="perl"
  else
    timeout_bin=""
  fi
  if [ -n "$timeout_bin" ]; then
    if [ "$timeout_bin" = "perl" ]; then
      if response="$(printf '%s\n' "$task" | AGENT_TASK="$task" AGENT_ROUTE_CANDIDATES="$candidates" AGENT_ROUTER_CMD="$AGENT_ROUTER_CMD" perl -e '$SIG{ALRM}=sub{exit 124}; alarm 10; exec "bash", "-c", $ENV{AGENT_ROUTER_CMD}' 2>/dev/null)"; then
        router_ok=1
      else
        router_ok=0
      fi
    elif response="$(printf '%s\n' "$task" | AGENT_TASK="$task" AGENT_ROUTE_CANDIDATES="$candidates" "$timeout_bin" 10 bash -c "$AGENT_ROUTER_CMD")"; then
      router_ok=1
    else
      router_ok=0
    fi
    if [ "$router_ok" = 1 ]; then
      if parsed="$(printf '%s' "$response" | AGENT_TASK="$task" AGENT_CANDIDATES="$candidates" "$PYTHON" -c '
import json, os, sys
d=json.load(sys.stdin)
agent=json.loads(os.environ["AGENT_TASK"])["agent"]
choices=json.loads(os.environ["AGENT_CANDIDATES"])[agent]
assert d["model"] in choices
assert d["effort"] in ("low", "medium", "high")
assert isinstance(d["reason"], str) and d["reason"].strip()
print(json.dumps(d, ensure_ascii=False))
' 2>/dev/null)"; then
        ROUTER_STATUS="external"
      else
        parsed=""
      fi
    fi
  fi
  if [ -z "$parsed" ] && [ -n "$fallback_reason" ]; then fallback_reason="$fallback_reason; внешний маршрутизатор отклонён/недоступен"; fi
  if [ -z "$parsed" ]; then
    ROUTER_STATUS="fallback"
  fi
  if [ "$jev_enabled" = 1 ] && [ "$ROUTER_STATUS" = "fallback" ]; then
    fallback_reason="${fallback_reason:+$fallback_reason; }Jev завершился ошибкой"
  fi
fi
if [ -z "$parsed" ]; then
  parsed="$("$PYTHON" - "$POLICY" "$agent" "$kind" <<'PY'
import json, sys
policy=json.load(open(sys.argv[1], encoding="utf-8"))
agent, kind=sys.argv[2:]
try:
    answer=policy["tasks"][kind][agent]
    assert answer["model"] in policy["candidates"][agent]
    assert answer["effort"] in ("low", "medium", "high")
except (KeyError, AssertionError) as exc:
    raise SystemExit(f"ошибка политики маршрутизации: {exc}")
print(json.dumps(answer, ensure_ascii=False))
PY
)"
  [[ "$ROUTER_STATUS" == "static" ]] && ROUTER_STATUS="fallback"
fi
model="$("$PYTHON" -c 'import json,sys; print(json.load(sys.stdin)["model"])' <<<"$parsed")"
effort="$("$PYTHON" -c 'import json,sys; print(json.load(sys.stdin)["effort"])' <<<"$parsed")"
reason="$("$PYTHON" -c 'import json,sys; print(json.load(sys.stdin)["reason"])' <<<"$parsed")"
printf 'model=%s effort=%s reason=%s\n' "$model" "$effort" "$reason"
if [ -n "$meta" ] && [ "$meta" != '{}' ]; then printf 'meta=%s\n' "$meta"; fi
# Причину отката печатаем всегда: иначе в --dry-run не видно, почему Jev не выбрал модель.
if [ -n "$fallback_reason" ]; then printf 'fallback=%s source=%s\n' "$fallback_reason" "$ROUTER_STATUS"; fi
if [ "$dry_run" = 1 ]; then exit 0; fi
log="${AGENT_ROUTE_LOG:-.agents/routes.ndjson}"
mkdir -p "$(dirname "$log")"
umask 077
"$PYTHON" - "$log" "$task" "$model" "$effort" "$reason" "$ROUTER_STATUS" "$fallback_reason" "$meta" <<'PY'
import json, sys, time
import os
log, task, model, effort, reason, source, fallback_reason, meta=sys.argv[1:]
row=json.loads(task)
row.update(decision={"model": model, "effort": effort, "reason": reason}, model=model, effort=effort, reason=reason, source=source, fallback_reason=fallback_reason or None, meta=json.loads(meta), ts=int(time.time()))
fd=os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
with os.fdopen(fd, "a", encoding="utf-8") as f:
    f.write(json.dumps(row, ensure_ascii=False)+"\n")
PY
