#!/usr/bin/env python3
"""Бюджет токенов исполнителей (opencode, dsh): предохранитель прогона и общий журнал расхода на спринт.

    oc_budget.py [--tool opencode|dsh] check      # код 0 — можно запускать, 3 — бюджет спринта исчерпан
    oc_budget.py [--tool …] status [--all]        # расход и остаток (--all — по всем инструментам)
    oc_budget.py [--tool …] add <токены> <метка> [--note текст] [--reference] [--cache-read N]   # занести расход вручную; --reference — справочно
    oc_budget.py [--tool …] guard --title <метка> # фильтр NDJSON: stdin → stdout, при превышении выходит с кодом 3
    oc_budget.py snapshot                         # (opencode) токены за сегодня по `opencode stats`; OC_STATS_CMD меняет команду
    oc_budget.py settle --title <метка> --before <N> [--guard-file <файл>]   # (opencode) доучесть по разнице snapshot

Что считается:
- opencode — input + output + reasoning из `step_finish`; поток не содержит последний шаг прогона, поэтому после прогона расход
  сверяется с `opencode stats` (источник истины), у opencode кеш промптов не работает — длинные прогоны очень дорогие;
- dsh (DeepSeek Harness) — fresh input + output из `status/step_end` (каждый шаг присылает usage); токены из кеша префикса
  (`cacheReadTokens`) стоят дешевле и пишутся в журнал отдельно (`cache_read`), в лимит не входят.
Лимиты — budget.json рядом (по инструментам), переопределяются OC_SPRINT / OC_SPRINT_TOKEN_LIMIT / OC_RUN_TOKEN_LIMIT (opencode) и
DSH_SPRINT_TOKEN_LIMIT / DSH_RUN_TOKEN_LIMIT (dsh). Журнал — OC_LEDGER (~/.config/agent-tools/oc-ledger.ndjson): {ts, sprint, tool,
title, tokens, cache_read, counted}; записи без `tool` — opencode. Чужой спринт и counted=false в остаток не входят.
"""

import json
import os
import signal
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = ("opencode", "dsh")
ENV_PREFIX = {"opencode": "OC", "dsh": "DSH"}
KILL_PATTERN = {"opencode": "opencode run", "dsh": "bin/dsh"}


def config(tool):
    with open(os.path.join(HERE, "budget.json"), encoding="utf-8") as stream:
        data = json.load(stream)
    section = data["tools"][tool]
    prefix = ENV_PREFIX[tool]
    sprint = os.environ.get("OC_SPRINT") or data["sprint"]
    sprint_limit = int(os.environ.get(f"{prefix}_SPRINT_TOKEN_LIMIT") or section["sprint_tokens"])
    run_limit = int(os.environ.get(f"{prefix}_RUN_TOKEN_LIMIT") or section["run_tokens"])
    return sprint, sprint_limit, run_limit, int(section.get("min_remaining_to_start", 0))


def ledger_path():
    return os.environ.get("OC_LEDGER") or os.path.expanduser("~/.config/agent-tools/oc-ledger.ndjson")


def spent(sprint, tool):
    total = 0
    try:
        with open(ledger_path(), encoding="utf-8") as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get("sprint") == sprint and row.get("tool", "opencode") == tool and row.get("counted", True):
                    total += int(row.get("tokens", 0))
    except FileNotFoundError:
        pass
    return total


def append(sprint, tool, title, tokens, counted=True, note="", cache_read=0):
    path = ledger_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    row = {"ts": int(time.time()), "sprint": sprint, "tool": tool, "title": title, "tokens": int(tokens), "counted": counted}
    if cache_read:
        row["cache_read"] = int(cache_read)
    if note:
        row["note"] = note
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as stream:
        stream.write(json.dumps(row, ensure_ascii=False) + "\n")


def step_tokens(event, tool):
    """(платные токены шага, токены из кеша) для события потока данного инструмента."""
    if tool == "dsh":
        if event.get("type") != "status" or event.get("phase") != "step_end":
            return 0, 0
        usage = event.get("usage") or {}
        return int(usage.get("inputTokens") or 0) + int(usage.get("outputTokens") or 0), int(usage.get("cacheReadTokens") or 0)
    if event.get("type") != "step_finish":
        return 0, 0
    tokens = (event.get("part") or {}).get("tokens")
    if not isinstance(tokens, dict):
        return 0, 0
    return sum(int(tokens.get(key) or 0) for key in ("input", "output", "reasoning")), 0


def stats_total():
    """Токены opencode за сегодня (input + output + reasoning) или None, если статистика недоступна."""
    command = os.environ.get("OC_STATS_CMD") or "opencode stats --standalone --days 0 --json"
    try:
        out = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=60, check=False).stdout
        tokens = json.loads(out)["tokens"]
        return sum(int(tokens.get(key) or 0) for key in ("input", "output", "reasoning"))
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
        return None


