#!/usr/bin/env bash
# Подключается через `.`: dsh_find печатает путь к исполняемому файлу dsh (DeepSeek Harness CLI) или завершается с кодом 1.
# Порядок: $DSH_BIN → dsh в PATH → самый свежий установленный через npx (~/.npm/_npx/*/node_modules/.bin/dsh).
dsh_find() {
  local candidate newest="" path
  if [ -n "${DSH_BIN:-}" ]; then [ -x "$DSH_BIN" ] && { printf '%s\n' "$DSH_BIN"; return 0; }; return 1; fi
  candidate="$(command -v dsh 2>/dev/null)" && { printf '%s\n' "$candidate"; return 0; }
  for path in "$HOME"/.npm/_npx/*/node_modules/.bin/dsh; do
    [ -x "$path" ] || continue
    if [ -z "$newest" ] || [ "$path" -nt "$newest" ]; then newest="$path"; fi
  done
  [ -n "$newest" ] && { printf '%s\n' "$newest"; return 0; }
  return 1
}
