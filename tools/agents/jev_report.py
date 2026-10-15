#!/usr/bin/env python3
"""Сводка по решениям Jev: сколько выборов моделей и простых решений принял Jev, сколько откатов и во что это обошлось.

    jev_report.py [--brief]     # журналы: AGENT_ROUTE_LOG (.agents/routes.ndjson) и AGENT_DECISION_LOG (.agents/decisions.ndjson)
"""
import collections
import json
import os
import sys


def rows(path):
    try:
        with open(path, encoding="utf-8") as stream:
            for line in stream:
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                if isinstance(item, dict):
                    yield item
    except FileNotFoundError:
        return


def mean(values):
    values = [v for v in values if isinstance(v, (int, float))]
    return sum(values) / len(values) if values else None


def main(argv):
    routes = list(rows(os.environ.get("AGENT_ROUTE_LOG", ".agents/routes.ndjson")))
    decisions = list(rows(os.environ.get("AGENT_DECISION_LOG", ".agents/decisions.ndjson")))
    sources = collections.Counter(r.get("source", "?") for r in routes)
    jev_routes = [r for r in routes if r.get("source") == "jev"]
    reasons = collections.Counter((r.get("fallback_reason") or "—")[:60] for r in routes if r.get("source") != "jev")
    cost = sum(r["meta"]["cost"] for r in jev_routes + decisions if isinstance(r.get("meta"), dict) and isinstance(r["meta"].get("cost"), (int, float)))
    latency = mean([r["meta"].get("latency_ms") for r in jev_routes if isinstance(r.get("meta"), dict)])
    confidence = mean([r["meta"].get("confidence") for r in jev_routes if isinstance(r.get("meta"), dict)])
    print(f"jev: выборов модели {len(routes)} (jev {sources.get('jev', 0)}, откат на статику {len(routes) - sources.get('jev', 0)}); "
          f"простых решений {len(decisions)} (к человеку {sum(1 for d in decisions if d.get('choice') == 'ASK_HUMAN')})")
    if "--brief" in argv:
        return 0
    if reasons:
        print("  причины отката: " + "; ".join(f"{reason} ×{count}" for reason, count in reasons.most_common(4)))
    by_agent = collections.defaultdict(collections.Counter)
    for r in jev_routes:
        by_agent[r.get("agent", "?")][r.get("model", "?")] += 1
    for agent, models in sorted(by_agent.items()):
        print(f"  {agent}: " + ", ".join(f"{model} ×{count}" for model, count in models.most_common()))
    print(f"  стоимость решений Jev: {cost:.8f}" + (f", средняя задержка {latency:.0f} мс" if latency is not None else "")
          + (f", средняя уверенность {confidence:.2f}" if confidence is not None else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
