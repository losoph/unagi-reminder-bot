#!/usr/bin/env python3
"""Лестница исполнителей: кому отдать задачу вида `kind`, учитывая экономию токенов и доступность.

    executor_policy.py <kind> [--json]     # печатает лестницу и выбранного исполнителя; код 1 — никто не доступен

Приоритет экономии токенов (решение владельца 2026-10-16, подтверждено 17.10), от самых дорогих к самым дешёвым:
    opencode  >  deepseek (dsh)  >  Jev (платит за каждое решение)  >  Claude  >  agy
Бесплатны для нас agy и Argon (веб-Gemini, вручную), дёшевы Jev и dsh, дорог opencode. Поэтому основная нагрузка — на agy
(«рабочая лошадка»), затем dsh, а opencode — последним и только если вид задачи явно разрешён (`opencode_allowed`)
и Jev может выбрать модель: opencode без решения Jev не запускается вообще.
Лестницы по видам задач лежат в tools/agents/orchestra.json (там же роли, лимиты и правила; `agents.sh orchestra`).
Claude (наблюдатель) и Jev — не исполнители: первый принимает работу, второй выбирает модель (`route.sh`).
"""

from __future__ import annotations

import glob
import json
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import oc_budget  # noqa: E402
import orchestra  # noqa: E402

SAVE_ORDER = ("opencode", "dsh", "jev", "claude", "agy")  # от самых дорогих токенов к самым дешёвым
KINDS = ("executor-complex", "executor-ux", "review", "adversarial-review", "docs", "merge-audit", "short-check")


SCRIPT_EXECUTORS = ("dsh", "agy")  # исполнители, которых запускают скрипты; opencode добавляется последним, argon-web и claude — вручную


def configured_ladder(kind: str) -> list[str]:
    """Лестница вида из tools/agents/orchestra.json; пусто, если файла нет или вид не описан (тогда встроенные значения)."""
    return orchestra.ladder(kind)


def manual_steps(kind: str) -> list[str]:
    """Ручные шаги лестницы (веб-Gemini Argon, наблюдатель): скрипты их не запускают, а только напоминают."""
    manual = orchestra.manual_agents()
    return [agent for agent in configured_ladder(kind) if agent in manual]


def opencode_allowed(kind: str) -> bool:
    """Можно ли отдать задачу этого вида opencode, когда dsh и agy недоступны или не подходят.

    opencode не кеширует промпты: один агентный прогон стоил миллионы токенов, поэтому лимит — 5 млн токенов на сессию и
    1 млн на прогон (budget.json), а запускается он только по решению Jev. Разрешён для проверок и аудита; реализация — dsh/agy.
    """
    # Решение владельца 16.10: лимит 5 млн токенов на сессию; opencode — для проверок и аудита, качество выдачи ведём (quality.py).
    # С 17.10 разрешение читается из лестницы orchestra.json (opencode в ней — только у проверок и ревью).
    configured = configured_ladder(kind)
    if configured:
        return "opencode" in configured
    return kind in ("review", "adversarial-review", "merge-audit", "short-check")


def executor_order(kind: str) -> list[str]:
    """Порядок попыток для вида задачи: сначала бесплатные и дешёвые исполнители, opencode — последним и только если разрешён."""
    ladder = configured_ladder(kind)
    if ladder:
        order = [agent for agent in ladder if agent in SCRIPT_EXECUTORS]
        if order:  # недостающих исполнителей добавляем в конце как запасные ступени; у чисто ручных видов (strategy) скриптовых нет
            order += [agent for agent in SCRIPT_EXECUTORS if agent not in order]
    else:
        order = ["agy", "dsh"]  # встроенный порядок, если orchestra.json нет: сначала бесплатный agy
    if opencode_allowed(kind):
        order.append("opencode")
    return order


def jev_key_present() -> bool:
    if os.environ.get("OPENROUTER_API_KEY") or os.environ.get("TYPESAFE_API_KEY"):
        return True
    path = os.environ.get("AGENT_ENV_FILE") or os.path.expanduser("~/.config/agent-tools/env")
    try:
        with open(path, encoding="utf-8") as stream:
            for line in stream:
                name, _, value = line.strip().removeprefix("export ").partition("=")
                if name in ("OPENROUTER_API_KEY", "TYPESAFE_API_KEY") and value.strip(" '\""):
                    return True
    except OSError:
        pass
    return False


def dsh_binary() -> str | None:
    if os.environ.get("DSH_BIN"):
        return os.environ["DSH_BIN"] if os.access(os.environ["DSH_BIN"], os.X_OK) else None
    found = shutil.which("dsh")
    if found:
        return found
    candidates = sorted(glob.glob(os.path.expanduser("~/.npm/_npx/*/node_modules/.bin/dsh")), key=os.path.getmtime, reverse=True)
    return candidates[0] if candidates else None


def budget_ok(tool: str) -> tuple[bool, str]:
    sprint, limit, _, minimum = oc_budget.config(tool)
    remaining = limit - oc_budget.spent(sprint, tool)
    return remaining >= max(minimum, 1), f"остаток {max(remaining, 0)} из {limit}"


def availability(agent: str, kind: str) -> tuple[bool, str]:
    if agent == "dsh":
        if not dsh_binary():
            return False, "dsh не найден (DSH_BIN / npx dsh web)"
        ok, detail = budget_ok("dsh")
        return ok, detail
    if agent == "opencode":
        if not opencode_allowed(kind):
            return False, f"для вида «{kind}» не разрешён (executor_policy.opencode_allowed)"
        if not shutil.which("opencode"):
            return False, "opencode не найден"
        if not jev_key_present():
            return False, "нет ключа OpenRouter: модель opencode выбирается только через Jev"
        ok, detail = budget_ok("opencode")
        return ok, detail
    if agent == "agy":
        ok = bool(shutil.which("agy") or os.path.exists(os.path.expanduser("~/.local/bin/agy")))
        return ok, "agy найден" if ok else "agy не найден"
    return False, "неизвестный исполнитель"


def choose(kind: str) -> dict:
    ladder, chosen = [], None
    for agent in executor_order(kind):
        ok, detail = availability(agent, kind)
        ladder.append({"agent": agent, "available": ok, "detail": detail})
        if ok and chosen is None:
            chosen = agent
    return {"kind": kind, "executor": chosen, "ladder": ladder, "manual": manual_steps(kind)}


def main(argv):
    if not argv or argv[0].startswith("-"):
        print(__doc__, file=sys.stderr)
        return 2
    result = choose(argv[0])
    if "--json" in argv:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print("ladder: " + " → ".join(f"{step['agent']}({'ok' if step['available'] else 'нет: ' + step['detail']})" for step in result["ladder"]))
        print(f"executor={result['executor'] or 'none'}")
        if result["manual"]:
            print("manual: " + ", ".join(result["manual"]) + " (вручную; см. agents.sh orchestra)")
    return 0 if result["executor"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