def cmd_snapshot():
    total = stats_total()
    if total is None:
        return 1
    print(total)
    return 0


def cmd_settle(title, before, guard_file):
    after = stats_total()
    sprint, sprint_limit, _, _ = config("opencode")
    guarded = 0
    if guard_file:
        try:
            with open(guard_file, encoding="utf-8") as stream:
                guarded = int(stream.read().strip() or 0)
        except (OSError, ValueError):
            guarded = 0
    extra = 0
    if after is not None and before is not None and after >= before:  # отрицательная разница — сменились сутки
        extra = max((after - before) - guarded, 0)
        if extra:
            append(sprint, "opencode", f"{title} (по opencode stats)", extra)
    used = spent(sprint, "opencode")
    print(f"бюджет opencode: прогон ≈ {guarded + extra} токенов, спринт {used}/{sprint_limit}, остаток {max(sprint_limit - used, 0)}", file=sys.stderr)
    return 0


def status_line(tool):
    sprint, sprint_limit, run_limit, _ = config(tool)
    used = spent(sprint, tool)
    return f"tool={tool} sprint={sprint} used={used} limit={sprint_limit} remaining={max(sprint_limit - used, 0)} run_limit={run_limit}"


def cmd_status(tool, show_all):
    for name in (TOOLS if show_all else (tool,)):
        print(status_line(name))
    return 0


def cmd_check(tool):
    sprint, sprint_limit, run_limit, minimum = config(tool)
    remaining = sprint_limit - spent(sprint, tool)
    if remaining < max(minimum, 1):
        print(f"БЮДЖЕТ {tool} исчерпан: спринт {sprint}, остаток {max(remaining, 0)} из {sprint_limit} токенов. "
              f"Работайте с другим исполнителем (tools/agents/agents.sh pick) или явно поднимите {ENV_PREFIX[tool]}_SPRINT_TOKEN_LIMIT.", file=sys.stderr)
        return 3
    print(f"бюджет {tool}: остаток {remaining} из {sprint_limit}, лимит прогона {run_limit}", file=sys.stderr)
    return 0


def cmd_guard(tool, title):
    sprint, sprint_limit, run_limit, _ = config(tool)
    before = spent(sprint, tool)
    used = cached = 0
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
            paid, from_cache = step_tokens(event, tool)
            used += paid
            cached += from_cache
            if used > run_limit:
                reason = f"лимит прогона {run_limit}"
            elif before + used > sprint_limit:
                reason = f"лимит спринта {sprint_limit}"
            if reason:
                stop = {"type": "budget_exceeded", "tool": tool, "title": title, "run_tokens": used, "sprint_tokens": before + used, "reason": reason}
                sys.stdout.write(json.dumps(stop, ensure_ascii=False) + "\n")
                sys.stdout.flush()
                # SIGPIPE агент может перехватить — останавливаем соседа по конвейеру явно (дети run-скрипта).
                pattern = os.environ.get("BUDGET_KILL_PATTERN") or KILL_PATTERN[tool]
                subprocess.run(["pkill", "-TERM", "-P", str(os.getppid()), "-f", pattern], check=False)
                return 3
    except BrokenPipeError:
        pass
    finally:
        if used or cached:
            append(sprint, tool, title, used, cache_read=cached)
        count_file = os.environ.get("OC_GUARD_COUNT_FILE")
        if count_file:
            with open(count_file, "w", encoding="utf-8") as stream:
                stream.write(str(used))
    return 0


def option(rest, name):
    return rest[rest.index(name) + 1] if name in rest and rest.index(name) + 1 < len(rest) else None


def main(argv):
    tool = os.environ.get("BUDGET_TOOL") or "opencode"
    if argv and argv[0] == "--tool":
        if len(argv) < 2 or argv[1] not in TOOLS:
            print(__doc__, file=sys.stderr)
            return 2
        tool, argv = argv[1], argv[2:]
    if tool not in TOOLS or not argv:
        print(__doc__, file=sys.stderr)
        return 2
    cmd, rest = argv[0], argv[1:]
    if cmd == "status":
        return cmd_status(tool, "--all" in rest)
    if cmd == "check":
        return cmd_check(tool)
    if cmd == "add" and len(rest) >= 2:
        append(config(tool)[0], tool, rest[1], int(rest[0]), counted="--reference" not in rest, note=option(rest, "--note") or "",
               cache_read=int(option(rest, "--cache-read") or 0))
        return cmd_status(tool, False)
    if cmd == "snapshot":
        return cmd_snapshot()
    if cmd == "settle" and option(rest, "--title") and "--before" in rest:
        before = option(rest, "--before")
        return cmd_settle(option(rest, "--title"), int(before) if before and before.isdigit() else None, option(rest, "--guard-file"))
    if cmd == "guard" and option(rest, "--title"):
        return cmd_guard(tool, option(rest, "--title"))
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
