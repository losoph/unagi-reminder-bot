#!/usr/bin/env bash
# Разовое письмо со статусом разработки unagi: тело — файл из репозитория (по умолчанию docs/ALERT-HANDOFF.md).
# Письмо самодостаточно: его можно вставить в новую сессию Claude на другом компьютере — статус репозитория, спринт, окружение агентов,
# балансировка и Jev, распределение ролей, контроль расхода, прод и следующие шаги.
#
#   unagi-status-alert.sh --once 2026-10-16 [--file docs/ALERT-HANDOFF.md] [--subject "..."] [--dry-run]
#
# Запускается кроном на aiqu (дата — в строке крона), отправляет ровно один раз (флаг в $STATE_DIR/unagi). Отправка — общим send_mail из
# /usr/local/lib/tldw/common.sh (тот же msmtp и адрес, что у остальных алертов сервера; адрес в репозиторий не попадает).
set -uo pipefail
LIB_DIR="${TLDW_LIB_DIR:-/usr/local/lib/tldw}"
# shellcheck source=/dev/null
. "$LIB_DIR/common.sh"
APP_DIR="${UNAGI_APP_DIR:-/root/projects/unagi-reminder-bot}"
FILE="docs/ALERT-HANDOFF.md" SUBJECT="" ONCE="" DRY=false
while [ $# -gt 0 ]; do
  case "$1" in
    --once) ONCE="${2:?--once требует дату YYYY-MM-DD}"; shift ;;
    --file) FILE="${2:?}"; shift ;;
    --subject) SUBJECT="${2:?}"; shift ;;
    --dry-run) DRY=true ;;
  esac
  shift
done
W="${STATE_DIR:-/var/lib/tldw}/unagi"; mkdir -p "$W"
if [ -n "$ONCE" ]; then
  [ "$(TZ=Europe/Moscow date +%F)" = "$ONCE" ] || exit 0
  FLAG="$W/status-alert-$ONCE.sent"
  [ -e "$FLAG" ] && exit 0
fi
[ -r "$APP_DIR/$FILE" ] || { echo "нет файла $APP_DIR/$FILE" >&2; exit 1; }
HEAD="$(git -C "$APP_DIR" log -1 --format='%h %cd %s' --date=short 2>/dev/null)"
BODY="$(cat "$APP_DIR/$FILE")

—
Код на сервере: $HEAD
Сформировано: $(TZ=Europe/Moscow date '+%F %H:%M') MSK"
SUBJECT="${SUBJECT:-[${TAG:-aiqu}] unagi: статус разработки, спринт, агенты и следующие шаги}"
if $DRY; then printf 'To: %s\nSubject: %s\n\n%s\n' "${ALERT_TO:-<ALERT_TO>}" "$SUBJECT" "$BODY"; exit 0; fi
send_mail "$SUBJECT" "$BODY" && { [ -n "$ONCE" ] && touch "$FLAG"; exit 0; }
exit 1
