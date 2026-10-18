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
from collections import defaultdict
from pathlib import Path
import sqlite3
import sys
from typing import Any
import urllib.parse

VALID_PERIODS = {"daily", "weekly", "monthly"}
VALID_MONTHLY_MODES = {"date", "weekday", "last_day"}
SYSTEM_FOLDER_NAMES_CASEFOLD = {"основное", "основной"}
DEFAULT_FOLDER_NAME = "Основное"


def normalize_tag(tag: str | None) -> tuple[str, int]:
    """Единая нормализация тега подписки в имя целевой папки и флаг is_default.

    Правила:
    - clean = (tag or "").strip()
    - пустой, а также совпадающий по casefold() с именем системной папки
      («Основное»/«Основной») -> в системную папку 'Основное', is_default=1.
    - иначе -> (clean, is_default=0).
    """
    clean = (tag or "").strip()
    if not clean or clean.casefold() in SYSTEM_FOLDER_NAMES_CASEFOLD:
        return (DEFAULT_FOLDER_NAME, 1)
    return (clean, 0)


def open_readonly_db(db_path: Path) -> sqlite3.Connection:
    """Открывает базу данных строго в режиме read-only через SQLite URI с экранированием."""
    if not db_path.is_file():
        raise FileNotFoundError(f"Файл базы данных не найден: {db_path}")

    # SEC1 / Q4D: строго read-only подключение через URI без разрешений на запись
    # Экранируем символы пути (? и #), чтобы они не воспринимались как URI query/fragment
    resolved_posix = db_path.resolve().as_posix()
    encoded_path = urllib.parse.quote(resolved_posix, safe="/:")
    uri = f"file:{encoded_path}?mode=ro"
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
            "zero_or_null_cursors": 0,
            "null_cursors": 0,
            "zero_cursors": 0,
        },
        "folders_detail": [],
        "conflicts": {
            "channel_in_multiple_tags": [],
            "channel_in_multiple_periods": [],
            "heterogeneous_period_in_folder": [],
            "time_discrepancy_in_folder": [],
            "empty_folders": [],
            "schedule_without_time": [],
            "invalid_schedule_params": [],
            "unconfirmed_email": [],
            "folder_name_case_collision": [],
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
    # Модель Q4S: email-подписки мигрируют в папки с delivery='email'
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

            if periods and not email:
                report["conflicts"]["unconfirmed_email"].append(
                    {
                        "user_id": uid,
                        "pending_email": pending or None,
                        "email_periods": sorted(periods),
                        "resolution": "Неподтверждённый email при указанных email_periods — конфликт (код 3)",
                    }
                )

    # 3. Анализ digest_settings и валидация параметров расписания
    digest_settings_map: dict[tuple[int, str], dict[str, Any]] = {}
    if _table_exists(conn, "digest_settings"):
        for row in conn.execute("SELECT * FROM digest_settings"):
            uid = row["user_id"]
            period = row["period"]
            sh = row["send_hour"]
            sm = row["send_minute"]
            weekday = row["weekday"]
            month_day = row["month_day"]
            monthly_mode = row["monthly_mode"]

            digest_settings_map[(uid, period)] = dict(row)
            report["legacy"]["digest_settings_rows"] += 1

            # Валидация периода
            if period not in VALID_PERIODS:
                report["conflicts"]["invalid_schedule_params"].append(
                    {
                        "user_id": uid,
                        "period": period,
                        "reason": f"Недопустимое значение period: '{period}' (ожидается daily, weekly, monthly)",
                    }
                )

            # Валидация часов и минут
            if sh is None or sm is None or not (0 <= sh <= 23) or not (0 <= sm <= 59):
                report["conflicts"]["schedule_without_time"].append(
                    {
                        "user_id": uid,
                        "period": period,
                        "send_hour": sh,
                        "send_minute": sm,
                        "reason": "Некорректное или отсутствующее время в digest_settings",
                        "resolution": "Будет применён дефолт из кода (07:00, текущий день недели/месяца, monthly_mode='date')",
                    }
                )

            # Валидация weekday
            if weekday is not None and not (0 <= weekday <= 6):
                report["conflicts"]["invalid_schedule_params"].append(
                    {
                        "user_id": uid,
                        "period": period,
                        "param": "weekday",
                        "value": weekday,
                        "reason": f"Недопустимое значение weekday={weekday} (ожидается 0..6)",
                    }
                )

            # Валидация month_day
            if month_day is not None and not (1 <= month_day <= 31):
                report["conflicts"]["invalid_schedule_params"].append(
                    {
                        "user_id": uid,
                        "period": period,
                        "param": "month_day",
                        "value": month_day,
                        "reason": f"Недопустимое значение month_day={month_day} (ожидается 1..31)",
                    }
                )

            # Валидация monthly_mode
            if monthly_mode is not None and monthly_mode not in VALID_MONTHLY_MODES:
                report["conflicts"]["invalid_schedule_params"].append(
                    {
                        "user_id": uid,
                        "period": period,
                        "param": "monthly_mode",
                        "value": monthly_mode,
                        "reason": f"Недопустимое значение monthly_mode='{monthly_mode}' (ожидается date, weekday, last_day)",
                    }
                )

    # 4. Анализ папок и расписаний по пользователям
    users_with_subs = conn.execute(
        "SELECT DISTINCT user_id FROM subscriptions WHERE is_disabled = 0 ORDER BY user_id"
    ).fetchall()

    total_folders = 0
    total_schedules = 0

    # Структуры для выявления конфликтов каналов и папок
    # (user_id, channel_username) -> список записей
    user_channel_subs: dict[tuple[int, str], list[dict[str, Any]]] = defaultdict(list)

    for u_row in users_with_subs:
        uid = u_row["user_id"]
        sub_rows = conn.execute(
            """
            SELECT id, channel_username, period, tag, last_post_id, next_send_at, is_paused
            FROM subscriptions
            WHERE user_id = ? AND is_disabled = 0
            ORDER BY id
            """,
            (uid,),
        ).fetchall()

        folders_for_user: dict[str, dict[str, Any]] = {}

        for s in sub_rows:
            folder_name, is_default = normalize_tag(s["tag"])
            sub_status = "paused" if s["is_paused"] else "active"

            raw_period = s["period"]
            channel_username = s["channel_username"]
            sub_id = s["id"]
            next_send_at = s["next_send_at"]

            user_channel_subs[(uid, channel_username)].append(
                {
                    "sub_id": sub_id,
                    "folder_name": folder_name,
                    "period": raw_period,
                    "next_send_at": next_send_at,
                    "last_post_id": s["last_post_id"],
                    "status": sub_status,
                }
            )

            # Валидация period подписки
            if raw_period is None or raw_period not in VALID_PERIODS:
                report["conflicts"]["invalid_schedule_params"].append(
                    {
                        "user_id": uid,
                        "sub_id": sub_id,
                        "channel_username": channel_username,
                        "period": raw_period,
                        "status": sub_status,
                        "reason": f"Подписка имеет недопустимый period={repr(raw_period)} (ожидается daily, weekly, monthly)",
                    }
                )

            if folder_name not in folders_for_user:
                folders_for_user[folder_name] = {
                    "name": folder_name,
                    "is_default": is_default,
                    "periods": set(),
                    "channel_count": 0,
                    "channels": set(),
                    "next_send_at_set": set(),
                    "statuses": set(),
                }
            folders_for_user[folder_name]["periods"].add(raw_period)
            folders_for_user[folder_name]["channel_count"] += 1
            folders_for_user[folder_name]["channels"].add(channel_username)
            folders_for_user[folder_name]["statuses"].add(sub_status)
            if next_send_at is not None:
                folders_for_user[folder_name]["next_send_at_set"].add(next_send_at)

            # Проверка наличия явных настроек в digest_settings (если period валидный)
            if raw_period in VALID_PERIODS and (uid, raw_period) not in digest_settings_map:
                report["conflicts"]["schedule_without_time"].append(
                    {
                        "user_id": uid,
                        "period": raw_period,
                        "send_hour": None,
                        "send_minute": None,
                        "reason": f"Нет явной записи в digest_settings для активной подписки '{raw_period}'",
                        "resolution": "Будет применён дефолт из кода (07:00, текущий день недели/месяца, monthly_mode='date')",
                    }
                )

        # Проверка коллизий имён папок по casefold() (теги, различающиеся только регистром)
        cf_groups: dict[str, list[str]] = defaultdict(list)
        for f_name in folders_for_user:
            cf_groups[f_name.casefold()].append(f_name)

        for cf_key, names in cf_groups.items():
            if len(names) > 1:
                all_statuses = set()
                for n in names:
                    all_statuses.update(folders_for_user[n]["statuses"])
                report["conflicts"]["folder_name_case_collision"].append(
                    {
                        "user_id": uid,
                        "names": sorted(names),
                        "statuses": sorted(all_statuses),
                        "reason": f"Теги пользователя различаются только регистром: {sorted(names)}",
                        "resolution": "Коллизия имён папок по casefold() — конфликт (код 3), запись не делается",
                    }
                )

        if _table_exists(conn, "folders"):
            existing_rows = conn.execute(
                "SELECT name FROM folders WHERE user_id = ?", (uid,)
            ).fetchall()
            for er in existing_rows:
                ex_name = er["name"]
                for f_name in folders_for_user:
                    if ex_name != f_name and ex_name.casefold() == f_name.casefold():
                        report["conflicts"]["folder_name_case_collision"].append(
                            {
                                "user_id": uid,
                                "names": sorted([ex_name, f_name]),
                                "statuses": sorted(folders_for_user[f_name]["statuses"]),
                                "reason": f"Имя папки '{f_name}' конфликтует с существующей папкой '{ex_name}' по регистру",
                                "resolution": "Коллизия имён папок по casefold() — конфликт (код 3), запись не делается",
                            }
                        )

        for f_name, f_info in folders_for_user.items():
            periods_set = f_info["periods"]
            subs_in_folder = len(f_info["channels"])

            # 1. Проверка однородности period в папке (включая папку "Основное", где tag IS NULL)
            # Разные периоды внутри одной папки или NULL — жесткий конфликт (код 3)
            has_hetero = (len(periods_set) > 1) or (None in periods_set)
            if has_hetero:
                report["conflicts"]["heterogeneous_period_in_folder"].append(
                    {
                        "user_id": uid,
                        "folder_name": f_name,
                        "periods": sorted([str(p) for p in periods_set]),
                        "statuses": sorted(f_info["statuses"]),
                        "channels_count": subs_in_folder,
                        "resolution": "Папка содержит неоднородный period (включая NULL) — требуется один period на папку (код 3)",
                    }
                )
                valid_candidates = [p for p in periods_set if p in VALID_PERIODS]
                folder_period = sorted(valid_candidates)[0] if valid_candidates else "daily"
            else:
                folder_period = list(periods_set)[0] if periods_set else "daily"

            # 2. Проверка расхождения времени внутри папки
            if len(f_info["next_send_at_set"]) > 1:
                report["conflicts"]["time_discrepancy_in_folder"].append(
                    {
                        "user_id": uid,
                        "folder_name": f_name,
                        "period": folder_period,
                        "statuses": sorted(f_info["statuses"]),
                        "channels_count": subs_in_folder,
                        "distinct_times": sorted(str(t) for t in f_info["next_send_at_set"]),
                        "resolution": "Подписки одной папки имеют разные next_send_at — расхождение времени (код 3)",
                    }
                )

            # 3. Определение доставки (Telegram или Email)
            # В модели Q4S email-подписки мигрируют в папки с delivery='email'
            user_email_info = user_emails_map.get(uid)
            if (
                user_email_info
                and user_email_info["email"]
                and folder_period in user_email_info["periods"]
            ):
                delivery = "email"
                report["summary"]["email_schedules"] += 1
            else:
                delivery = "telegram"
                report["summary"]["telegram_schedules"] += 1

            total_folders += 1
            total_schedules += 1  # Папка = одна рассылка

            # Определение времени в digest_settings для отчёта
            ds_entry = digest_settings_map.get((uid, folder_period))
            if ds_entry and ds_entry.get("send_hour") is not None and ds_entry.get("send_minute") is not None:
                sh = ds_entry["send_hour"]
                sm = ds_entry["send_minute"]
                time_str = f"{sh:02d}:{sm:02d}"
                has_time = True
            else:
                time_str = "нет (дефолт 07:00)"
                has_time = False

            report["folders_detail"].append(
                {
                    "user_id": uid,
                    "folder_name": f_name,
                    "is_default": 1 if f_info["is_default"] else 0,
                    "channels_count": subs_in_folder,
                    "period": folder_period,
                    "time": time_str,
                    "has_explicit_time": has_time,
                    "delivery": delivery,
                    "statuses": sorted(f_info["statuses"]),
                }
            )

    report["summary"]["folders_to_create"] = total_folders
    report["summary"]["schedules_to_create"] = total_schedules

    # 5. Анализ курсоров в legacy-таблице subscriptions
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
                    "resolution": "Курсор subscriptions.last_post_id равен NULL (вычитка с момента подписки)",
                }
            )

    # 6. Конфликты каналов: один канал в нескольких папках (тегах)
    for (uid, channel_username), subs in user_channel_subs.items():
        unique_folders = sorted({s["folder_name"] for s in subs})
        statuses = sorted({s["status"] for s in subs})
        if len(unique_folders) > 1:
            report["conflicts"]["channel_in_multiple_tags"].append(
                {
                    "user_id": uid,
                    "channel_username": channel_username,
                    "tags": unique_folders,
                    "statuses": statuses,
                    "resolution": "Канал принадлежит более чем одной папке — требуется выбор владельца",
                }
            )

        # Конфликт: дубли периодов одного канала (один канал в нескольких периодах)
        if len(subs) > 1:
            periods = [s["period"] for s in subs]
            report["conflicts"]["channel_in_multiple_periods"].append(
                {
                    "user_id": uid,
                    "channel_username": channel_username,
                    "periods": periods,
                    "statuses": statuses,
                    "resolution": "Один канал в нескольких периодах/подписках — конфликт уникальности (код 3)",
                }
            )

    # 7. Конфликт: пустые папки (теги только у отключённых подписок)
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
              WHERE s2.user_id = s1.user_id AND TRIM(s2.tag) = TRIM(s1.tag) AND s2.is_disabled = 0
          )
    """
    for r in conn.execute(disabled_tags_query).fetchall():
        tag_norm, is_def = normalize_tag(r["tag"])
        if is_def:
            # Если тег нормализуется в системную папку 'Основное', и у пользователя уже есть активные подписки в 'Основное'
            if any(fd["user_id"] == r["user_id"] and fd["is_default"] == 1 for fd in report["folders_detail"]):
                continue
        tag_clean = r["tag"].strip() if r["tag"] else ""
        report["conflicts"]["empty_folders"].append(
            {
                "user_id": r["user_id"],
                "tag": tag_clean,
                "reason": "Тег присутствует только у неактивных/отключённых подписок",
                "resolution": "Папка не будет создана при автоматической миграции",
                "status": "disabled",
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
    # Модель Q4S: неоднородный period внутри папки (в т.ч. tag IS NULL), дубли периодов,
    # расхождения времени, невалидные параметры, канал в нескольких папках, коллизии регистра — всё это конфликты (код 3).
    has_conflicts = bool(
        report["conflicts"]["channel_in_multiple_tags"]
        or report["conflicts"]["channel_in_multiple_periods"]
        or report["conflicts"]["heterogeneous_period_in_folder"]
        or report["conflicts"]["time_discrepancy_in_folder"]
        or report["conflicts"]["empty_folders"]
        or report["conflicts"]["schedule_without_time"]
        or report["conflicts"]["invalid_schedule_params"]
        or report["conflicts"]["unconfirmed_email"]
        or report["conflicts"]["folder_name_case_collision"]
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
    lines.append(f"   • Будет создано расписаний: {summary['schedules_to_create']} (папка = одна рассылка)")
    lines.append(f"     - Telegram-расписаний:   {summary['telegram_schedules']}")
    lines.append(f"     - Email-расписаний:      {summary['email_schedules']}")
    lines.append("   • Курсоры: один на подписку (subscriptions.last_post_id, таблица курсоров упразднена)")
    lines.append(
        f"   • Число подписок с курсором 0/NULL: {summary['zero_or_null_cursors']} "
        f"(NULL: {summary['null_cursors']}, 0: {summary['zero_cursors']})"
    )

    # 2. Таблица целевых папок (G1)
    lines.append("\n2. ЦЕЛЕВЫЕ ПАПКИ И РАСПИСАНИЯ:")
    folders_detail = report.get("folders_detail", [])
    if folders_detail:
        header = f"| {'user_id':<8} | {'папка':<15} | {'is_default':<10} | {'period':<8} | {'время (digest_settings)':<23} | {'delivery':<8} | {'число каналов':<13} |"
        sep = f"|{'-' * 10}|{'-' * 17}|{'-' * 12}|{'-' * 10}|{'-' * 25}|{'-' * 10}|{'-' * 15}|"
        lines.append(header)
        lines.append(sep)
        for fd in folders_detail:
            row_str = (
                f"| {fd['user_id']:<8} "
                f"| {fd['folder_name']:<15} "
                f"| {fd['is_default']:<10} "
                f"| {fd['period']:<8} "
                f"| {fd['time']:<23} "
                f"| {fd['delivery']:<8} "
                f"| {fd['channels_count']:<13} |"
            )
            lines.append(row_str)
    else:
        lines.append("   (нет активных папок для миграции)")

    # 3. Отдельно список папок, где в digest_settings нет времени, с подставляемым дефолтом
    no_time_folders = [fd for fd in folders_detail if not fd.get("has_explicit_time")]
    lines.append("\n3. ПАПКИ БЕЗ ЯВНОГО ВРЕМЕНИ В DIGEST_SETTINGS (ПОДСТАВЛЯЕТСЯ ДЕФОЛТ 07:00):")
    if no_time_folders:
        for fd in no_time_folders:
            lines.append(
                f"   • user={fd['user_id']} папка='{fd['folder_name']}' period={fd['period']} -> дефолт 07:00"
            )
    else:
        lines.append("   • (нет таких папок, у всех задано явное время в digest_settings)")

    lines.append("\n4. ОБНАРУЖЕННЫЕ КОНФЛИКТЫ ДАННЫХ:")
    conflicts = report["conflicts"]

    c1 = conflicts["channel_in_multiple_tags"]
    lines.append(f"   [1] Канал в нескольких тегах: {len(c1)}")
    for item in c1:
        statuses_str = ", ".join(item.get("statuses", ["active"]))
        lines.append(
            f"       - user={item['user_id']} channel=@{item['channel_username']} "
            f"теги={item['tags']} (статусы: {statuses_str}) -> {item['resolution']}"
        )

    c2 = conflicts["empty_folders"]
    lines.append(f"   [2] Пустые / неактивные теги: {len(c2)}")
    for item in c2:
        lines.append(
            f"       - user={item['user_id']} тег='{item['tag']}' "
            f"({item['reason']}, статус: disabled) -> {item['resolution']}"
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
    lines.append(f"   [5] Дублирование каналов в разных периодах (конфликт уникальности): {len(c5)}")
    for item in c5:
        statuses_str = ", ".join(item.get("statuses", ["active"]))
        lines.append(
            f"       - user={item['user_id']} channel=@{item['channel_username']} "
            f"periods={item['periods']} (статусы: {statuses_str}) -> {item['resolution']}"
        )

    c6 = conflicts.get("time_discrepancy_in_folder", [])
    lines.append(f"   [6] Расхождения времени у подписок одной папки: {len(c6)}")
    for item in c6:
        statuses_str = ", ".join(item.get("statuses", ["active"]))
        lines.append(
            f"       - user={item['user_id']} папка='{item['folder_name']}' period={item['period']} "
            f"(статусы: {statuses_str}) next_send_at={item['distinct_times']} -> {item['resolution']}"
        )

    c7 = conflicts.get("invalid_schedule_params", [])
    lines.append(f"   [7] Некорректные параметры расписания (weekday/month_day/monthly_mode/period): {len(c7)}")
    for item in c7:
        lines.append(
            f"       - user={item['user_id']} {item.get('param', item.get('period', ''))}: {item['reason']}"
        )

    c8 = conflicts.get("heterogeneous_period_in_folder", [])
    lines.append(f"   [8] Неоднородные периоды внутри папки (включая папку без тега): {len(c8)}")
    for item in c8:
        statuses_str = ", ".join(item.get("statuses", ["active"]))
        lines.append(
            f"       - user={item['user_id']} папка='{item['folder_name']}' "
            f"периоды={item['periods']} (статусы: {statuses_str}) "
            f"каналы={item['channels_count']} -> {item['resolution']}"
        )

    c9 = conflicts.get("folder_name_case_collision", [])
    lines.append(f"   [9] Коллизии имён папок по регистру (casefold): {len(c9)}")
    for item in c9:
        statuses_str = ", ".join(item.get("statuses", ["active"]))
        lines.append(
            f"       - user={item['user_id']} папки={item['names']} "
            f"(статусы: {statuses_str}) -> {item['resolution']}"
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
    lines.append("\n5. ЧТО ОСТАНЕТСЯ КАК LEGACY:")
    lines.append(f"   • Записей digest_executions (история сбоев/успехов): {legacy['digest_executions_count']}")
    lines.append(f"   • Записей digest_alerts (технические алерты):       {legacy['digest_alerts_count']}")
    lines.append(f"   • Строк digest_settings (сохраняются для отката):   {legacy['digest_settings_rows']}")
    lines.append(f"   • Подписок с колонкой subscriptions.period:         {legacy['legacy_subscriptions_period_count']}")
    lines.append(f"   • Пользователей с user_emails.email_periods:        {legacy['user_email_periods_count']}")

    lines.append("\n" + "=" * 72)
    if report["has_conflicts"]:
        lines.append(" ВЕРДИКТ: ОБНАРУЖЕНЫ КОНФЛИКТЫ (КОД ВЫХОДА 3)")
        lines.append(" Конфликты показаны в отчёте; автоматическое разрешение не выполняется.")
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
    except Exception as exc:
        print(f"Ошибка анализа схемы и данных базы: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()

    output = format_report(report)
    print(output)

    return 3 if report["has_conflicts"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
