#!/usr/bin/env bash
# Сводит затраты токенов по запускам и подмешивает выбор модели из routes.ndjson.
set -euo pipefail
agy_log="${AGY_LOG_DIR:-.agy}/session.ndjson"
oc_log="${OC_LOG:-}"
dsh_log="${DSH_LOG:-}"
routes="${AGENT_ROUTE_LOG:-.agents/routes.ndjson}"
decisions="${AGENT_DECISION_LOG:-.agents/decisions.ndjson}"
python3 - "$agy_log" "$oc_log" "$routes" "$decisions" "$dsh_log" <<'PY'
import json, os, sys
agy_path, oc_path, route_path, decision_path, dsh_path = sys.argv[1:]
routes = {}
jev_cost = 0.0
jev_seen = False
if os.path.isfile(route_path):
    with open(route_path, encoding="utf-8") as stream:
        for line in stream:
            try:
                item = json.loads(line)
            except ValueError:
                continue
            routes[(item.get("title", ""), item.get("agent", ""))] = item.get("model", "")
            if item.get("source") == "jev" or (item.get("meta") or {}).get("router") == "jev":
                jev_seen = True
                cost = (item.get("meta") or {}).get("cost")
                if isinstance(cost, (int, float)):
                    jev_cost += cost

def read(path):
    if not path or not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as stream:
        for line in stream:
            try:
                yield json.loads(line)
            except ValueError:
                continue

rows = []
for event in read(agy_path):
    if event.get("event") != "result":
        continue
    result = event.get("result") or {}
    usage = result.get("usage") or {}
    if not usage:
        continue
    rows.append({"agent": "agy", "title": result.get("title") or event.get("title") or "", "model": result.get("model") or "", "input_tokens": usage.get("input_tokens", 0), "output_tokens": usage.get("output_tokens", 0), "total_tokens": usage.get("total_tokens", 0), "cost": None})
for event in read(oc_path):
    if event.get("type") != "step_finish":
        continue
    part = event.get("part") or {}
    title = event.get("title") or event.get("session", {}).get("title") or ""
    tokens = part.get("tokens")
    if not isinstance(tokens, dict) and part.get("cost") is None:
        continue
    usage = tokens if isinstance(tokens, dict) else {}
    values = [v for v in usage.values() if isinstance(v, (int, float))]
    rows.append({"agent": "opencode", "title": title, "model": event.get("model") or part.get("model") or "", "input_tokens": usage.get("input", 0) or usage.get("inputTokens", 0), "output_tokens": usage.get("output", 0) or usage.get("outputTokens", 0), "total_tokens": usage.get("total", sum(values)), "cost": part.get("cost")})
run = {"input": 0, "output": 0, "cache_read": 0, "steps": 0}
for event in read(dsh_path):
    if event.get("type") == "status" and event.get("phase") == "step_end":
        usage = event.get("usage") or {}
        run["steps"] += 1
        run["input"] += usage.get("inputTokens", 0)
        run["output"] += usage.get("outputTokens", 0)
        run["cache_read"] += usage.get("cacheReadTokens", 0)
    elif event.get("type") == "run_end":
        rows.append({"agent": "dsh", "title": event.get("title") or "", "model": event.get("model") or "", "input_tokens": run["input"], "output_tokens": run["output"],
                     "total_tokens": run["input"] + run["output"], "cost": f"cache_read={run['cache_read']}"})
        run = {"input": 0, "output": 0, "cache_read": 0, "steps": 0}
for row in rows:
    selected = routes.get((row["title"], row["agent"]), routes.get((row["title"], ""), ""))
    print("agent={agent} title={title} selected_model={selected} actual_model={model} input={input_tokens} output={output_tokens} total={total_tokens} cost={cost}".format(selected=selected or "-", **row))
if jev_seen:
    print(f"jev_decisions cost={jev_cost:.8f}")
decided = human = 0
decision_cost = 0.0
for item in read(decision_path):
    decided += 1
    human += item.get("choice") == "ASK_HUMAN"
    cost = (item.get("meta") or {}).get("cost")
    if isinstance(cost, (int, float)):
        decision_cost += cost
if decided:
    print(f"decide count={decided} human={human} cost={decision_cost:.8f}")
PY
