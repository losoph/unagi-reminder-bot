#!/bin/sh
# Точка входа контейнера unagi-bot (ревью безопасности, п.7).
#
# От root работает доли секунды: готовит каталог данных и копию SMTP-конфига, затем навсегда
# сбрасывает права до пользователя bot (uid 10001, без capabilities, no-new-privs) и запускает
# команду контейнера. Если контейнер уже запущен не от root (`--user`), просто выполняет команду.
set -eu

BOT_UID=10001
BOT_GID=10001
STORAGE="${STORAGE_DIR:-/app/storage}"
SMTP_SRC="${MSMTPRC_SRC:-/run/secrets/msmtprc}"
SMTP_DIR="${MSMTPRC_DIR:-/run/unagi}"

if [ "$(id -u)" -eq 0 ]; then
    # ./storage — bind-mount с хоста: раньше им владел root, а боту нужно писать БД, WAL и снимки.
    chown -R "$BOT_UID:$BOT_GID" "$STORAGE"
    # Исходный msmtprc на хосте закрыт 0600 для root. Бот читает копию из tmpfs: на диск и в образ она не попадает.
    if [ -f "$SMTP_SRC" ] && [ -d "$SMTP_DIR" ]; then
        { (umask 077 && cp "$SMTP_SRC" "$SMTP_DIR/msmtprc") && chown "$BOT_UID:$BOT_GID" "$SMTP_DIR/msmtprc"; } \
            || echo "entrypoint: SMTP-конфиг не скопирован, email-дайджесты будут выключены" >&2
    fi
    exec setpriv --reuid="$BOT_UID" --regid="$BOT_GID" --clear-groups --no-new-privs "$@"
fi
exec "$@"
