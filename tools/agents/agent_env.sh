#!/usr/bin/env bash
# Подключается через `.`: подгружает ключи агентов из файла вне репозитория, если переменная не задана вовсе.
# Вкладки терминала не наследуют окружение наблюдателя, а ~/.zshrc из скриптов не читается — поэтому отдельный файл.
# Формат файла (права 0600): строки `OPENROUTER_API_KEY=...`; выполняется не файл, а только разбор строк `ИМЯ=значение`.
# Переменная, заданная в окружении (даже пустая), файлом не перекрывается.
AGENT_ENV_FILE="${AGENT_ENV_FILE:-$HOME/.config/agent-tools/env}"

agent_env_load() {
  local name value
  [ -r "$AGENT_ENV_FILE" ] || return 0
  for name in "$@"; do
    [ -z "${!name+x}" ] || continue
    value="$(sed -n "s/^\(export[[:space:]]\{1,\}\)\{0,1\}${name}=//p" "$AGENT_ENV_FILE" | tail -n 1)"
    value="${value%\"}"; value="${value#\"}"; value="${value%\'}"; value="${value#\'}"
    [ -z "$value" ] || export "$name=$value"
  done
}
