#!/usr/bin/env bash
# Ключевое простое решение через Jev (OpenRouter Decisions API): закрытый список вариантов, уверенность, эскалация к человеку.
#
#   decide.sh "Влить T24c в main?" merge="ревью чистое, тесты зелёные" fix="есть подтверждённые дефекты" hold="не хватает данных" \
#       --default hold [--context-file итог_ревью.txt] [--title T24c] [--dry-run]
#
# stdout: `choice=<вариант|ASK_HUMAN> source=jev|default|human confidence=…`, затем `fallback=…` и `meta=…` при наличии.
# Код выхода: 0 — решено, 10 — решает человек, 2 — неверный вызов. Журнал: $AGENT_DECISION_LOG (.agents/decisions.ndjson).
# Jev только советует; push, откат прода и удаление данных остаются за наблюдателем и владельцем.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
. "$SCRIPT_DIR/agent_env.sh"
agent_env_load OPENROUTER_API_KEY TYPESAFE_API_KEY
exec "${PYTHON:-python3}" "$SCRIPT_DIR/routers/jev_ask.py" "$@"
