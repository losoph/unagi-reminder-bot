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

PERIOD_ORDER = {"daily": 1, "weekly": 2, "monthly": 3}
PERIOD_SUFFIX = {
    "daily": "день",
    "weekly": "неделя",
    "monthly": "месяц",
}


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


def normalize_channel_username(channel_username: str | None) -> str:
    """Нормализует username канала (убирает @, пробелы и переводит в нижний регистр)."""
    if isinstance(channel_username, str):
        return channel_username.strip().lstrip("@").lower()
    return ""



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


def plan_user_folders(
    conn: sqlite3.Connection,
    uid: int,
    split_by_period: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Планирует целевые папки для пользователя uid.

    Возвращает:
        (target_folders, split_records, collisions)
    где:
        target_folders: список dict целевых папок:
            - folder_name: str
            - is_default: int (0 или 1)
            - period: str
            - subs: list[sqlite3.Row]
            - is_split: bool
            - source_tag: str
            - all_periods: list[str] (если не разбивалось)
        split_records: список dict с информацией о разбиении тегов (было -> стало)
        collisions: список обнаруженных casefold-коллизий
    """
    sub_rows = conn.execute(
        """
        SELECT id, channel_username, period, tag, last_post_id, next_send_at, is_paused
        FROM subscriptions
        WHERE user_id = ? AND is_disabled = 0
        ORDER BY id
        """,
        (uid,),
    ).fetchall()

    if not sub_rows:
        return [], [], []

    # Все исходные теги пользователя в subscriptions (для поиска коллизий со сгенерированными именами)
    existing_tags_rows = conn.execute(
        """
        SELECT DISTINCT tag FROM subscriptions
        WHERE user_id = ? AND tag IS NOT NULL AND TRIM(tag) != ''
        """,
        (uid,),
    ).fetchall()
    user_existing_tags_clean = {r["tag"].strip() for r in existing_tags_rows if r["tag"] and r["tag"].strip()}

    tag_groups: dict[str, list[sqlite3.Row]] = defaultdict(list)
    tag_defaults: dict[str, int] = {}
    for s in sub_rows:
        base_name, is_def = normalize_tag(s["tag"])
        tag_groups[base_name].append(s)
        tag_defaults[base_name] = is_def

    collisions: list[dict[str, Any]] = []
    target_folders: list[dict[str, Any]] = []
    split_records: list[dict[str, Any]] = []

    for base_name, group_subs in tag_groups.items():
        base_is_default = tag_defaults[base_name]
        periods_in_group = [s["period"] for s in group_subs]
        distinct_periods = sorted(set(periods_in_group))

        if split_by_period and len(distinct_periods) > 1:
            # Режим разбиения по period
            period_counts = {p: periods_in_group.count(p) for p in distinct_periods}

            if base_is_default == 1:
                # Системная папка («Основное»): daily если есть, иначе самый частый
                if "daily" in period_counts:
                    main_p = "daily"
                else:
                    main_p = min(distinct_periods, key=lambda p: (-period_counts[p], PERIOD_ORDER.get(p, 99)))
                main_name = base_name
                main_def = 1
            else:
                # Именованный тег: самый частый, при равенстве более короткий
                main_p = min(distinct_periods, key=lambda p: (-period_counts[p], PERIOD_ORDER.get(p, 99)))
                main_name = base_name
                main_def = 0

            became_list: list[dict[str, Any]] = []
            for p in distinct_periods:
                p_subs = [s for s in group_subs if s["period"] == p]
                if p == main_p:
                    fname = main_name
                    fdef = main_def
                else:
                    suffix = PERIOD_SUFFIX.get(p, p)
                    fname = f"{base_name} — {suffix}"
                    fdef = 0

                    # Имена проверять на casefold-коллизию с существующими тегами пользователя; коллизия — конфликт, код 3.
                    for et in user_existing_tags_clean:
                        if fname.casefold() == et.casefold():
                            collisions.append({
                                "user_id": uid,
                                "names": sorted([fname, et]),
                                "statuses": sorted({s["is_paused"] and "paused" or "active" for s in p_subs}),
                                "reason": f"Сгенерированное имя папки '{fname}' совпадает с существующим тегом пользователя '{et}'",
                                "resolution": "Коллизия имени папки по casefold() — конфликт (код 3), запись не делается",
                            })

                became_list.append({
                    "folder_name": fname,
                    "period": p,
                    "is_default": fdef,
                    "channels_count": len({s["channel_username"] for s in p_subs}),
                })

                target_folders.append({
                    "folder_name": fname,
                    "is_default": fdef,
                    "period": p,
                    "subs": p_subs,
                    "is_split": True,
                    "source_tag": base_name,
                })

            split_records.append({
                "user_id": uid,
                "tag": base_name,
                "periods": distinct_periods,
                "total_channels": len({s["channel_username"] for s in group_subs}),
                "became": became_list,
            })
        else:
            # Без разбиения (один период либо флаг не передан)
            if len(distinct_periods) > 1 or None in distinct_periods:
                valid_candidates = [p for p in distinct_periods if p in VALID_PERIODS]
                folder_period = sorted(valid_candidates)[0] if valid_candidates else "daily"
            else:
                folder_period = distinct_periods[0] if distinct_periods else "daily"

            target_folders.append({
                "folder_name": base_name,
                "is_default": base_is_default,
                "period": folder_period,
                "subs": group_subs,
                "is_split": False,
                "source_tag": base_name,
                "all_periods": distinct_periods,
            })

    # Проверка casefold-коллизий среди целевых папок пользователя
    tf_cf: dict[str, list[str]] = defaultdict(list)
    for tf in target_folders:
        tf_cf[tf["folder_name"].casefold()].append(tf["folder_name"])
    for cf_k, names in tf_cf.items():
        if len(names) > 1:
            all_statuses = set()
            for tf in target_folders:
                if tf["folder_name"] in names:
                    all_statuses.update(s["is_paused"] and "paused" or "active" for s in tf["subs"])
            collisions.append({
                "user_id": uid,
                "names": sorted(names),
                "statuses": sorted(all_statuses),
                "reason": f"Папки пользователя после разбиения совпадают по casefold: {sorted(names)}",
                "resolution": "Коллизия имён папок по casefold() — конфликт (код 3), запись не делается",
            })

    # Проверка casefold-коллизий с существующими папками в БД (таблица folders)
    if _table_exists(conn, "folders"):
        existing_rows = conn.execute(
            "SELECT name FROM folders WHERE user_id = ?", (uid,)
        ).fetchall()
        for er in existing_rows:
            ex_name = er["name"]
            for tf in target_folders:
                f_name = tf["folder_name"]
                if ex_name != f_name and ex_name.casefold() == f_name.casefold():
                    collisions.append({
                        "user_id": uid,
                        "names": sorted([ex_name, f_name]),
                        "statuses": sorted({s["is_paused"] and "paused" or "active" for s in tf["subs"]}),
                        "reason": f"Имя папки '{f_name}' конфликтует с существующей папкой '{ex_name}' по регистру",
                        "resolution": "Коллизия имён папок по casefold() — конфликт (код 3), запись не делается",
                    })

    return target_folders, split_records, collisions


def analyze_migration(conn: sqlite3.Connection, split_by_period: bool = False) -> dict[str, Any]:
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
        "split_folders": [],
        "cutover_warnings": [],
        "time_discrepancies": [],
        "conflicts": {
            "channel_in_multiple_tags": [],
            "channel_in_multiple_periods": [],
            "channel_duplicate_in_folder": [],
            "heterogeneous_period_in_folder": [],
            "time_discrepancy_in_folder": [],
            "empty_folders": [],
            "schedule_without_time": [],
            "schedule_default_time": [],
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

    # Структура для выявления конфликтов каналов: (user_id, channel_username) -> список назначений (folder_name, period, sub_row)
    user_channel_assignments: dict[tuple[int, str], list[tuple[str, str, sqlite3.Row]]] = defaultdict(list)

    for u_row in users_with_subs:
        uid = u_row["user_id"]
        target_folders, split_records, collisions = plan_user_folders(
            conn, uid, split_by_period=split_by_period
        )

        report["split_folders"].extend(split_records)
        report["conflicts"]["folder_name_case_collision"].extend(collisions)

        for tf in target_folders:
            f_name = tf["folder_name"]
            f_is_def = tf["is_default"]
            folder_period = tf["period"]
            subs = tf["subs"]
            subs_in_folder = len({s["channel_username"] for s in subs})

            # Проверка параметров расписания и digest_settings для каждой подписки
            for s in subs:
                raw_period = s["period"]
                ch_user = s["channel_username"]
                ch_user_norm = normalize_channel_username(ch_user)
                sub_id = s["id"]
                sub_status = "paused" if s["is_paused"] else "active"

                user_channel_assignments[(uid, ch_user_norm)].append((f_name, raw_period, s))

                # Валидация period подписки
                if raw_period is None or raw_period not in VALID_PERIODS:
                    report["conflicts"]["invalid_schedule_params"].append(
                        {
                            "user_id": uid,
                            "sub_id": sub_id,
                            "channel_username": ch_user,
                            "period": raw_period,
                            "status": sub_status,
                            "reason": f"Подписка имеет недопустимый period={repr(raw_period)} (ожидается daily, weekly, monthly)",
                        }
                    )

                # Проверка наличия явных настроек в digest_settings (если period валидный)
                if raw_period in VALID_PERIODS and (uid, raw_period) not in digest_settings_map:
                    # Не конфликт: дефолт из кода применяется штатно (информация для владельца)
                    report["conflicts"]["schedule_default_time"].append(
                        {
                            "user_id": uid,
                            "period": raw_period,
                            "send_hour": None,
                            "send_minute": None,
                            "reason": f"Нет явной записи в digest_settings для активной подписки '{raw_period}'",
                            "resolution": "Будет применён дефолт из кода (07:00, текущий день недели/месяца, monthly_mode='date')",
                        }
                    )

            # Проверка однородности period в папке (если не разбивалось)
            if not split_by_period and tf.get("all_periods"):
                all_p = tf["all_periods"]
                if len(all_p) > 1 or None in all_p:
                    report["conflicts"]["heterogeneous_period_in_folder"].append(
                        {
                            "user_id": uid,
                            "folder_name": f_name,
                            "periods": sorted([str(p) for p in all_p]),
                            "statuses": sorted({s["is_paused"] and "paused" or "active" for s in subs}),
                            "channels_count": subs_in_folder,
                            "resolution": "Папка содержит неоднородный period (включая NULL) — требуется один period на папку (код 3)",
                        }
                    )

            # Проверка расхождения времени внутри папки (информация, не конфликт)
            next_send_at_set = {s["next_send_at"] for s in subs if s["next_send_at"] is not None}
            if len(next_send_at_set) > 1:
                td_info = {
                    "user_id": uid,
                    "folder_name": f_name,
                    "period": folder_period,
                    "statuses": sorted({s["is_paused"] and "paused" or "active" for s in subs}),
                    "channels_count": subs_in_folder,
                    "distinct_times": sorted(str(t) for t in next_send_at_set),
                    "resolution": "Информация: next_send_at папки пересчитает T21 (не блокирует миграцию)",
                }
                report["time_discrepancies"].append(td_info)

            # Определение доставки (Telegram или Email)
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
                    "is_default": f_is_def,
                    "channels_count": subs_in_folder,
                    "period": folder_period,
                    "time": time_str,
                    "has_explicit_time": has_time,
                    "delivery": delivery,
                    "statuses": sorted({s["is_paused"] and "paused" or "active" for s in subs}),
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

    # 6. Конфликты каналов
    for (uid, channel_username), assignments in user_channel_assignments.items():
        unique_folders = sorted({item[0] for item in assignments})
        periods_list = [item[1] for item in assignments]
        unique_periods = sorted(set(periods_list))
        statuses = sorted({item[2]["is_paused"] and "paused" or "active" for item in assignments})

        if len(assignments) > 1:
            # 1. Дубль канала В ОДНОЙ папке с одним period остаётся строгим конфликтом (код 3)
            for fname in unique_folders:
                folder_assignments = [item for item in assignments if item[0] == fname]
                period_counts_in_folder = {p: [item[1] for item in folder_assignments].count(p) for p in set(item[1] for item in folder_assignments)}
                for dup_period, p_cnt in period_counts_in_folder.items():
                    if p_cnt > 1:
                        report["conflicts"]["channel_duplicate_in_folder"].append(
                            {
                                "user_id": uid,
                                "channel_username": channel_username,
                                "folder_name": fname,
                                "period": dup_period,
                                "statuses": statuses,
                                "resolution": "Канал дважды в одной папке с одним period — конфликт (код 3)",
                            }
                        )

            # 2. Канал в нескольких папках
            if len(unique_folders) > 1:
                if split_by_period and len(unique_periods) > 1:
                    # В режиме split-by-period разные папки с разными периодами -> предупреждение!
                    report["cutover_warnings"].append(
                        {
                            "user_id": uid,
                            "channel_username": channel_username,
                            "folders": unique_folders,
                            "periods": periods_list,
                            "statuses": statuses,
                            "message": "канал в нескольких папках — решить до cutover v2 (T21)",
                        }
                    )
                else:
                    report["conflicts"]["channel_in_multiple_tags"].append(
                        {
                            "user_id": uid,
                            "channel_username": channel_username,
                            "tags": unique_folders,
                            "statuses": statuses,
                            "resolution": "Канал принадлежит более чем одной папке — требуется выбор владельца",
                        }
                    )
                    report["conflicts"]["channel_in_multiple_periods"].append(
                        {
                            "user_id": uid,
                            "channel_username": channel_username,
                            "periods": periods_list,
                            "statuses": statuses,
                            "resolution": "Один канал в нескольких периодах/подписках — конфликт уникальности (код 3)",
                        }
                    )
            elif not split_by_period and len(unique_periods) > 1:
                # В одной неразбитой папке несколько периодов у канала
                report["conflicts"]["channel_in_multiple_periods"].append(
                    {
                        "user_id": uid,
                        "channel_username": channel_username,
                        "periods": periods_list,
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
    # Примечание: time_discrepancy_in_folder исключён из блокирующих конфликтов (пересчитает T21).
    has_conflicts = bool(
        report["conflicts"]["channel_in_multiple_tags"]
        or report["conflicts"]["channel_in_multiple_periods"]
        or report["conflicts"]["channel_duplicate_in_folder"]
        or report["conflicts"]["heterogeneous_period_in_folder"]
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

    # 4. Блок разбитых папок (было -> стало)
    split_folders = report.get("split_folders", [])
    lines.append("\n4. РАЗБИТЫЕ ПАПКИ (БЫЛО → СТАЛО):")
    if split_folders:
        for sf in split_folders:
            periods_str = ", ".join(sf["periods"])
            lines.append(
                f"   • user={sf['user_id']} тег='{sf['tag']}' (периоды: [{periods_str}], каналов: {sf['total_channels']}) ->"
            )
            for b in sf["became"]:
                lines.append(
                    f"       - '{b['folder_name']}' ({b['period']}, is_default={b['is_default']}, каналов: {b['channels_count']})"
                )
    else:
        lines.append("   • (нет разбитых папок)")

    # 5. Предупреждения до cutover
    cutover_warnings = report.get("cutover_warnings", [])
    time_discrepancies = report.get("time_discrepancies", [])
    lines.append("\n5. ПРЕДУПРЕЖДЕНИЯ ДО CUTOVER (T21):")
    has_cutover_items = False
    if cutover_warnings:
        has_cutover_items = True
        for cw in cutover_warnings:
            folders_str = ", ".join(repr(f) for f in cw["folders"])
            periods_str = ", ".join(repr(p) for p in cw["periods"])
            lines.append(
                f"   • [канал] user={cw['user_id']} channel=@{cw['channel_username']} "
                f"(папки: [{folders_str}], периоды: [{periods_str}]) -> {cw['message']}"
            )
    if time_discrepancies:
        has_cutover_items = True
        for td in time_discrepancies:
            times_str = ", ".join(repr(t) for t in td["distinct_times"])
            lines.append(
                f"   • [время] user={td['user_id']} папка='{td['folder_name']}' period={td['period']} "
                f"next_send_at=[{times_str}] -> next_send_at папки пересчитает T21"
            )
    if not has_cutover_items:
        lines.append("   • (нет предупреждений до cutover)")

    lines.append("\n6. ОБНАРУЖЕННЫЕ КОНФЛИКТЫ ДАННЫХ:")
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

    c3d = conflicts["schedule_default_time"]
    lines.append(f"   [3a] Подписки без явной записи в digest_settings (не конфликт, применится дефолт 07:00): {len(c3d)}")

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

    c6 = conflicts.get("channel_duplicate_in_folder", [])
    lines.append(f"   [6] Канал дублируется внутри одной папки: {len(c6)}")
    for item in c6:
        statuses_str = ", ".join(item.get("statuses", ["active"]))
        lines.append(
            f"       - user={item['user_id']} channel=@{item['channel_username']} "
            f"папка='{item['folder_name']}' period={item['period']} "
            f"(статусы: {statuses_str}) -> {item['resolution']}"
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
    lines.append("\n7. ЧТО ОСТАНЕТСЯ КАК LEGACY:")
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
    parser.add_argument(
        "--split-by-period",
        action="store_true",
        help="Разбивать папки/теги с неоднородными period на отдельные папки по period",
    )
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
        report = analyze_migration(conn, split_by_period=args.split_by_period)
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
