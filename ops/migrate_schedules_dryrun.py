#!/usr/bin/env python3
"""Read-only dry-run отчёт миграции расписаний дайджеста (Очередь 4).

Инспектирует существующую базу данных в режиме strictly read-only:
    python3 ops/migrate_schedules_dryrun.py <путь к копии БД>

Коды возврата:
    0 — Конфликтов данных не обнаружено, миграция готова к выполнению.
    3 — Обнаружены конфликты данных (список приводится в отчёте).
    1 — Ошибка доступа к файлу базы данных или структура повреждена.
    2 — Неверные аргументы командной строки.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sqlite3
import sys
from typing import Any


def open_readonly_db(db_path: Path) -> sqlite3.Connection:
    """Открывает базу данных строго в режиме read-only через SQLite URI."""
    if not db_path.is_file():
        raise FileNotFoundError(f"Файл базы данных не найден: {db_path}")

    # SEC1 / Q4D: строго read-only подключение через URI без разрешений на запись
    uri = f"file:{db_path.resolve().as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return bool(row)


def analyze_migration(conn: sqlite3.Connection) -> dict[str, Any]:
    """Анализирует текущее состояние данных и прогнозирует результат миграции."""
    report: dict[str, Any] = {
        "summary": {
            "total_users": 0,
            "active_subscriptions": 0,
            "unique_channels": 0,
            "folders_to_create": 0,
            "schedules_to_create": 0,
            "telegram_schedules": 0,
            "email_schedules": 0,
            "cursors_to_create": 0,
            "zero_or_null_cursors": 0,
            "null_cursors": 0,
            "zero_cursors": 0,
        },
        "folders_detail": [],
        "conflicts": {
            "channel_in_multiple_tags": [],
            "empty_folders": [],
            "schedule_without_time": [],
            "unconfirmed_email": [],
            "channel_in_multiple_periods": [],
        },
        "warnings": {
            "null_cursors": [],
        },
        "legacy": {
            "digest_executions_count": 0,
            "digest_alerts_count": 0,
            "digest_settings_rows": 0,
            "legacy_subscriptions_period_count": 0,
            "user_email_periods_count": 0,
        },
        "has_conflicts": False,
    }

    if not _table_exists(conn, "subscriptions"):
        return report

    # 1. Базовые показатели пользователей и подписок
    row_subs = conn.execute(
        """
        SELECT
            COUNT(DISTINCT user_id) as users_count,
            COUNT(*) as total_active,
            COUNT(DISTINCT channel_username) as unique_channels
        FROM subscriptions
        WHERE is_disabled = 0
        """
    ).fetchone()

    report["summary"]["total_users"] = int(row_subs["users_count"] or 0)
    report["summary"]["active_subscriptions"] = int(row_subs["total_active"] or 0)
    report["summary"]["unique_channels"] = int(row_subs["unique_channels"] or 0)

    # 2. Анализ email-доставки пользователей
    user_emails_map: dict[int, dict[str, Any]] = {}
    if _table_exists(conn, "user_emails"):
        for row in conn.execute(
            "SELECT user_id, email, email_periods, pending_email FROM user_emails"
        ):
            uid = row["user_id"]
            email = (row["email"] or "").strip()
            pending = (row["pending_email"] or "").strip()
            raw_periods = (row["email_periods"] or "").strip()
            periods = {p.strip() for p in raw_periods.split(",") if p.strip()}
            user_emails_map[uid] = {
                "email": email,
                "pending_email": pending,
                "periods": periods,
            }
            if raw_periods:
                report["legacy"]["user_email_periods_count"] += 1

            # Конфликт 1(е): Почта без подтверждения
            if periods and not email:
                report["conflicts"]["unconfirmed_email"].append(
                    {
                        "user_id": uid,
                        "pending_email": pending or None,
                        "email_periods": sorted(periods),
                        "resolution": "Расписания будут переведены на delivery='telegram' (email не подтверждён)",
                    }
                )

    # 3. Анализ digest_settings
    digest_settings_map: dict[tuple[int, str], dict[str, Any]] = {}
    if _table_exists(conn, "digest_settings"):
        for row in conn.execute("SELECT * FROM digest_settings"):
            uid = row["user_id"]
            period = row["period"]
            sh = row["send_hour"]
            sm = row["send_minute"]
            digest_settings_map[(uid, period)] = dict(row)
            report["legacy"]["digest_settings_rows"] += 1

            # Конфликт 1(е): Расписание с невалидным или отсутствующим временем
            if sh is None or sm is None or not (0 <= sh <= 23) or not (0 <= sm <= 59):
                report["conflicts"]["schedule_without_time"].append(
                    {
                        "user_id": uid,
                        "period": period,
                        "send_hour": sh,
                        "send_minute": sm,
                        "reason": "Некорректное или отсутствующее время в digest_settings",
                        "resolution": "Будет применён системный дефолт (07:00 / 09:00 / 10:00)",
                    }
                )

    # 4. Анализ папок и расписаний по пользователям
    users_with_subs = conn.execute(
        "SELECT DISTINCT user_id FROM subscriptions WHERE is_disabled = 0 ORDER BY user_id"
    ).fetchall()

    total_folders = 0
    total_schedules = 0
    telegram_scheds = 0
    email_scheds = 0
    total_cursors = 0

    for u_row in users_with_subs:
        uid = u_row["user_id"]
        sub_rows = conn.execute(
            """
            SELECT id, channel_username, period, tag, last_post_id
            FROM subscriptions
            WHERE user_id = ? AND is_disabled = 0
            ORDER BY id
            """,
            (uid,),
        ).fetchall()

        folders_for_user: dict[str, dict[str, Any]] = {}

        for s in sub_rows:
            tag_raw = s["tag"]
            tag_clean = tag_raw.strip() if isinstance(tag_raw, str) else ""
            folder_name = tag_clean if tag_clean else "Основное"
            is_default = (folder_name == "Основное")

            if folder_name not in folders_for_user:
                folders_for_user[folder_name] = {
                    "name": folder_name,
                    "is_default": is_default,
                    "periods": set(),
                    "channel_count": 0,
                    "channels": set(),
                }
            period = s["period"] or "daily"
            folders_for_user[folder_name]["periods"].add(period)
            folders_for_user[folder_name]["channel_count"] += 1
            folders_for_user[folder_name]["channels"].add(s["channel_username"])

            # Проверка: есть ли для периода настройки в digest_settings
            if (uid, period) not in digest_settings_map:
                # Мягкий конфликт: отсутствие явной строки digest_settings
                report["conflicts"]["schedule_without_time"].append(
                    {
                        "user_id": uid,
                        "period": period,
                        "send_hour": None,
                        "send_minute": None,
                        "reason": f"Нет явной записи в digest_settings для активной подписки '{period}'",
                        "resolution": "Будет создан слот с системным временем по умолчанию",
                    }
                )

        u_email_info = user_emails_map.get(uid, {"email": "", "periods": set()})
        confirmed_email = u_email_info["email"]
        email_periods = u_email_info["periods"]

        for f_name, f_info in folders_for_user.items():
            total_folders += 1
            f_periods = f_info["periods"] or {"daily"}
            num_f_scheds = len(f_periods)
            for p in f_periods:
                total_schedules += 1
                if confirmed_email and (p in email_periods):
                    email_scheds += 1
                else:
                    telegram_scheds += 1

            subs_in_folder = len(f_info["channels"])
            f_cursors = num_f_scheds * subs_in_folder
            total_cursors += f_cursors

            report["folders_detail"].append(
                {
                    "user_id": uid,
                    "folder_name": f_name,
                    "is_default": f_info["is_default"],
                    "channels_count": subs_in_folder,
                    "schedules": sorted(f_periods),
                    "cursors_count": f_cursors,
                }
            )

    report["summary"]["folders_to_create"] = total_folders
    report["summary"]["schedules_to_create"] = total_schedules
    report["summary"]["telegram_schedules"] = telegram_scheds
    report["summary"]["email_schedules"] = email_scheds
    report["summary"]["cursors_to_create"] = total_cursors

    # Анализ курсоров в legacy-таблице subscriptions
    cursor_stats = conn.execute(
        """
        SELECT
            SUM(CASE WHEN last_post_id IS NULL THEN 1 ELSE 0 END) as null_count,
            SUM(CASE WHEN last_post_id = 0 THEN 1 ELSE 0 END) as zero_count
        FROM subscriptions
        WHERE is_disabled = 0
        """
    ).fetchone()

    null_cnt = int(cursor_stats["null_count"] or 0)
    zero_cnt = int(cursor_stats["zero_count"] or 0)
    report["summary"]["zero_or_null_cursors"] = null_cnt + zero_cnt
    report["summary"]["null_cursors"] = null_cnt
    report["summary"]["zero_cursors"] = zero_cnt

    if null_cnt > 0:
        null_rows = conn.execute(
            """
            SELECT id, user_id, channel_username
            FROM subscriptions
            WHERE is_disabled = 0 AND last_post_id IS NULL
            ORDER BY user_id, id
            """
        ).fetchall()
        for nr in null_rows:
            report["warnings"]["null_cursors"].append(
                {
                    "sub_id": nr["id"],
                    "user_id": nr["user_id"],
                    "channel_username": nr["channel_username"],
                    "resolution": "Курсор digest_schedule_cursors будет инициализирован как NULL (backfill при первом запуске)",
                }
            )

    # 5. Конфликт 1(е): Канал в нескольких тегах
    multiple_tags_rows = conn.execute(
        """
        SELECT
            user_id,
            channel_username,
            COUNT(DISTINCT COALESCE(tag, '')) as tag_count,
            GROUP_CONCAT(DISTINCT COALESCE(tag, '<без тега>')) as tags_list
        FROM subscriptions
        WHERE is_disabled = 0
        GROUP BY user_id, channel_username
        HAVING tag_count > 1
        """
    ).fetchall()

    for r in multiple_tags_rows:
        tags = [t.strip() for t in (r["tags_list"] or "").split(",") if t.strip()]
        report["conflicts"]["channel_in_multiple_tags"].append(
            {
                "user_id": r["user_id"],
                "channel_username": r["channel_username"],
                "tags": tags,
                "resolution": "Канал будет привязан к папке старшей/активной подписки, остальные теги отброшены",
            }
        )

    # 6. Дублирование подписки одного канала в разных периодах (с одним тегом)
    multiple_periods_rows = conn.execute(
        """
        SELECT
            user_id,
            channel_username,
            COUNT(*) as sub_count,
            GROUP_CONCAT(period) as periods_list
        FROM subscriptions
        WHERE is_disabled = 0
        GROUP BY user_id, channel_username
        HAVING sub_count > 1
        """
    ).fetchall()

    for r in multiple_periods_rows:
        periods = [p.strip() for p in (r["periods_list"] or "").split(",") if p.strip()]
        report["conflicts"]["channel_in_multiple_periods"].append(
            {
                "user_id": r["user_id"],
                "channel_username": r["channel_username"],
                "periods": periods,
                "resolution": "Подписки будут объединены в одну, курсор last_post_id = MAX(last_post_id)",
            }
        )

    # 7. Конфликт 1(е): Пустые папки
    # Теги, которые фигурируют только у отключённых подписок (is_disabled = 1)
    # или пустые строки тегов
    disabled_tags_query = """
        SELECT DISTINCT user_id, tag
        FROM subscriptions
        WHERE tag IS NOT NULL AND TRIM(tag) != ''
          AND user_id NOT IN (
              SELECT DISTINCT user_id FROM subscriptions WHERE is_disabled = 0
          )
        UNION
        SELECT DISTINCT user_id, tag
        FROM subscriptions s1
        WHERE tag IS NOT NULL AND TRIM(tag) != ''
          AND NOT EXISTS (
              SELECT 1 FROM subscriptions s2
              WHERE s2.user_id = s1.user_id AND s2.tag = s1.tag AND s2.is_disabled = 0
          )
    """
    for r in conn.execute(disabled_tags_query).fetchall():
        report["conflicts"]["empty_folders"].append(
            {
                "user_id": r["user_id"],
                "tag": r["tag"],
                "reason": "Тег присутствует только у неактивных/отключённых подписок",
                "resolution": "Папка не будет создана при автоматической миграции",
            }
        )

    # 8. Legacy статистика
    if _table_exists(conn, "digest_executions"):
        row_exec = conn.execute("SELECT COUNT(*) FROM digest_executions").fetchone()
        report["legacy"]["digest_executions_count"] = int(row_exec[0] or 0)

    if _table_exists(conn, "digest_alerts"):
        row_alerts = conn.execute("SELECT COUNT(*) FROM digest_alerts").fetchone()
        report["legacy"]["digest_alerts_count"] = int(row_alerts[0] or 0)

    report["legacy"]["legacy_subscriptions_period_count"] = report["summary"]["active_subscriptions"]

    # Определение общего статуса конфликтов
    has_conflicts = bool(
        report["conflicts"]["channel_in_multiple_tags"]
        or report["conflicts"]["empty_folders"]
        or report["conflicts"]["schedule_without_time"]
        or report["conflicts"]["unconfirmed_email"]
    )
    report["has_conflicts"] = has_conflicts

    return report


def format_report(report: dict[str, Any]) -> str:
    """Форматирует человекочитаемый текстовый отчёт миграции."""
    lines: list[str] = []
    lines.append("=" * 72)
    lines.append(" DRY-RUN ОТЧЁТ МИГРАЦИИ РАСПИСАНИЙ ДАЙДЖЕСТА (ОЧЕРЕДЬ 4)")
    lines.append("=" * 72)

    summary = report["summary"]
    lines.append("\n1. ИТОГИ ТРАНСФОРМАЦИИ:")
    lines.append(f"   • Активных пользователей: {summary['total_users']}")
    lines.append(f"   • Активных подписок:      {summary['active_subscriptions']}")
    lines.append(f"   • Уникальных каналов:     {summary['unique_channels']}")
    lines.append(f"   • Будет создано папок:    {summary['folders_to_create']}")
    lines.append(f"   • Будет создано расписаний: {summary['schedules_to_create']}")
    lines.append(f"     - Telegram-расписаний:   {summary['telegram_schedules']}")
    lines.append(f"     - Email-расписаний:      {summary['email_schedules']}")
    lines.append(
        f"   • курсоры: будет создано {summary['cursors_to_create']} записей digest_schedule_cursors (расписания × подписки папки)"
    )
    lines.append(
        f"   • Число подписок с курсором 0/NULL: {summary['zero_or_null_cursors']} "
        f"(NULL: {summary['null_cursors']}, 0: {summary['zero_cursors']})"
    )

    lines.append("\n2. ОБНАРУЖЕННЫЕ КОНФЛИКТЫ ДАННЫХ:")
    conflicts = report["conflicts"]

    c1 = conflicts["channel_in_multiple_tags"]
    lines.append(f"   [1] Канал в нескольких тегах: {len(c1)}")
    for item in c1:
        lines.append(
            f"       - user={item['user_id']} channel=@{item['channel_username']} "
            f"теги={item['tags']} -> {item['resolution']}"
        )

    c2 = conflicts["empty_folders"]
    lines.append(f"   [2] Пустые / неактивные теги: {len(c2)}")
    for item in c2:
        lines.append(
            f"       - user={item['user_id']} тег='{item['tag']}' "
            f"({item['reason']}) -> {item['resolution']}"
        )

    c3 = conflicts["schedule_without_time"]
    lines.append(f"   [3] Расписания без явного времени: {len(c3)}")
    for item in c3:
        lines.append(
            f"       - user={item['user_id']} period={item['period']} "
            f"({item['reason']}) -> {item['resolution']}"
        )

    c4 = conflicts["unconfirmed_email"]
    lines.append(f"   [4] Почта без подтверждения: {len(c4)}")
    for item in c4:
        lines.append(
            f"       - user={item['user_id']} email_periods={item['email_periods']} "
            f"pending={item['pending_email']} -> {item['resolution']}"
        )

    c5 = conflicts["channel_in_multiple_periods"]
    lines.append(f"   [*] Дублирование каналов в разных периодах (схлопывание): {len(c5)}")
    for item in c5:
        lines.append(
            f"       - user={item['user_id']} channel=@{item['channel_username']} "
            f"periods={item['periods']} -> {item['resolution']}"
        )

    warnings = report.get("warnings", {})
    null_subs = warnings.get("null_cursors", [])
    if null_subs:
        lines.append(f"   [!] Предупреждение: у {len(null_subs)} подписок в legacy-таблице курсор NULL:")
        for item in null_subs:
            lines.append(
                f"       - sub_id={item['sub_id']} user={item['user_id']} channel=@{item['channel_username']} -> {item['resolution']}"
            )

    legacy = report["legacy"]
    lines.append("\n3. ЧТО ОСТАНЕТСЯ КАК LEGACY:")
    lines.append(f"   • Записей digest_executions (история сбоев/успехов): {legacy['digest_executions_count']}")
    lines.append(f"   • Записей digest_alerts (технические алерты):       {legacy['digest_alerts_count']}")
    lines.append(f"   • Строк digest_settings (сохраняются для отката):   {legacy['digest_settings_rows']}")
    lines.append(f"   • Подписок с колонкой subscriptions.period:         {legacy['legacy_subscriptions_period_count']}")
    lines.append(f"   • Пользователей с user_emails.email_periods:        {legacy['user_email_periods_count']}")

    lines.append("\n" + "=" * 72)
    if report["has_conflicts"]:
        lines.append(" ВЕРДИКТ: ОБНАРУЖЕНЫ КОНФЛИКТЫ (КОД ВЫХОДА 3)")
        lines.append(" Рекомендуется сверить правила разрешения перед запуском T20.")
    else:
        lines.append(" ВЕРДИКТ: ЧИСТО, КОНФЛИКТОВ НЕ ОБНАРУЖЕНО (КОД ВЫХОДА 0)")
    lines.append("=" * 72)

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Dry-run отчёт миграции расписаний дайджеста (Очередь 4, strictly read-only)"
    )
    parser.add_argument("db_path", help="Путь к файлу копии базы данных SQLite")
    args = parser.parse_args(argv)

    db_path = Path(args.db_path)
    if not db_path.exists():
        print(f"Ошибка: файл базы данных не существует: {db_path}", file=sys.stderr)
        return 1

    try:
        conn = open_readonly_db(db_path)
    except Exception as exc:
        print(f"Ошибка открытия базы данных в read-only режиме: {exc}", file=sys.stderr)
        return 1

    try:
        report = analyze_migration(conn)
    finally:
        conn.close()

    output = format_report(report)
    print(output)

    return 3 if report["has_conflicts"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
