#!/usr/bin/env bash
# ops/aiqu/setup_deploy_user.sh — Идемпотентная подготовка пользователя deploy на сервере aiqu.
# Запускается владельцем сервера под root (или через sudo):
#     ./ops/aiqu/setup_deploy_user.sh /path/to/deploy_key.pub [/home/deploy/projects/unagi-reminder-bot]
#
# Что делает скрипт:
# 1. Создаёт пользователя deploy (без пароля, без sudo, shell /bin/bash), если он ещё не существует.
# 2. Добавляет пользователя deploy в группу docker (для работы с docker compose без root).
# 3. Настраивает ~/.ssh/authorized_keys из переданного файла публичного ключа (идемпотентно, без дублирования).
# 4. Создаёт и передаёт во владение пользователю deploy каталог проекта (по умолчанию /home/deploy/projects/unagi-reminder-bot).
# 5. Проверяет и гарантирует отсутствие привилегий sudo у пользователя deploy.

set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "ОШИБКА: этот скрипт должен запускаться от root (или через sudo)" >&2
    exit 1
fi

KEY_FILE="${1:-}"
PROJECT_DIR="${2:-/home/deploy/projects/unagi-reminder-bot}"
DEPLOY_USER="deploy"

if [ -z "$KEY_FILE" ]; then
    echo "Использование: $0 <файл_с_публичным_ключом> [каталог_проекта]" >&2
    echo "Пример: $0 /tmp/deploy_id_ed25519.pub /home/deploy/projects/unagi-reminder-bot" >&2
    exit 1
fi

if [ ! -f "$KEY_FILE" ]; then
    echo "ОШИБКА: файл публичного ключа '$KEY_FILE' не существует" >&2
    exit 1
fi

if [ ! -s "$KEY_FILE" ]; then
    echo "ОШИБКА: файл публичного ключа '$KEY_FILE' пуст" >&2
    exit 1
fi

# 1. Пользователь deploy (без пароля)
if id -u "$DEPLOY_USER" >/dev/null 2>&1; then
    echo "[OK] Пользователь '$DEPLOY_USER' уже существует."
else
    echo "[+] Создание пользователя '$DEPLOY_USER'..."
    useradd -m -s /bin/bash "$DEPLOY_USER"
fi

# Блокировка пароля: вход только по SSH-ключам (без пароля)
passwd -l "$DEPLOY_USER" >/dev/null 2>&1 || usermod -p '*' "$DEPLOY_USER"

# 2. Группа docker
if ! getent group docker >/dev/null 2>&1; then
    echo "[+] Создание группы 'docker'..."
    groupadd docker
fi

echo "[+] Добавление '$DEPLOY_USER' в группу 'docker'..."
usermod -aG docker "$DEPLOY_USER"

# 3. Без sudo: удаление из привилегированных групп и удаление файлов sudoers
for grp in sudo wheel admin; do
    if getent group "$grp" >/dev/null 2>&1; then
        gpasswd -d "$DEPLOY_USER" "$grp" >/dev/null 2>&1 || true
    fi
done
rm -f /etc/sudoers.d/deploy /etc/sudoers.d/90-deploy

# 4. Настройка SSH и authorized_keys
DEPLOY_HOME="$(getent passwd "$DEPLOY_USER" | cut -d: -f6)"
if [ -z "$DEPLOY_HOME" ]; then
    DEPLOY_HOME="/home/$DEPLOY_USER"
fi

SSH_DIR="$DEPLOY_HOME/.ssh"
AUTH_KEYS="$SSH_DIR/authorized_keys"

mkdir -p "$SSH_DIR"
chmod 0700 "$SSH_DIR"
touch "$AUTH_KEYS"
chmod 0600 "$AUTH_KEYS"

echo "[+] Обновление $AUTH_KEYS из '$KEY_FILE'..."
while IFS= read -r line || [ -n "$line" ]; do
    trimmed="$(echo "$line" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
    [ -z "$trimmed" ] && continue
    case "$trimmed" in
        \#*) continue ;;
    esac

    if grep -Fxq "$trimmed" "$AUTH_KEYS"; then
        echo "  [i] Ключ уже присутствует в authorized_keys (пропуск)"
    else
        echo "$trimmed" >> "$AUTH_KEYS"
        echo "  [+] Ключ добавлен в authorized_keys"
    fi
done < "$KEY_FILE"

chown -R "$DEPLOY_USER:$DEPLOY_USER" "$SSH_DIR"

# 5. Каталог проекта
echo "[+] Настройка прав на каталог проекта '$PROJECT_DIR'..."
mkdir -p "$PROJECT_DIR"
chown -R "$DEPLOY_USER:$DEPLOY_USER" "$PROJECT_DIR"

# Если каталог проекта находится внутри домашней директории пользователя deploy,
# убеждаемся, что родительские подкаталоги также принадлежат deploy
if [[ "$PROJECT_DIR" == "$DEPLOY_HOME"/* ]]; then
    rel_path="${PROJECT_DIR#$DEPLOY_HOME/}"
    parent_part="${rel_path%%/*}"
    if [ -n "$parent_part" ] && [ -d "$DEPLOY_HOME/$parent_part" ]; then
        chown "$DEPLOY_USER:$DEPLOY_USER" "$DEPLOY_HOME/$parent_part"
    fi
fi

echo "[OK] Пользователь '$DEPLOY_USER' успешно настроен:"
echo "     - Домашний каталог: $DEPLOY_HOME"
echo "     - Группы: $(id -Gn "$DEPLOY_USER")"
echo "     - Файл ключей: $AUTH_KEYS (0600)"
echo "     - Каталог проекта: $PROJECT_DIR"
