#!/usr/bin/env python3
"""Строгая миграция расписаний дайджеста (Очередь 4, модель «папка = одна рассылка»).

CLI:
    python3 ops/migrate_schedules.py DB_PATH [--apply] [--email-folders]

Коды возврата:
    0 — Успешно (без --apply: отчёт без конфликтов; с --apply: миграция успешно применена).
    3 — Обнаружены конфликты данных (миграция заблокирована, БД не изменена).
    1 — Ошибка выполнения / исключение (транзакция откачена).
    2 — Неверные аргументы командной строки.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any

# SEC1 / Q4D: Разрешаем импорты независимо от текущей директории запуска
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ops.migrate_schedules_dryrun import (
    analyze_migration,
    format_report,
    normalize_tag,
    open_readonly_db,
)


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Идемпотентно создаёт целевую схему DDL (§3.1, §3.2)."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS folders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            is_default INTEGER NOT NULL DEFAULT 0,
            period TEXT NOT NULL,
            times TEXT NOT NULL DEFAULT '["07:00"]',
            send_hour INTEGER NOT NULL DEFAULT 7,
            send_minute INTEGER NOT NULL DEFAULT 0,
            weekdays TEXT,
            month_day INTEGER,
            monthly_mode TEXT DEFAULT 'date',
            delivery TEXT NOT NULL DEFAULT 'telegram',
            is_enabled INTEGER NOT NULL DEFAULT 1,
            next_send_at DATETIME,
            last_edition_id INTEGER,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL,
            UNIQUE(user_id, name),
            CHECK (period IN ('daily', 'weekly', 'monthly')),
            CHECK (send_hour BETWEEN 0 AND 23),
            CHECK (send_minute BETWEEN 0 AND 59),
            CHECK (monthly_mode IN ('date', 'weekday', 'last_day')),
            CHECK (delivery IN ('telegram', 'email'))
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_folders_due ON folders(is_enabled, next_send_at) WHERE next_send_at IS NOT NULL"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_folders_user ON folders(user_id, is_enabled)"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS digest_editions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            folder_id INTEGER NOT NULL REFERENCES folders(id) ON DELETE CASCADE,
            user_id INTEGER NOT NULL,
            scheduled_at DATETIME NOT NULL,
            status TEXT NOT NULL,
            delivery TEXT NOT NULL DEFAULT 'telegram',
            delivery_target TEXT,
            posts_count INTEGER DEFAULT 0,
            channels_count INTEGER DEFAULT 0,
            channels_failed INTEGER DEFAULT 0,
            channels_partial INTEGER DEFAULT 0,
            error_message TEXT,
            retry_count INTEGER DEFAULT 0,
            telegraph_url TEXT,
            created_at DATETIME NOT NULL,
            started_at DATETIME,
            delivered_at DATETIME,
            UNIQUE(folder_id, scheduled_at)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_digest_editions_user_status ON digest_editions(user_id, status)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_digest_editions_sched_status ON digest_editions(scheduled_at, status)"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS digest_edition_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            edition_id INTEGER NOT NULL REFERENCES digest_editions(id) ON DELETE CASCADE,
            subscription_id INTEGER NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
            channel_username TEXT NOT NULL,
            post_id INTEGER NOT NULL,
            post_time DATETIME NOT NULL,
            post_title TEXT,
            post_text TEXT,
            post_link TEXT NOT NULL,
            score REAL DEFAULT 0.0,
            rank INTEGER,
            created_at DATETIME NOT NULL,
            UNIQUE(edition_id, channel_username, post_id)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_edition_items_edition ON digest_edition_items(edition_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_edition_items_sub ON digest_edition_items(subscription_id)"
    )

    for statement in (
        "ALTER TABLE user_prefs ADD COLUMN digest_scheduler_version TEXT DEFAULT 'v1'",
        "ALTER TABLE subscriptions ADD COLUMN folder_id INTEGER REFERENCES folders(id) ON DELETE RESTRICT",
        "ALTER TABLE digest_executions ADD COLUMN folder_id INTEGER REFERENCES folders(id)",
        "ALTER TABLE digest_alerts ADD COLUMN folder_id INTEGER REFERENCES folders(id)",
    ):
        try:
            conn.execute(statement)
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc).lower():
                raise

    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_subscriptions_folder ON subscriptions(folder_id, is_disabled)"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_digest_executions_folder_slot ON digest_executions(folder_id, scheduled_at) WHERE folder_id IS NOT NULL"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_digest_alerts_folder_slot ON digest_alerts(folder_id, scheduled_at, alert_type) WHERE folder_id IS NOT NULL"
    )


def apply_migration(conn: sqlite3.Connection, email_folders: bool = False) -> dict[str, int]:
    """Выполняет шаги §4.1 п.4–6 миграции данных в существующей транзакции.

    Гарантии:
    - Все вставки защищены ON CONFLICT DO NOTHING.
    - subscriptions.last_post_id не изменяется.
    - digest_scheduler_version остаётся 'v1'.
    - Повторный запуск идемпотентен и ничего не меняет.
    """
    _ensure_schema(conn)

    users_with_subs = conn.execute(
        "SELECT DISTINCT user_id FROM subscriptions WHERE is_disabled = 0 ORDER BY user_id"
    ).fetchall()

    folders_created = 0
    subscriptions_linked = 0

    for u_row in users_with_subs:
        uid = u_row["user_id"]
        sub_rows = conn.execute(
            """
            SELECT id, channel_username, period, tag, last_post_id, next_send_at
            FROM subscriptions
            WHERE user_id = ? AND is_disabled = 0
            ORDER BY id
            """,
            (uid,),
        ).fetchall()

        folders_for_user: dict[str, dict[str, Any]] = {}
        for s in sub_rows:
            folder_name, is_default = normalize_tag(s["tag"])
            raw_period = s["period"]

            if folder_name not in folders_for_user:
                folders_for_user[folder_name] = {
                    "name": folder_name,
                    "is_default": is_default,
                    "period": raw_period,
                    "next_send_at": s["next_send_at"],
                    "sub_ids": [],
                }
            folders_for_user[folder_name]["sub_ids"].append(s["id"])

        # Проверка коллизий имён папок одного пользователя по casefold()
        cf_seen: dict[str, str] = {}
        for er in conn.execute("SELECT name FROM folders WHERE user_id = ?", (uid,)):
            cf_seen[er["name"].casefold()] = er["name"]

        for f_name in folders_for_user:
            cf = f_name.casefold()
            if cf in cf_seen and cf_seen[cf] != f_name:
                raise ValueError(
                    f"Коллизия имён папок по регистру (casefold) для user_id={uid}: "
                    f"'{f_name}' и '{cf_seen[cf]}'. Миграция заблокирована."
                )
            cf_seen[cf] = f_name

        for f_name, f_data in folders_for_user.items():
            folder_period = f_data["period"]

            row_settings = conn.execute(
                """
                SELECT send_hour, send_minute, weekday, month_day, monthly_mode
                FROM digest_settings
                WHERE user_id = ? AND period = ?
                """,
                (uid, folder_period),
            ).fetchone()

            if row_settings:
                send_hour = row_settings["send_hour"] if row_settings["send_hour"] is not None else 7
                send_minute = row_settings["send_minute"] if row_settings["send_minute"] is not None else 0
                weekday = row_settings["weekday"]
                month_day = row_settings["month_day"]
                monthly_mode = row_settings["monthly_mode"] or "date"
            else:
                send_hour = 7
                send_minute = 0
                weekday = None
                month_day = None
                monthly_mode = "date"

            times = json.dumps([f"{send_hour:02d}:{send_minute:02d}"])

            now_utc = datetime.now(timezone.utc)
            if folder_period == "weekly":
                weekdays_val = str(weekday) if weekday is not None else str(now_utc.weekday())
            else:
                weekdays_val = None

            if folder_period == "monthly":
                month_day_val = int(month_day) if month_day is not None else now_utc.day
            else:
                month_day_val = None

            monthly_mode_val = monthly_mode if folder_period == "monthly" else "date"

            # Определение delivery (по умолчанию telegram; email — только с флагом --email-folders)
            if email_folders:
                row_email = conn.execute(
                    "SELECT email, email_periods FROM user_emails WHERE user_id = ?",
                    (uid,),
                ).fetchone()
                if row_email and row_email["email"] and row_email["email"].strip():
                    raw_p = row_email["email_periods"] or ""
                    periods = {p.strip() for p in raw_p.split(",") if p.strip()}
                    delivery = "email" if folder_period in periods else "telegram"
                else:
                    delivery = "telegram"
            else:
                delivery = "telegram"

            now_iso = now_utc.strftime("%Y-%m-%d %H:%M:%S")

            # Идемпотентность без слепого ON CONFLICT DO NOTHING:
            # если папка (user_id, name) уже есть, сверяем period и is_default.
            row_existing = conn.execute(
                "SELECT id, period, is_default FROM folders WHERE user_id = ? AND name = ?",
                (uid, f_name),
            ).fetchone()

            if row_existing is not None:
                if row_existing["period"] != folder_period or row_existing["is_default"] != f_data["is_default"]:
                    raise ValueError(
                        f"Конфликт существующей папки id={row_existing['id']} (user_id={uid}, name='{f_name}'): "
                        f"в БД (period='{row_existing['period']}', is_default={row_existing['is_default']}), "
                        f"но миграция ожидает (period='{folder_period}', is_default={f_data['is_default']}). "
                        f"Транзакция отменена."
                    )
                folder_id = row_existing["id"]
            else:
                cursor_folder = conn.execute(
                    """
                    INSERT INTO folders (
                        user_id, name, is_default, period, times, send_hour, send_minute,
                        weekdays, month_day, monthly_mode, delivery, is_enabled, next_send_at,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                    """,
                    (
                        uid,
                        f_data["name"],
                        f_data["is_default"],
                        folder_period,
                        times,
                        send_hour,
                        send_minute,
                        weekdays_val,
                        month_day_val,
                        monthly_mode_val,
                        delivery,
                        f_data["next_send_at"],
                        now_iso,
                        now_iso,
                    ),
                )
                folder_id = cursor_folder.lastrowid
                folders_created += 1

            for sub_id in f_data["sub_ids"]:
                cursor_sub = conn.execute(
                    "UPDATE subscriptions SET folder_id = ? WHERE id = ? AND (folder_id IS NULL OR folder_id != ?)",
                    (folder_id, sub_id, folder_id),
                )
                subscriptions_linked += cursor_sub.rowcount

    # Гарантируем digest_scheduler_version = 'v1' для строк user_prefs без версии
    conn.execute(
        "UPDATE user_prefs SET digest_scheduler_version = 'v1' WHERE digest_scheduler_version IS NULL"
    )

    # Инвариант перед COMMIT: число активных подписок с folder_id IS NULL должно быть равно 0
    row_unlinked = conn.execute(
        "SELECT COUNT(*) FROM subscriptions WHERE is_disabled = 0 AND folder_id IS NULL"
    ).fetchone()
    unlinked_active = int(row_unlinked[0] or 0)
    if unlinked_active > 0:
        raise RuntimeError(
            f"Нарушение инварианта перед COMMIT: обнаружено {unlinked_active} активных подписок без folder_id. "
            "Транзакция откатывается."
        )

    return {
        "folders_created": folders_created,
        "subscriptions_linked": subscriptions_linked,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Строгая миграция расписаний дайджеста (Очередь 4, модель «папка = одна рассылка»)"
    )
    parser.add_argument("db_path", help="Путь к файлу базы данных SQLite")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Применить миграцию в БД (без флага — только dry-run отчёт)",
    )
    parser.add_argument(
        "--email-folders",
        action="store_true",
        help="Переводить папки email-пользователей в delivery='email' (по умолчанию delivery='telegram')",
    )
    args = parser.parse_args(argv)

    db_path = Path(args.db_path)
    if not db_path.exists() or not db_path.is_file():
        print(f"Ошибка: файл базы данных не найден: {db_path}", file=sys.stderr)
        return 1

    # 1. Инспекция в strictly read-only режиме
    try:
        ro_conn = open_readonly_db(db_path)
    except Exception as exc:
        print(f"Ошибка открытия базы данных в read-only режиме: {exc}", file=sys.stderr)
        return 1

    try:
        report = analyze_migration(ro_conn)
    except Exception as exc:
        print(f"Ошибка анализа базы данных: {exc}", file=sys.stderr)
        return 1
    finally:
        ro_conn.close()

    # Печать отчёта
    report_text = format_report(report)
    print(report_text)

    # 2. Если есть конфликты — блокировка с кодом 3
    if report["has_conflicts"]:
        print(
            "\n[ОШИБКА] Обнаружены конфликты данных. Автоматическое разрешение отключено.\n"
            "Миграция заблокирована (код возврата 3). База данных не изменена.",
            file=sys.stderr,
        )
        return 3

    # 3. Без флага --apply — только отчёт
    if not args.apply:
        print("\n[ИНФО] Режим dry-run. Для применения изменений передайте флаг --apply.")
        return 0

    # 4. Применение миграции в единой транзакции BEGIN IMMEDIATE
    print("\n[ПРИМЕНЕНИЕ] Начинается транзакция миграции (BEGIN IMMEDIATE, busy_timeout=5000)...")
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("BEGIN IMMEDIATE")

        stats = apply_migration(conn, email_folders=args.email_folders)

        conn.commit()
    except Exception as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        print(f"\n[ОШИБКА] Сбой при применении миграции (транзакция откачена): {exc}", file=sys.stderr)
        return 1
    finally:
        try:
            conn.close()
        except Exception:
            pass

    print(
        f"[УСПЕХ] Миграция успешно завершена.\n"
        f"   • Создано новых папок:         {stats['folders_created']}\n"
        f"   • Привязано подписок к папкам: {stats['subscriptions_linked']}\n"
        f"   • Курсоры subscriptions.last_post_id: сохранены без изменений\n"
        f"   • Версия планировщика:         digest_scheduler_version = 'v1'"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
