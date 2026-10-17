#!/usr/bin/env bash
# Контроль прода по ssh: только чтение, один сеанс. Печатает `OK|WARN|ALERT <проверка>: <детали>`; код выхода 1 при ALERT.
#
#   ops/aiqu/prod_check.sh [--since 45m] [--expect <sha|origin/main>]
#
# PROD_SSH (ssh-алиас, по умолчанию aiqu), PROD_DIR, PROD_CONTAINER — переопределяются окружением.
# Диск — порог ALERT_DISK_PCT (85), возраст резервной копии — WARN_BACKUP_HOURS (48).
set -uo pipefail
SSH_HOST="${PROD_SSH:-aiqu}"
PROD_DIR="${PROD_DIR:-projects/unagi-reminder-bot}"
CONTAINER="${PROD_CONTAINER:-unagi-bot}"
SINCE="45m"
EXPECT="origin/main"
while [ $# -gt 0 ]; do
  case "$1" in
    --since) SINCE="${2:?нужен интервал}"; shift 2 ;;
    --expect) EXPECT="${2:?нужен коммит}"; shift 2 ;;
    *) echo "использование: prod_check.sh [--since 45m] [--expect <sha|origin/main>]" >&2; exit 2 ;;
  esac
done
case "$SINCE" in *[!0-9smh]*|"") echo "некорректный интервал: $SINCE" >&2; exit 2 ;; esac
expected="$(git rev-parse --short "$EXPECT" 2>/dev/null || true)"

remote() {
  cd "$1" || { echo "dir_missing=1"; return; }
  echo "rev=$(git rev-parse --short HEAD)"
  # docker-compose.yml на проде правят вручную (лимит CPU) — это известная, не тревожная правка.
  echo "dirty=$(git status --porcelain --untracked-files=no | grep -v ' docker-compose.yml$' | wc -l | tr -d ' ')"
  echo "container=$(docker inspect -f '{{.State.Status}} restarting={{.State.Restarting}} started={{.State.StartedAt}}' "$2" 2>/dev/null || echo missing)"
  logs="$(docker logs --since "$3" "$2" 2>&1)"
  echo "log_errors=$(printf '%s\n' "$logs" | grep -cE ' - (ERROR|CRITICAL) - |Traceback')"
  echo "last_error=$(printf '%s\n' "$logs" | grep -E ' - (ERROR|CRITICAL) - ' | tail -n 1 | cut -c1-200)"
  echo "last_digest_cycle=$(printf '%s\n' "$logs" | grep 'digest_cycle' | tail -n 1 | cut -c1-24)"
  echo "feed_builds=$(printf '%s\n' "$logs" | grep -c 'feed_build')"
  echo "db_check=$(sqlite3 -readonly storage/bot_data.db 'pragma quick_check;' 2>&1 | head -n 1)"
  echo "db_tables=$(sqlite3 -readonly storage/bot_data.db "select count(*) from sqlite_master where type='table';" 2>&1)"
  # OPS1: цифры для решений владельца. Пустой результат или ошибка не ломают prod_check.
  stats="$(sqlite3 -readonly storage/bot_data.db < ops/aiqu/prod_stats.sql 2>/dev/null || true)"
  [ -n "$stats" ] && printf '%s\n' "$stats"
  echo "disk_pct=$(df --output=pcent . | tail -n 1 | tr -dc '0-9')"
  newest="$(ls -1t storage/backups/*.db 2>/dev/null | head -n 1)"
  echo "backup_age_h=$([ -n "$newest" ] && echo $(( ( $(date +%s) - $(stat -c %Y "$newest") ) / 3600 )) || echo none)"
  echo "mem=$(docker stats --no-stream --format '{{.MemUsage}}' "$2" 2>/dev/null)"
  echo "compose_dirty=$(git status --porcelain --untracked-files=no docker-compose.yml | wc -l | tr -d ' ')"
  proc="$(docker exec "$2" sh -c 'grep -E "^(Uid|CapEff|NoNewPrivs):" /proc/1/status | tr -s "[:blank:]" " " | tr "\n" ";"' 2>&1 | cut -c1-150)"
  echo "proc=$proc"
  # Тот же пользователь, под которым работает бот: от root файл msmtprc читается всегда и проверка ничего бы не показала.
  uid="$(printf '%s' "$proc" | sed -n 's/^Uid: \([0-9]*\) .*/\1/p')"
  echo "email=$(docker exec -u "${uid:-0}" "$2" python -c 'import email_digest; print(email_digest.is_enabled())' 2>&1 | tail -n 1 | cut -c1-100)"
  echo "now=$(date '+%F %T')"
}

