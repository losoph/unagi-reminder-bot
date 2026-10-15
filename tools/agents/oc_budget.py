#!/usr/bin/env python3
"""Бюджет токенов opencode: предохранитель одного прогона и общий журнал расхода на спринт.

    oc_budget.py check                 # код 0 — можно запускать, 3 — бюджет спринта исчерпан
    oc_budget.py status                # расход и остаток
    oc_budget.py add <токены> <метка> [--note текст] [--reference]   # занести расход вручную; --reference — только справочно
    oc_budget.py guard --title <метка> # фильтр NDJSON opencode: stdin → stdout, при превышении выходит с кодом 3

Считается input + output + reasoning из событий step_finish (у opencode каждый шаг заново присылает весь контекст, поэтому
длинные прогоны дорогие). Лимиты — budget.json рядом, переопределяются OC_SPRINT, OC_SPRINT_TOKEN_LIMIT, OC_RUN_TOKEN_LIMIT.
Журнал — OC_LEDGER (по умолчанию ~/.config/agent-tools/oc-ledger.ndjson): строки {ts, sprint, title, tokens, counted}.
Записи с counted=false (справочные) и чужого спринта в остаток не входят.
"""

import json
import os
import signal
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def config():
    with open(os.path.join(HERE, "budget.json"), encoding="utf-8") as stream:
        data = json.load(stream)
    sprint = os.environ.get("OC_SPRINT") or data["sprint"]
    sprint_limit = int(os.environ.get("OC_SPRINT_TOKEN_LIMIT") or data["sprint_tokens"])
    run_limit = int(os.environ.get("OC_RUN_TOKEN_LIMIT") or data["run_tokens"])
    return sprint, sprint_limit, run_limit, int(data.get("min_remaining_to_start", 0))


def ledger_path():
    return os.environ.get("OC_LEDGER") or os.path.expanduser("~/.config/agent-tools/oc-ledger.ndjson")


def spent(sprint):
    total = 0
    try:
        with open(ledger_path(), encoding="utf-8") as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get("sprint") == sprint and row.get("counted", True):
                    total += int(row.get("tokens", 0))
    except FileNotFoundError:
        pass
    return total


def append(sprint, title, tokens, counted=True, note=""):
    path = ledger_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    row = {"ts": int(time.time()), "sprint": sprint, "title": title, "tokens": int(tokens), "counted": counted}
    if note:
        row["note"] = note
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as stream:
        stream.write(json.dumps(row, ensure_ascii=False) + "\n")


def step_tokens(event):
    if event.get("type") != "step_finish":
        return 0
    tokens = (event.get("part") or {}).get("tokens")
    if not isinstance(tokens, dict):
        return 0
    return sum(int(tokens.get(key) or 0) for key in ("input", "output", "reasoning"))


def cmd_status():
    sprint, sprint_limit, run_limit, _ = config()
    used = spent(sprint)
    print(f"sprint={sprint} used={used} limit={sprint_limit} remaining={max(sprint_limit - used, 0)} run_limit={run_limit}")
    return 0


def cmd_check():
    sprint, sprint_limit, run_limit, minimum = config()
    remaining = sprint_limit - spent(sprint)
    if remaining < max(minimum, 1):
        print(f"БЮДЖЕТ opencode исчерпан: спринт {sprint}, остаток {max(remaining, 0)} из {sprint_limit} токенов. "
              "Работайте без opencode или явно поднимите OC_SPRINT_TOKEN_LIMIT.", file=sys.stderr)
        return 3
    print(f"бюджет opencode: остаток {remaining} из {sprint_limit}, лимит прогона {run_limit}", file=sys.stderr)
    return 0


def cmd_guard(title):
    sprint, sprint_limit, run_limit, _ = config()
    before = spent(sprint)
    used = 0
    reason = ""
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    try:
        for line in sys.stdin:
            sys.stdout.write(line)
            sys.stdout.flush()
            try:
                event = json.loads(line)
            except ValueError:
                continue
            used += step_tokens(event)
            if used > run_limit:
                reason = f"лимит прогона {run_limit}"
            elif before + used > sprint_limit:
                reason = f"лимит спринта {sprint_limit}"
            if reason:
                stop = {"type": "budget_exceeded", "title": title, "run_tokens": used, "sprint_tokens": before + used, "reason": reason}
                sys.stdout.write(json.dumps(stop, ensure_ascii=False) + "\n")
                sys.stdout.flush()
                # SIGPIPE агента может быть перехвачен — останавливаем соседа по конвейеру явно (дети oc_run.sh).
                subprocess.run(["pkill", "-TERM", "-P", str(os.getppid()), "-f", "opencode run"], check=False)
                return 3
    except BrokenPipeError:
        pass
    finally:
        if used:
            append(sprint, title, used)
    return 0


def main(argv):
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    cmd, rest = argv[0], argv[1:]
    if cmd == "status":
        return cmd_status()
    if cmd == "check":
        return cmd_check()
    if cmd == "add" and len(rest) >= 2:
        sprint = config()[0]
        note = rest[rest.index("--note") + 1] if "--note" in rest and rest.index("--note") + 1 < len(rest) else ""
        append(sprint, rest[1], int(rest[0]), counted="--reference" not in rest, note=note)
        return cmd_status()
    if cmd == "guard" and "--title" in rest and rest.index("--title") + 1 < len(rest):
        return cmd_guard(rest[rest.index("--title") + 1])
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
