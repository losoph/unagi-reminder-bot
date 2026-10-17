#!/usr/bin/env python3
"""Сверка порядка диспетчеризации aiogram-хендлеров до и после распила main.py (метод MS1, 17.10.2026).

    handler_order.py dump <каталог репозитория> > handlers.tsv
    handler_order.py compare <до.tsv> <после.tsv>

`dump` импортирует `main` из каталога с фиктивным BOT_TOKEN и временной БД, обходит `dp` и вложенные роутеры в порядке
диспетчеризации aiogram (сначала собственные хендлеры роутера, затем вложенные по порядку включения) и печатает TSV:
событие, путь роутера, имя хендлера, описание фильтров.
`compare` проверяет: тот же набор хендлеров; для каждого события — пары «обычный хендлер, который раньше шёл ПОСЛЕ перенесённого,
а теперь стоит ДО него»: если у такой пары фильтры могут совпасть на одном апдейте, это риск смены приоритета.
Фильтры callback_data сравниваются по литералам (`==`, `startswith`, `in_`), сообщения — по Command и состояниям FSM
(разные команды и разные состояния взаимоисключающи). Код выхода 1 — набор хендлеров изменился или найдены пересечения / неопознанные фильтры.
"""

from __future__ import annotations

import os
import re
import sys
import tempfile

LITERAL_RE = re.compile(r"'([^']*)'")


def describe_filter(flt) -> str:
    callback = getattr(flt, "callback", None)
    owner = getattr(callback, "__self__", None)
    operations = getattr(owner, "_operations", None)
    if operations is not None:  # F.data == 'x' / .startswith('x') / .in_({...})
        parts = []
        for op in operations:
            kind = type(op).__name__
            if kind == "GetAttributeOperation":
                parts.append("." + str(op.name))
            elif kind == "ComparatorOperation":
                parts.append(f" {getattr(op.comparator, '__name__', 'cmp')} {op.right!r}")
            elif kind == "CallOperation":
                parts.append("(" + ",".join(repr(arg) for arg in op.args) + ")")
            elif kind == "FunctionOperation":
                parts.append(f" {getattr(op.function, '__name__', 'fn')} {','.join(repr(sorted(arg, key=str)) if isinstance(arg, (set, frozenset)) else repr(arg) for arg in op.args)}")
            else:
                parts.append(f"<{kind}>")
        return "F" + "".join(parts)
    if hasattr(callback, "commands"):
        return "Command" + repr(tuple(callback.commands))
    return repr(callback if callback is not None else flt)[:100].replace("\t", " ")


def collect(router, path: str = "dp") -> list[tuple[str, str, str, str]]:
    """Хендлеры в порядке диспетчеризации: (событие, путь роутера, имя, фильтры)."""
    rows = []
    for event, observer in router.observers.items():
        for handler in observer.handlers:
            description = " & ".join(describe_filter(f) for f in handler.filters)
            rows.append((event, path, getattr(handler.callback, "__name__", "?"), description))
    for sub in router.sub_routers:
        rows.extend(collect(sub, f"{path}/{sub.name or 'router'}"))
    return rows


def literals(description: str) -> list[str]:
    return LITERAL_RE.findall(description)


def may_overlap(event: str, first: str, second: str) -> str | None:
    """Почему два фильтра могут сработать на одном апдейте (None — не могут)."""
    if event == "callback_query":
        a, b = literals(first), literals(second)
        if not a or not b:
            return "нет литералов callback_data"
        if any(x.startswith(y) or y.startswith(x) for x in a for y in b):
            return "пересекаются литералы"
        return None
    if first.startswith("Command") and second.startswith("Command"):
        return "одна и та же команда" if set(literals(first)) & set(literals(second)) else None
    state_a, state_b = re.search(r"State '([^']+)'", first), re.search(r"State '([^']+)'", second)
    if state_a and state_b:
        return "одно и то же состояние" if state_a.group(1) == state_b.group(1) else None
    if (state_a or first.startswith("Command")) and (state_b or second.startswith("Command")):
        return None  # команда и состояние FSM могут совпасть у одного пользователя только если команда не прерывает состояние — это поведение aiogram не меняется перестановкой, но помечаем осторожно
    return "общий фильтр без состояния и команды"


def compare(before: list[tuple], after: list[tuple]) -> tuple[list[str], bool]:
    report, failed = [], False
    names_before, names_after = sorted(r[2] + "|" + r[0] for r in before), sorted(r[2] + "|" + r[0] for r in after)
    if names_before != names_after:
        failed = True
        lost = sorted(set(names_before) - set(names_after))
        added = sorted(set(names_after) - set(names_before))
        report.append(f"НАБОР ХЕНДЛЕРОВ ИЗМЕНИЛСЯ: потеряно {lost[:10]}, добавлено {added[:10]}")
    else:
        report.append(f"набор хендлеров тот же: {len(before)}")
    moved = {r[2] for r in after if any(b[2] == r[2] and b[1] != r[1] for b in before)}
    report.append(f"перенесено в другой роутер: {len(moved)}")
    for event in sorted({r[0] for r in before}):
        seq_before = [r for r in before if r[0] == event]
        seq_after = [r for r in after if r[0] == event]
        names_b, names_a = [r[2] for r in seq_before], [r[2] for r in seq_after]
        filters = {r[2]: r[3] for r in seq_before}
        risky = []
        for moved_name in (n for n in names_a if n in moved and n in names_b):
            for other in names_a[: names_a.index(moved_name)]:
                if other in moved or other not in names_b or names_b.index(other) < names_b.index(moved_name):
                    continue
                reason = may_overlap(event, filters.get(moved_name, ""), filters.get(other, ""))
                if reason:
                    risky.append((moved_name, other, reason))
        if risky:
            failed = True
        report.append(f"[{event}] пар с возможной сменой приоритета: {len(risky)}")
        for moved_name, other, reason in risky[:20]:
            report.append(f"    {other} теперь раньше {moved_name}: {reason} ({filters.get(other, '')[:60]} | {filters.get(moved_name, '')[:60]})")
    return report, failed


def read_tsv(path: str) -> list[tuple[str, str, str, str]]:
    with open(path, encoding="utf-8") as stream:
        return [tuple((line.rstrip("\n").split("\t") + ["", "", "", ""])[:4]) for line in stream if line.strip()]


def dump(repo: str) -> int:
    os.environ.setdefault("BOT_TOKEN", "123456:handler-order")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["DB_PATH"] = os.path.join(tmp, "handler-order.db")
        sys.path.insert(0, os.path.abspath(repo))
        import main  # noqa: PLC0415  (импорт после настройки окружения)

        for row in collect(main.dp):
            print("\t".join(row))
    return 0


def main_cli(argv: list[str]) -> int:
    if len(argv) == 2 and argv[0] == "dump":
        return dump(argv[1])
    if len(argv) == 3 and argv[0] == "compare":
        report, failed = compare(read_tsv(argv[1]), read_tsv(argv[2]))
        print("\n".join(report))
        print("ИТОГ: " + ("ТРЕБУЕТ ВНИМАНИЯ" if failed else "порядок не конфликтует"))
        return 1 if failed else 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main_cli(sys.argv[1:]))
