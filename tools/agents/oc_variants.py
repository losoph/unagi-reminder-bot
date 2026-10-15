#!/usr/bin/env python3
"""Варианты эффорта opencode (`provider/model#variant`): проверка и настройка в глобальном конфиге.

    oc_variants.py has <provider/model> <low|medium|high>    # код 0 — вариант определён, 1 — нет
    oc_variants.py status                                    # какие варианты есть у кандидатов opencode из routing.json
    oc_variants.py apply [--dry-run]                         # добавить low/medium/high кандидатам (резервная копия конфига рядом)

Без `variants` в конфиге суффикс `#low` ни на что не влияет (opencode 2.x не читает ни OPENCODE_CONFIG, ни OPENCODE_CONFIG_CONTENT),
поэтому `agents.sh run opencode` отказывается запускаться, пока вариант выбранного Jev уровня не определён. Правится только ключ
`variants` у моделей из routing.json; остальной файл (в том числе ключи) не трогается и не печатается.
Путь конфига — OC_CONFIG_PATH (по умолчанию ~/.config/opencode/opencode.json).
"""

import json
import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
LEVELS = ("low", "medium", "high")


def config_path():
    return os.environ.get("OC_CONFIG_PATH") or os.path.expanduser("~/.config/opencode/opencode.json")


def load():
    with open(config_path(), encoding="utf-8") as stream:
        return json.load(stream)


def candidates():
    with open(os.path.join(HERE, "routing.json"), encoding="utf-8") as stream:
        return json.load(stream)["candidates"]["opencode"]


def split(model):
    provider, _, name = model.partition("/")
    return provider, name


def has(model, level):
    provider, name = split(model)
    try:
        entry = load()["provider"][provider]["models"][name]
    except (OSError, ValueError, KeyError, TypeError):
        return False
    variants = entry.get("variants") if isinstance(entry, dict) else None
    return isinstance(variants, dict) and isinstance(variants.get(level), dict) and not variants[level].get("disabled")


def cmd_status():
    for model in candidates():
        print(f"{model}: " + ", ".join(f"{level}={'да' if has(model, level) else 'нет'}" for level in LEVELS))
    return 0


def cmd_apply(dry_run):
    data = load()
    changed = []
    for model in candidates():
        provider, name = split(model)
        try:
            entry = data["provider"][provider]["models"][name]
        except (KeyError, TypeError):
            print(f"пропуск {model}: нет в конфиге", file=sys.stderr)
            continue
        variants = entry.setdefault("variants", {})
        for level in LEVELS:
            if level not in variants:
                variants[level] = {"reasoningEffort": level}
                changed.append(f"{model}#{level}")
    if not changed:
        print("ничего менять не нужно: варианты уже определены")
        return 0
    print(("добавил бы: " if dry_run else "добавлено: ") + ", ".join(changed))
    if dry_run:
        return 0
    path = config_path()
    backup = f"{path}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.copy2(path, backup)
    os.chmod(backup, 0o600)
    temporary = f"{path}.tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    os.replace(temporary, path)
    print(f"резервная копия: {backup}")
    return 0


def main(argv):
    if argv[:1] == ["has"] and len(argv) == 3:
        return 0 if has(argv[1], argv[2]) else 1
    if argv[:1] == ["status"]:
        return cmd_status()
    if argv[:1] == ["apply"]:
        return cmd_apply("--dry-run" in argv)
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
