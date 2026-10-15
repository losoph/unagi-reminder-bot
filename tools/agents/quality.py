#!/usr/bin/env python3
"""Журнал качества выдачи агентов: наблюдатель оценивает каждый результат, отчёт сводит по агентам и моделям.

    quality.py rate <agent> <model> <title> <good|partial|bad> [заметка]
    quality.py report
Журнал — AGENT_QUALITY_LOG (.agents/quality.ndjson). good = принято как есть, partial = принято после правок, bad = отброшено/переделано.
"""
import collections
import json
import os
import sys
import time

LOG = os.environ.get("AGENT_QUALITY_LOG", ".agents/quality.ndjson")
VERDICTS = ("good", "partial", "bad")


def rows():
    try:
        with open(LOG, encoding="utf-8") as stream:
            for line in stream:
                try:
                    yield json.loads(line)
                except ValueError:
                    continue
    except FileNotFoundError:
        return


def main(argv):
    if argv[:1] == ["rate"] and len(argv) >= 5 and argv[4] in VERDICTS:
        os.makedirs(os.path.dirname(LOG) or ".", exist_ok=True)
        row = {"ts": int(time.time()), "agent": argv[1], "model": argv[2], "title": argv[3], "verdict": argv[4], "note": " ".join(argv[5:])[:300]}
        fd = os.open(LOG, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
        return 0
    if argv[:1] == ["report"]:
        stats = collections.defaultdict(collections.Counter)
        for row in rows():
            stats[(row.get("agent"), row.get("model"))][row.get("verdict")] += 1
        if not stats:
            print("quality: оценок пока нет")
        for (agent, model), counter in sorted(stats.items(), key=lambda item: str(item[0])):
            total = sum(counter.values())
            print(f"quality {agent}/{model}: good {counter['good']}, partial {counter['partial']}, bad {counter['bad']} (принято без правок {counter['good'] * 100 // total}%)")
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
