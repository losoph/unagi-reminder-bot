#!/usr/bin/env bash
# Экономичный мониторинг agy-сессии.
#
# Проблема: интерактивный TUI agy печатает кадры спиннера и многократно
# перерисовывает диффы. Чтение такого вывода стоит тысячи токенов и почти не
# содержит смысла.
#
# Решение: гонять agy в print-режиме со stream-json, писать NDJSON в лог и
# читать только дистиллят — по одной строке на завершённый шаг.
#
#   ./scripts/agy_watch.sh run  "<промпт>"   # новый разговор
#   ./scripts/agy_watch.sh cont "<промпт>"   # продолжить последний разговор
#   ./scripts/agy_watch.sh steps [N]         # дистиллят: последние N шагов
#   ./scripts/agy_watch.sh reasoning [N]     # только рассуждения/текст агента
#   ./scripts/agy_watch.sh result            # финальный ответ последнего прогона
#   ./scripts/agy_watch.sh id                # conversation_id
#   ./scripts/agy_watch.sh usage             # токены по прогонам
set -euo pipefail

LOG_DIR="${AGY_LOG_DIR:-.agy}"
LOG="$LOG_DIR/session.ndjson"
mkdir -p "$LOG_DIR"

_need_jq() { command -v jq >/dev/null || { echo "нужен jq"; exit 1; }; }

case "${1:-steps}" in
  run|cont)
    # Промпт передаётся ФАЙЛОМ: подстановку делает сам zsh, поэтому любой
    # многострочный текст с кавычками и не-ASCII проходит без искажений.
    # agy — это алиас, задающий прокси, поэтому нужен интерактивный zsh (-i);
    # креды прокси остаются в личном ~/.zshrc и в репозиторий не попадают.
    AGY_TASK_FILE="${2:?нужен путь к файлу с промптом}"
    [ -r "$AGY_TASK_FILE" ] || { echo "файл не читается: $AGY_TASK_FILE"; exit 1; }
    export AGY_TASK_FILE
    flag=""; [ "$1" = "cont" ] && flag="-c"
    zsh -ic "agy $flag -p \"\$(cat \"\$AGY_TASK_FILE\")\" --output-format stream-json" \
      2>&1 | tee -a "$LOG" >/dev/null
    echo "прогон записан в $LOG"
    "$0" result
    ;;
  steps)
    _need_jq; n="${2:-40}"
    # Только последний разговор: лог накапливается между прогонами.
    CID="$("$0" id)"
    jq -r --arg cid "$CID" 'select(.event=="step_update") | .step_update
           | select(.conversation_id==$cid and .state=="DONE")
           | . as $s
           | (if .step_type=="tool"
                then "\(.tool_name // "?") \((($s.tool_info.parameters // {}) | tostring)[0:90])"
                else ((.text_delta // "") | gsub("\\s+";" "))[0:150] end) as $what
           | select($what | test("\\S"))
           | "\(.step_index)\t\(.step_type)\t\($what)"' \
      "$LOG" | tail -n "$n"
    ;;
  reasoning)
    _need_jq; n="${2:-25}"
    CID="$("$0" id)"
    jq -r --arg cid "$CID" 'select(.event=="step_update") | .step_update
           | select(.conversation_id==$cid and .state=="DONE")
           | select(.step_type|test("thinking|reason|agent_response"))
           | "[\(.step_type)] \(((.text_delta // "") | gsub("\\s+";" "))[0:400])"' \
      "$LOG" | grep -v '^\[[a-z_]*\] *$' | tail -n "$n"
    ;;
  result)
    _need_jq
    jq -r 'select(.event=="result") | .result
           | "status=\(.status) turns=\(.num_turns) sec=\(.duration_seconds|floor)\n\(.response)"' \
      "$LOG" | tail -40
    ;;
  id)
    _need_jq; jq -r 'select(.event=="init") | .conversation_id' "$LOG" | tail -1
    ;;
  usage)
    _need_jq
    jq -r 'select(.event=="result") | .result.usage
           | "in=\(.input_tokens) out=\(.output_tokens) think=\(.thinking_tokens) total=\(.total_tokens)"' "$LOG"
    ;;
  *) sed -n '2,20p' "$0"; exit 1 ;;
esac
