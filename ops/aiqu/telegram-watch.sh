#!/usr/bin/env bash
# Мониторинг выхода официальной ленты постов в Telegram: следит за страницами релизов и изменениями Bot API,
# при изменении страницы или появлении ключевых слов присылает письмо (общий send_mail сервера). Первый запуск только запоминает состояние.
#
#   telegram-watch.sh [--dry-run]
# Крон (воскресенье 18.10 и понедельник 19.10, каждые 30 минут с 12:00 до 23:30): см. docs/ALERT-HANDOFF.md.
set -uo pipefail
LIB_DIR="${TLDW_LIB_DIR:-/usr/local/lib/tldw}"
# shellcheck source=/dev/null
. "$LIB_DIR/common.sh"
DRY=false; [ "${1:-}" = "--dry-run" ] && DRY=true
W="${STATE_DIR:-/var/lib/tldw}/unagi/telegram-watch"; mkdir -p "$W"
KEYWORDS='feed|ленту? постов|лент[аы] (постов|каналов)|channel feed|posts feed|new feed|Feed tab|вкладк[аи] .{0,20}(Лент|Feed)'
SOURCES="https://telegram.org/blog https://core.telegram.org/bots/api-changelog https://telegram.org/apps"
changes=""
for url in $SOURCES; do
  key="$(printf '%s' "$url" | tr -c 'A-Za-z0-9' '_')"
  page="$(curl -fsS -m 30 -A 'Mozilla/5.0 unagi-watch' "$url" 2>/dev/null)" || { changes="$changes\n- не удалось прочитать $url"; continue; }
  text="$(printf '%s' "$page" | sed -e 's/<script[^>]*>.*<\/script>//g' -e 's/<[^>]*>/ /g' | tr -s ' \n' ' ')"
  hash="$(printf '%s' "$text" | sha256sum | cut -c1-16)"
  hits="$(printf '%s' "$text" | grep -oiE ".{0,80}($KEYWORDS).{0,120}" | head -5)"
  old="$(cat "$W/$key.hash" 2>/dev/null || true)"
  if [ -z "$old" ]; then echo "$hash" > "$W/$key.hash"; echo "baseline $url"; continue; fi
  if [ "$hash" != "$old" ]; then
    echo "$hash" > "$W/$key.hash"
    changes="$changes\n- ИЗМЕНИЛАСЬ $url${hits:+\n    совпадения: $hits}"
  fi
done
[ -n "$changes" ] || { echo "изменений нет"; exit 0; }
BODY="$(printf 'Мониторинг официальной ленты постов Telegram (ожидается 18.10 вечером).\n%b\n\nЧто проверить: описание функции, влияние на Ленту unagi (docs/ALERT-HANDOFF.md, раздел «Ленты Telegram»), сроки доступности для каналов и ботов.' "$changes")"
if $DRY; then printf '%s\n' "$BODY"; else send_mail "[${TAG:-aiqu}] Telegram: возможное обновление — официальная лента постов" "$BODY"; fi
