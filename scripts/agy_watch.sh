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
#   ./scripts/agy_watch.sh follow            # live: по строке на каждый завершённый шаг
#   ./scripts/agy_watch.sh digest [SEC]      # сводка раз в SEC (600): рассуждения, правки,
#                                            # коммиты; риск и ошибки — сразу
#
# Модель: AGY_MODEL (по умолчанию gemini-3.8-flash-high — дешёвая рабочая
# лошадка с нормальным рассуждением; Pro/Opus тратят лимиты в разы быстрее
# и нужны только для точечных сложных задач).
set -euo pipefail

LOG_DIR="${AGY_LOG_DIR:-.agy}"
LOG="$LOG_DIR/session.ndjson"
AGY_MODEL="${AGY_MODEL:-gemini-3.8-flash-high}"
mkdir -p "$LOG_DIR"

_need_jq() { command -v jq >/dev/null || { echo "нужен jq"; exit 1; }; }
# В лог попадают и не-JSON строки (ошибки CLI, сообщения шелла) — отсекаем их,
# иначе jq падает и монитор слепнет ровно тогда, когда что-то пошло не так.
_json() { grep '^{' "$LOG"; }

case "${1:-steps}" in
  run|cont)
    # Промпт передаётся ФАЙЛОМ: подстановку делает сам zsh, поэтому любой
    # многострочный текст с кавычками и не-ASCII проходит без искажений.
    # agy — это алиас, задающий прокси, поэтому нужен интерактивный zsh (-i);
    # креды прокси остаются в личном ~/.zshrc и в репозиторий не попадают.
    AGY_TASK_FILE="${2:?нужен путь к файлу с промптом}"
    [ -r "$AGY_TASK_FILE" ] || { echo "файл не читается: $AGY_TASK_FILE"; exit 1; }
    export AGY_TASK_FILE AGY_MODEL
    flag=""; [ "$1" = "cont" ] && flag="-c"
    zsh -ic "agy $flag --model \"\$AGY_MODEL\" -p \"\$(cat \"\$AGY_TASK_FILE\")\" --output-format stream-json" \
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
      <(_json) | tail -n "$n"
    ;;
  reasoning)
    _need_jq; n="${2:-25}"
    CID="$("$0" id)"
    jq -r --arg cid "$CID" 'select(.event=="step_update") | .step_update
           | select(.conversation_id==$cid and .state=="DONE")
           | select(.step_type|test("thinking|reason|agent_response"))
           | "[\(.step_type)] \(((.text_delta // "") | gsub("\\s+";" "))[0:400])"' \
      <(_json) | grep -v '^\[[a-z_]*\] *$' | tail -n "$n"
    ;;
  follow)
    # Живой монитор для наблюдателя (Claude Monitor / второй терминал).
    # Текст агента приходит дельтами в событиях ACTIVE, а DONE приходит пустым,
    # поэтому дельты копятся по шагу и печатаются целиком на его завершении.
    # Чтения файлов (view_file/grep/list) сворачиваются в счётчик — это шум.
    touch "$LOG"
    ${AGY_FOLLOW_SRC:-tail -n0 -F "$LOG"} 2>/dev/null | grep --line-buffered '^{' | python3 -u -c '
import json, sys
buf, quiet = {}, 0
READS = ("view_file", "grep_search", "list_dir", "find_by_name", "codebase_search", "view_code_item")
def flush_quiet():
    global quiet
    if quiet:
        print(f"READ x{quiet}"); quiet = 0
for line in sys.stdin:
    try:
        ev = json.loads(line)
    except ValueError:
        continue
    if ev.get("event") == "result":
        r = ev.get("result") or {}
        flush_quiet()
        print("RESULT status=%s turns=%s %s" % (r.get("status"), r.get("num_turns"), r.get("error") or ""))
        continue
    st = ev.get("step_update")
    if ev.get("event") != "step_update" or not st:
        continue
    key = (st.get("conversation_id"), st.get("step_index"))
    if st.get("step_type") != "tool":
        buf[key] = buf.get(key, "") + (st.get("text_delta") or "")
    if st.get("state") != "DONE":
        continue
    if st.get("step_type") == "tool":
        name = st.get("tool_name") or "?"
        if name in READS:
            quiet += 1
            continue
        flush_quiet()
        params = json.dumps((st.get("tool_info") or {}).get("parameters") or {}, ensure_ascii=False)
        print(f"TOOL {name} {params[:200]}")
    else:
        text = " ".join(buf.pop(key, "").split())
        if text:
            flush_quiet()
            print(f"SAY {text[:700]}")
'
    ;;
  digest)
    # Экономный режим наблюдателя: поверх follow копит события и раз в SEC
    # секунд печатает одну сводку (если что-то было). Рискованное и сбои —
    # отдельной строкой сразу, не дожидаясь окна.
    SEC="${2:-600}" "$0" follow | SEC="${2:-600}" python3 -u -c '
import os, re, select, sys, time
period = int(os.environ["SEC"])
risk = re.compile(r"git push|git reset --hard|rm -rf|\.env|msmtprc|SESSION|Traceback|FAILED|RESULT status=(?!SUCCESS)")
says, tools, files, commits, reads = [], 0, set(), [], 0
last = time.time()
def emit():
    global says, tools, files, commits, reads, last
    if says or tools or commits:
        parts = ["DIGEST " + time.strftime("%H:%M") + ": действий %d, чтений %d" % (tools, reads)]
        if files: parts.append("правки: " + ", ".join(sorted(files))[:300])
        if commits: parts.append("коммиты: " + "; ".join(commits)[:300])
        if says: parts.append("мысли: " + " | ".join(says[-3:])[:600])
        print(" · ".join(parts))
    says, tools, files, commits, reads = [], 0, set(), [], 0
    last = time.time()
while True:
    r, _, _ = select.select([sys.stdin], [], [], 30)
    if r:
        line = sys.stdin.readline()
        if not line:
            emit(); break
        line = line.rstrip()
        if risk.search(line):
            print("ALERT " + line[:400])
        if line.startswith("SAY "):
            says.append(line[4:200])
        elif line.startswith("READ x"):
            reads += int(line[6:] or 1)
        elif line.startswith("TOOL "):
            tools += 1
            for f in re.findall(r"\"(?:TargetFile|AbsolutePath|File)\": ?\"([^\"]+)", line):
                files.add(f.rsplit("/", 1)[-1])
            if "git commit" in line:
                commits.append(line[5:160])
        elif line.startswith("RESULT"):
            print(line); emit()
    if time.time() - last >= period:
        emit()
'
    ;;
  status)
    _need_jq
    _json | jq -r 'select(.event=="result") | .result | "status=\(.status) \(.error // "")"' | tail -1
    grep -c '^AGY_ERROR' "$LOG" 2>/dev/null | sed 's/^/сетевых ошибок в логе: /'
    ;;
  result)
    _need_jq
    jq -r 'select(.event=="result") | .result
           | "status=\(.status) turns=\(.num_turns) sec=\(.duration_seconds|floor)\n\(.response)"' \
      <(_json) | tail -40
    ;;
  id)
    _need_jq; _json | jq -r 'select(.event=="init") | .conversation_id' | tail -1
    ;;
  usage)
    _need_jq
    jq -r 'select(.event=="result") | .result.usage
           | "in=\(.input_tokens) out=\(.output_tokens) think=\(.thinking_tokens) total=\(.total_tokens)"' <(_json)
    ;;
  *) sed -n '2,20p' "$0"; exit 1 ;;
esac
