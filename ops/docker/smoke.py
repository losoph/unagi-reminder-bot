"""Смоук-проверка образа под боевыми ограничениями контейнера (запускается в CI: .github/workflows/deploy.yml).

Контейнер стартует с теми же флагами, что в docker-compose.yml (read-only, cap_drop ALL, no-new-privileges),
точка входа сбрасывает права до пользователя bot, после чего выполняется этот файл. Любое расхождение — код 1.
"""
import os
import sys
import tempfile
from pathlib import Path

BOT_ID = 10001
# Скрипт лежит в ops/docker, а модули приложения — в корне образа.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'OK  ' if ok else 'FAIL'} {name}{': ' + detail if detail and not ok else ''}")
    if not ok:
        failures.append(name)


def proc_status() -> dict[str, str]:
    fields = {}
    for line in Path("/proc/self/status").read_text().splitlines():
        key, _, value = line.partition(":")
        fields[key] = value.strip()
    return fields


def can_write(path: str) -> bool:
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("x")
        os.remove(path)
        return True
    except OSError:
        return False


status = proc_status()
check("uid/gid бота", os.getuid() == BOT_ID and os.getgid() == BOT_ID, f"{os.getuid()}/{os.getgid()}")
check("нет capabilities", set(status.get("CapEff", "")) <= {"0"} and set(status.get("CapPrm", "")) <= {"0"}, status.get("CapEff", ""))
check("no-new-privs", status.get("NoNewPrivs") == "1", status.get("NoNewPrivs", ""))
check("код приложения только для чтения", not can_write("/app/smoke-probe"))
check("/tmp доступен для записи", can_write(os.path.join(tempfile.gettempdir(), "smoke-probe")))

import data.database as db  # DB_PATH берётся из окружения контейнера

db.init_db()
check("БД создаётся и пишется в storage", Path(db.DB_PATH).exists() and oct(Path(db.DB_PATH).stat().st_mode & 0o777) == "0o600", db.DB_PATH)

smtp_path = os.getenv("MSMTPRC_PATH", "")
if smtp_path and Path("/run/secrets/msmtprc").exists():
    import email_digest

    check("SMTP-конфиг доступен боту и включает email", email_digest.is_enabled(), smtp_path)
    check("исходный msmtprc боту недоступен", not os.access("/run/secrets/msmtprc", os.R_OK))

import main  # noqa: F401  (импорт всего приложения под ограничениями; BOT_TOKEN в окружении фиктивный)

check("импорт main", True)
sys.exit(1 if failures else 0)
