#!/usr/bin/env bash
# Выбирает модель по политике или подключённому внешнему маршрутизатору.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
POLICY="$SCRIPT_DIR/routing.json"
[[ $# -ge 2 ]] || { echo "использование: route.sh <agy|opencode> <task-kind> [описание]" >&2; exit 2; }
agent="$1" kind="$2"; shift 2
description="${*:-}"
case "$agent" in agy|opencode) ;; *) echo "неподдерживаемый агент: $agent" >&2; exit 2;; esac
task="$(python3 - "$agent" "$kind" "$description" <<'PY'
import json, sys
print(json.dumps({"agent": sys.argv[1], "task_kind": sys.argv[2], "description": sys.argv[3]}, ensure_ascii=False))
PY
)"
ROUTER_STATUS="fallback"
parsed=""
if [ -n "${AGENT_ROUTER_CMD:-}" ]; then
  candidates="$(python3 -c 'import json,sys; print(json.dumps(json.load(open(sys.argv[1]))["candidates"]))' "$POLICY")"
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
      if parsed="$(printf '%s' "$response" | AGENT_TASK="$task" AGENT_CANDIDATES="$candidates" python3 -c '
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
fi
if [ -z "$parsed" ]; then
  parsed="$(python3 - "$POLICY" "$agent" "$kind" <<'PY'
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
fi
model="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["model"])' <<<"$parsed")"
effort="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["effort"])' <<<"$parsed")"
reason="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["reason"])' <<<"$parsed")"
printf 'model=%s effort=%s reason=%s\n' "$model" "$effort" "$reason"
log="${AGENT_ROUTE_LOG:-.agents/routes.ndjson}"
mkdir -p "$(dirname "$log")"
python3 - "$log" "$task" "$model" "$effort" "$reason" "$ROUTER_STATUS" <<'PY'
import json, sys, time
log, task, model, effort, reason, source=sys.argv[1:]
row=json.loads(task)
row.update(model=model, effort=effort, reason=reason, source=source, timestamp=int(time.time()))
with open(log, "a", encoding="utf-8") as f:
    f.write(json.dumps(row, ensure_ascii=False)+"\n")
PY
