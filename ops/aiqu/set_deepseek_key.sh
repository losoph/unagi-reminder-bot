#!/usr/bin/env bash
# Замена ключа DeepSeek на проде владельцем — с его машины, без вывода ключа на экран, в историю shell и в логи.
#   ops/aiqu/set_deepseek_key.sh            # спросит ключ скрыто
# Ключ идёт на сервер через stdin ssh; там .env копируется в .env.bak.<время>, строка DEEPSEEK_API_KEY заменяется,
# контейнер пересоздаётся (env_file читается только при создании), затем бот изнутри контейнера проверяет ключ
# запросом GET /models и печатает только HTTP-код: 200 — ключ рабочий, 401 — недействителен.
set -euo pipefail
SSH_HOST="${PROD_SSH:-aiqu}"
PROD_DIR="${PROD_DIR:-projects/unagi-reminder-bot}"
CONTAINER="${PROD_CONTAINER:-unagi-bot}"

IFS= read -rs -p "DEEPSEEK_API_KEY (ввод скрыт): " key; echo
[ -n "$key" ] || { echo "пустой ключ — ничего не меняю" >&2; exit 2; }
case "$key" in *[[:space:]]*) echo "в ключе пробел или перевод строки — проверь копирование" >&2; exit 2 ;; esac

# stdin ssh: первая строка — ключ (её читает `read` удалённого bash), дальше — текст скрипта для `bash -s`.
# `read` из канала читает побайтно и не забирает лишнего; команды скрипта, которым stdin не нужен, получают /dev/null.
{ printf '%s\n' "$key"; cat <<'REMOTE'
set -euo pipefail
cd "$PROD_DIR"
key="$DSK"; unset DSK
cp -p .env ".env.bak.$(date +%Y%m%d-%H%M%S)"
tmp="$(mktemp .env.new.XXXXXX)"
grep -v '^DEEPSEEK_API_KEY=' .env > "$tmp" || true
printf 'DEEPSEEK_API_KEY=%s\n' "$key" >> "$tmp"
chmod --reference=.env "$tmp"; chown --reference=.env "$tmp"
mv "$tmp" .env
unset key
# Метка версии ставится из окружения при создании контейнера (docker-compose.yml) — без неё prod_check поднимет ложный ALERT.
export GIT_SHA="$(git rev-parse --short HEAD)"
docker compose up -d --force-recreate </dev/null >/dev/null 2>&1
sleep 15
docker exec -i "$CONTAINER" python - <<'PY'
import os, urllib.error, urllib.request
base = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
req = urllib.request.Request(base + "/models", headers={"Authorization": "Bearer " + os.environ.get("DEEPSEEK_API_KEY", "")})
try:
    print("DeepSeek: HTTP", urllib.request.urlopen(req, timeout=20).status)
except urllib.error.HTTPError as exc:
    print("DeepSeek: HTTP", exc.code)
except Exception as exc:  # сеть/таймаут — без текста ответа, в нём может быть эхо заголовков
    print("DeepSeek: нет ответа", type(exc).__name__)
PY
REMOTE
} | ssh -o BatchMode=yes "$SSH_HOST" "PROD_DIR='$PROD_DIR' CONTAINER='$CONTAINER' bash -c 'IFS= read -r DSK; export DSK; exec bash -s'"
unset key
echo "готово; старый .env сохранён на сервере как .env.bak.<время>"
