#!/usr/bin/env bash
# Прогон тестов со сдвинутыми часами: ищет «бомбы замедленного действия» — тесты, которые сравнивают
# жёстко заданную дату с реальным «сейчас» и однажды начинают падать сами по себе.
#
#   tools/agents/time_shift_tests.sh [сдвиги…]        # по умолчанию: +1d +7d +30d +365d -30d
#   TEST_CMD="pytest -q" tools/agents/time_shift_tests.sh +30d
#
# Нужен libfaketime (macOS: brew install libfaketime). Python должен быть не системным
# (SIP не даёт подгружать библиотеки в /usr/bin/python3) — venv на Homebrew/pyenv подходит.
set -uo pipefail
LIB="${FAKETIME_LIB:-$(ls /opt/homebrew/lib/faketime/libfaketime.1.dylib /usr/local/lib/faketime/libfaketime.1.dylib /usr/lib/x86_64-linux-gnu/faketime/libfaketime.so.1 2>/dev/null | head -1)}"
[ -n "$LIB" ] || { echo "libfaketime не найден (brew install libfaketime)"; exit 2; }
TEST_CMD="${TEST_CMD:-${PYTHON:-python3} -m unittest discover -s tests}"
[ $# -gt 0 ] || set -- +1d +7d +30d +365d -30d
status=0
for shift in "$@"; do
  if [[ "$LIB" == *.dylib ]]; then
    out=$(DYLD_INSERT_LIBRARIES="$LIB" DYLD_FORCE_FLAT_NAMESPACE=1 FAKETIME="$shift" bash -c "$TEST_CMD" 2>&1)
  else
    out=$(LD_PRELOAD="$LIB" FAKETIME="$shift" bash -c "$TEST_CMD" 2>&1)
  fi
  printf '%s: %s\n' "$shift" "$(printf '%s\n' "$out" | tail -1)"
  failed=$(printf '%s\n' "$out" | grep -E '^(FAIL|ERROR):' | sort -u)
  if [ -n "$failed" ]; then status=1; printf '%s\n' "$failed" | sed 's/^/  /'; fi
done
exit $status
