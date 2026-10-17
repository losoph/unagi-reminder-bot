#!/usr/bin/env python3
"""Конфигурация оркестра агентов (tools/agents/orchestra.json): чтение, проверка, печать.

    orchestra.py show                 # роли, лестницы по видам задач, модели, лимиты, правила
    orchestra.py get <путь.через.точку>   # одно значение, например limits.monitor_digest_sec
    orchestra.py ladder <kind>        # порядок исполнителей для вида задачи, по одному в строке
    orchestra.py validate             # проверка согласованности; код 1, если найдены проблемы

Файл можно подменить переменной AGENT_ORCHESTRA (тесты, эксперименты). Нет файла или он битый — модули, которые его читают,
откатываются на встроенные значения, а `validate` сообщает причину.
"""

from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
KINDS = ("executor-complex", "executor-ux", "review", "adversarial-review", "docs", "merge-audit", "short-check")
EXTRA_KINDS = ("strategy",)  # виды без статической политики маршрутизации: решают вручную


def path() -> str:
    return os.environ.get("AGENT_ORCHESTRA") or os.path.join(HERE, "orchestra.json")


def load() -> dict:
    try:
        with open(path(), encoding="utf-8") as stream:
            data = json.load(stream)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def get(dotted: str, default=None):
    node = load()
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def ladder(kind: str) -> list[str]:
    value = get(f"ladder.{kind}")
    return [str(item) for item in value] if isinstance(value, list) else []


def manual_agents() -> set[str]:
    value = get("manual", [])
    return {str(item) for item in value} if isinstance(value, list) else set()


def validate() -> list[str]:
    problems: list[str] = []
    data = load()
    if not data:
        return [f"{path()}: не читается или пуст"]
    roles = data.get("roles") or {}
    if not isinstance(roles, dict) or not roles:
        problems.append("roles: нужен непустой объект")
    ladders = data.get("ladder") or {}
    for kind in KINDS + EXTRA_KINDS:
        steps = ladders.get(kind)
        if not isinstance(steps, list) or not steps:
            problems.append(f"ladder.{kind}: нужен непустой список")
            continue
        for agent in steps:
            if agent not in roles:
                problems.append(f"ladder.{kind}: агент «{agent}» не описан в roles")
        if len(set(steps)) != len(steps):
            problems.append(f"ladder.{kind}: повторы агентов")
    for kind in ladders:
        if kind not in KINDS + EXTRA_KINDS:
            problems.append(f"ladder.{kind}: неизвестный вид задачи")
    for agent in data.get("manual", []):
        if agent not in roles:
            problems.append(f"manual: «{agent}» не описан в roles")
    tiers = data.get("tiers") or {}
    cost = {}  # чем меньше число, тем дешевле: free < cheap < expensive; metered (Claude) в лестнице не сравнивается
    for rank, name in enumerate(("free", "cheap", "expensive")):
        for agent in tiers.get(name, []):
            cost[agent] = rank
    if not cost:
        problems.append("tiers: нужны уровни стоимости free / cheap / expensive")
    for kind, steps in ladders.items():
        ranked = [cost[a] for a in steps if a in cost and a not in (data.get("manual") or [])]
        if ranked != sorted(ranked):
            problems.append(f"ladder.{kind}: исполнители идут не от дешёвых к дорогим ({' → '.join(steps)}) — нарушен приоритет стоимости")
    for name, value in (data.get("limits") or {}).items():
        if not isinstance(value, int) or value <= 0:
            problems.append(f"limits.{name}: нужно положительное целое")
    for key in ("terminal_tabs_max", "monitor_digest_sec", "oc_run_timeout_sec", "supervise_attempts", "marker_history_depth"):
        if key not in (data.get("limits") or {}):
            problems.append(f"limits.{key}: не задан")
    if not data.get("rules"):
        problems.append("rules: нужен непустой список")
    return problems


def show() -> None:
    data = load()
    if not data:
        print(f"оркестр: {path()} не читается", file=sys.stderr)
        return
    print(f"Оркестр агентов (решение {data.get('decided', '?')}, версия {data.get('version', '?')}) — {path()}")
    print("\nРоли:")
    for name, text in (data.get("roles") or {}).items():
        print(f"  {name:10} {text}")
    print("\nЛестницы (слева направо; opencode в скриптах всегда последний; manual — вручную, не скриптом):")
    manual = manual_agents()
    for kind, steps in (data.get("ladder") or {}).items():
        print(f"  {kind:19} " + " → ".join(f"{s}*" if s in manual else s for s in steps))
    print("  (* — вручную)")
    print("\nМодели:")
    for name, spec in (data.get("models") or {}).items():
        print(f"  {name:10} " + "; ".join(f"{k}={v}" for k, v in spec.items()))
    print("\nЛимиты: " + ", ".join(f"{k}={v}" for k, v in (data.get("limits") or {}).items()))
    print("\nПравила:")
    for index, rule in enumerate(data.get("rules") or [], 1):
        print(f"  {index}. {rule}")


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    cmd = argv[0]
    if cmd == "show":
        show()
        return 0
    if cmd == "get" and len(argv) == 2:
        value = get(argv[1])
        if value is None:
            return 1
        print(value if not isinstance(value, (dict, list)) else json.dumps(value, ensure_ascii=False))
        return 0
    if cmd == "ladder" and len(argv) == 2:
        steps = ladder(argv[1])
        print("\n".join(steps))
        return 0 if steps else 1
    if cmd == "validate":
        problems = validate()
        for line in problems:
            print(line)
        if not problems:
            print("ok")
        return 1 if problems else 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