out="$(ssh -o BatchMode=yes -o ConnectTimeout=15 "$SSH_HOST" "$(declare -f remote); remote '$PROD_DIR' '$CONTAINER' '$SINCE'" 2>&1)" || { echo "ALERT ssh: нет связи с $SSH_HOST"; printf '%s\n' "$out" | tail -n 3; exit 1; }
get() { printf '%s\n' "$out" | sed -n "s/^$1=//p" | head -n 1; }

status=0
say() { printf '%s %s\n' "$1" "$2"; [ "$1" = ALERT ] && status=1; return 0; }

rev="$(get rev)"
if [ -n "$expected" ] && [ "$rev" != "$expected" ]; then say ALERT "версия: на проде $rev, ожидалось $expected"; else say OK "версия: $rev"; fi
[ "$(get dirty)" = 0 ] && say OK "рабочее дерево: чисто" || say WARN "рабочее дерево: изменённых файлов $(get dirty)"
container="$(get container)"
case "$container" in
  "running restarting=false"*) say OK "контейнер: $container" ;;
  *) say ALERT "контейнер: $container" ;;
esac
errors="$(get log_errors)"
if [ "${errors:-0}" -gt 0 ]; then say ALERT "логи за $SINCE: ошибок $errors, последняя: $(get last_error)"; else say OK "логи за $SINCE: ошибок нет"; fi
cycle="$(get last_digest_cycle)"
[ -n "$cycle" ] && say OK "цикл дайджеста: $cycle" || say WARN "цикл дайджеста: за $SINCE строки digest_cycle нет"
say OK "сборок Ленты за $SINCE: $(get feed_builds)"
[ "$(get db_check)" = ok ] && say OK "БД: quick_check ok, таблиц $(get db_tables)" || say ALERT "БД: $(get db_check)"
disk="$(get disk_pct)"
if [ "${disk:-100}" -ge "${ALERT_DISK_PCT:-85}" ]; then say ALERT "диск: $disk%"; else say OK "диск: $disk%"; fi
age="$(get backup_age_h)"
if [ "$age" = none ]; then say WARN "резервные копии: нет"
elif [ "$age" -gt "${WARN_BACKUP_HOURS:-48}" ]; then say WARN "резервная копия: ${age} ч назад"
else say OK "резервная копия: ${age} ч назад"; fi
say OK "память контейнера: $(get mem)"
stats_failed="$(get reminders_failed_30d)"
stats_total="$(get reminders_total_30d)"
stats_subs="$(get subs_active)"
stats_users="$(get users_with_subs)"
stats_conflicts="$(get channels_multi_tag_or_period)"
if [ -n "$stats_failed" ] && [ -n "$stats_total" ] && [ -n "$stats_subs" ] && [ -n "$stats_users" ] && [ -n "$stats_conflicts" ]; then
  say OK "цифры для решений: failed/total за 30 дн $stats_failed/$stats_total, активных подписок/пользователей $stats_subs/$stats_users, каналов с конфликтом $stats_conflicts"
else
  say OK "цифры для решений: нет данных"
fi
[ "$(get compose_dirty)" = 0 ] && say OK "docker-compose.yml: без ручных правок" || say OK "docker-compose.yml: ручная правка на проде (cpus), деплой сохраняет её через --autostash"
proc="$(get proc)"
if printf '%s' "$proc" | grep -q '^Uid: 0 '; then
  say WARN "права процесса: бот работает от root"
elif printf '%s' "$proc" | grep -Eq 'CapEff: 0+;' && printf '%s' "$proc" | grep -q 'NoNewPrivs: 1'; then
  say OK "права процесса: не root, без capabilities, no-new-privs"
else
  say WARN "права процесса: $proc"
fi
case "$(get email)" in
  True) say OK "email-дайджесты: SMTP-конфиг читается ботом" ;;
  False) say WARN "email-дайджесты выключены: нет SMTP-конфига или он недоступен боту" ;;
  *) say WARN "email-дайджесты: проверка не удалась: $(get email)" ;;
esac
exit "$status"
