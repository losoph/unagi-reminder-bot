#!/usr/bin/env bash
# Очередь ревью без участия наблюдателя: для каждой пары <worktree>:<МЕТКА> ждёт маркер готовности «(МЕТКА)» в git log ветки,
# затем запускает read-only ревью dsh (факты среды из orchestra.json + дифф к origin/main) и пишет отчёт в <репозиторий>/.agents/reviews/<МЕТКА>.txt
#
#   review_queue.sh <основной-репозиторий> <worktree>:<МЕТКА> [...]      # ждёт до REVIEW_WAIT_MIN минут (по умолчанию 180)
set -uo pipefail
MAIN="${1:?основной репозиторий}"; shift
MAIN="$(cd "$MAIN" && pwd)"
OUT="$MAIN/.agents/reviews"; mkdir -p "$OUT"
FACTS="$(python3 "$MAIN/tools/agents/orchestra.py" get facts | python3 -c 'import json,sys; [print("- "+f) for f in json.loads(sys.stdin.read())]')"
review_one() {
  local wt="$1" tag="$2" waited=0 limit="${REVIEW_WAIT_MIN:-180}"
  until git -C "$wt" log --oneline -40 | grep -qF "($tag)"; do
    sleep 60; waited=$((waited + 1))
    [ "$waited" -lt "$limit" ] || { echo "$tag: маркер не появился за ${limit} мин" > "$OUT/$tag.txt"; return 1; }
  done
  local brief; brief="$(mktemp "${TMPDIR:-/tmp}/review-$tag.XXXXXX")"
  {
    echo "ТОЛЬКО ЧТЕНИЕ. Состязательное ревью ветки задачи $tag: файлы не менять, коммитов нет."
    echo "ФАКТЫ СРЕДЫ:"; echo "$FACTS"
    echo "Прочитай изменения: git -C $wt diff origin/main...HEAD (и сами файлы). Найди реальные дефекты: несовместимости со средой, пропущенные места использования, ослабленные тесты, поведение, отличное от брифа, риски для прода и деплоя."
    echo "Ответ: до 10 пунктов «файл: суть — сценарий — серьёзность» и вердикт OK / ЕСТЬ ЗАМЕЧАНИЯ."
  } > "$brief"
  "$MAIN/tools/agents/agents.sh" run dsh review "$wt" "$brief" "$OUT/$tag.ndjson" "REV-$tag" --review 2>&1 | sed -E 's/\x1b\[[0-9;]*m//g' | tail -60 > "$OUT/$tag.txt"
  rm -f "$brief"
}
for spec in "$@"; do review_one "${spec%%:*}" "${spec#*:}" & done
wait
echo "очередь ревью завершена: $OUT"
