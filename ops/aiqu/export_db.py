#!/usr/bin/env python3
"""Зашифрованный экспорт и восстановление БД SQLite для переноса между серверами.

Использование:
    python3 ops/aiqu/export_db.py export --db storage/bot_data.db --recipient <age1…> [--recipients-file F] --out storage/exports/ [--tag migrate]
    python3 ops/aiqu/export_db.py verify --in FILE.age
    python3 ops/aiqu/export_db.py restore --in FILE.age --identity KEYFILE --db storage/bot_data.db [--backup-dir storage/backups] [--dry-run] [--force-schema]

Коды выхода:
    0 — успех;
    1 — ошибка ввода-вывода / утилиты age / активный WAL;
    2 — неверные аргументы;
    3 — несоответствие версии схемы (экспорт новее целевой БД / кода без --force-schema);
    4 — не удалось создать обязательный снимок целевой БД перед заменой;
    5 — несовпадение контрольной суммы sha256 или формата файла / манифеста.
"""

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

# Обеспечиваем импорт ops.aiqu.backup_db из корня репозитория
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

try:
    from ops.aiqu.backup_db import compress_old_snapshots, rotate, snapshot
except ImportError:
    # Запасной вариант при вызове из нестандартных путей
    snapshot = None  # type: ignore[assignment]
    rotate = None  # type: ignore[assignment]
    compress_old_snapshots = None  # type: ignore[assignment]


def find_age_bin() -> str | None:
    """Ищет исполняемый бинарник age в PATH или по AGE_BIN."""
    bin_name = os.environ.get("AGE_BIN", "age")
    if os.path.isabs(bin_name) or os.path.dirname(bin_name):
        if os.path.isfile(bin_name) and os.access(bin_name, os.X_OK):
            return bin_name
        return None
    return shutil.which(bin_name)


def extract_db_meta(conn: sqlite3.Connection) -> tuple[int, int, str]:
    """Возвращает метаданные БД: (число_таблиц, sqlite_user_version, app_schema)."""
    tables_count = conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchone()[0]
    user_version = conn.execute("PRAGMA user_version").fetchone()[0]

    app_schema = None
    has_app_meta = conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE type = 'table' AND name = 'app_meta'"
    ).fetchone()[0] > 0
    if has_app_meta:
        for key in ("schema_version", "app_schema", "schema", "timestamps_storage"):
            row = conn.execute("SELECT value FROM app_meta WHERE key = ?", (key,)).fetchone()
            if row and row[0] is not None:
                app_schema = str(row[0])
                break
    if app_schema is None:
        app_schema = str(user_version) if user_version else ("empty" if tables_count == 0 else "unknown")
    return tables_count, user_version, app_schema


class SchemaComparisonResult(tuple):
    """Кортеж (is_newer, is_older), расширенный атрибутом reason для детальной диагностики."""

    def __new__(cls, is_newer: bool, is_older: bool, reason: str | None = None):
        return super().__new__(cls, (is_newer, is_older))

    def __init__(self, is_newer: bool, is_older: bool, reason: str | None = None):
        self.is_newer = is_newer
        self.is_older = is_older
        self.reason = reason


def get_db_schema_structure(conn: sqlite3.Connection) -> dict[str, set[str]]:
    """Возвращает структуру схемы {имя_таблицы: множество_колонок} для всех пользовательских таблиц."""
    cur = conn.cursor()
    tables = cur.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    structure: dict[str, set[str]] = {}
    for (t_name,) in tables:
        cols = cur.execute(f"PRAGMA table_info({t_name})").fetchall()
        structure[t_name] = {col[1] for col in cols}
    return structure


def get_code_schema_structure() -> dict[str, set[str]] | None:
    """Возвращает эталонную структуру схемы из кода data.database.init_db(), если модуль доступен."""
    try:
        import data.database as db
        with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
            orig_db_path = getattr(db, "DB_PATH", None)
            try:
                db.DB_PATH = tmp.name
                db.init_db()
                with closing(sqlite3.connect(tmp.name)) as conn:
                    return get_db_schema_structure(conn)
            finally:
                if orig_db_path is not None:
                    db.DB_PATH = orig_db_path
    except Exception:
        return None


def parse_schema_version(val: str | int) -> tuple[int, ...]:
    """Извлекает числовые компоненты версии (например 'utc_v1' -> (1,), '2' -> (2,), 'v2.1' -> (2, 1))."""
    if isinstance(val, int):
        return (val,)
    nums = [int(x) for x in re.findall(r"\d+", str(val))]
    return tuple(nums) if nums else (0,)


def compare_schemas(
    export_schema: str,
    export_user_version: int,
    target_schema: str,
    target_user_version: int,
    export_structure: dict[str, set[str]] | None = None,
    target_structure: dict[str, set[str]] | None = None,
) -> SchemaComparisonResult:
    """Сравнивает версии схемы экспорта и целевой базы/кода.

    Возвращает SchemaComparisonResult(is_newer, is_older, reason).
    """
    if export_user_version != target_user_version and (export_user_version > 0 or target_user_version > 0):
        if export_user_version > target_user_version:
            return SchemaComparisonResult(True, False, f"user_version ({export_user_version} > {target_user_version})")
        return SchemaComparisonResult(False, True, f"user_version ({export_user_version} < {target_user_version})")

    exp_parsed = parse_schema_version(export_schema)
    tar_parsed = parse_schema_version(target_schema)

    if exp_parsed != tar_parsed and exp_parsed != (0,) and tar_parsed != (0,):
        if exp_parsed > tar_parsed:
            return SchemaComparisonResult(True, False, f"версия схемы ({export_schema} > {target_schema})")
        return SchemaComparisonResult(False, True, f"версия схемы ({export_schema} < {target_schema})")

    if export_structure is not None and target_structure is not None:
        newer_details: list[str] = []
        older_details: list[str] = []

        # Проверка новых таблиц в экспорте
        new_tables = set(export_structure.keys()) - set(target_structure.keys())
        if new_tables:
            newer_details.append(f"новые таблицы: {sorted(new_tables)}")

        # Проверка отсутствующих таблиц
        missing_tables = set(target_structure.keys()) - set(export_structure.keys())
        if missing_tables:
            older_details.append(f"отсутствующие таблицы: {sorted(missing_tables)}")

        # Проверка колонок в общих таблицах
        common_tables = set(export_structure.keys()) & set(target_structure.keys())
        for t in sorted(common_tables):
            new_cols = export_structure[t] - target_structure[t]
            if new_cols:
                newer_details.append(f"новые колонки в {t}: {sorted(new_cols)}")
            missing_cols = target_structure[t] - export_structure[t]
            if missing_cols:
                older_details.append(f"отсутствующие колонки в {t}: {sorted(missing_cols)}")

        if newer_details:
            return SchemaComparisonResult(True, False, "; ".join(newer_details))
        if older_details:
            return SchemaComparisonResult(False, True, "; ".join(older_details))

    return SchemaComparisonResult(False, False, None)


def verify_export_file(file_path: Path) -> tuple[int, dict | None, str | None]:
    """Проверяет целостность зашифрованного файла и его манифеста.

    Возвращает (exit_code, manifest_dict, error_msg).
    0 — успешно;
    1 — ошибка ввода-вывода (файл не найден);
    5 — ошибка формата или несовпадение контрольной суммы.
    """
    if not file_path.is_file():
        return 1, None, f"файл не найден: {file_path}"

    manifest_path = file_path.with_name(f"{file_path.name}.json")
    if not manifest_path.is_file():
        return 5, None, f"манифест не найден: {manifest_path}"

    try:
        content = manifest_path.read_text(encoding="utf-8")
        manifest = json.loads(content)
    except (OSError, json.JSONDecodeError) as exc:
        return 5, None, f"ошибка чтения манифеста: {exc}"

    if not isinstance(manifest, dict):
        return 5, None, "манифест должен быть JSON-объектом"

    required_keys = ("format_version", "created_at", "sha256", "size", "plaintext_size")
    for key in required_keys:
        if key not in manifest:
            return 5, None, f"в манифесте отсутствует обязательное поле: {key}"

    if manifest.get("format_version") != 1:
        return 5, None, f"неподдерживаемая версия формата манифеста: {manifest.get('format_version')} (ожидается 1)"

    if not isinstance(manifest.get("size"), int) or not isinstance(manifest.get("plaintext_size"), int):
        return 5, None, "поля size и plaintext_size должны быть целыми числами"

    if manifest["plaintext_size"] <= 0:
        return 5, None, f"некорректный plaintext_size: {manifest['plaintext_size']}"

    try:
        actual_bytes = file_path.read_bytes()
    except OSError as exc:
        return 1, None, f"ошибка чтения файла: {exc}"

    if not actual_bytes.startswith(b"age-encryption.org/v1"):
        return 5, None, f"неверный заголовок age (ожидается age-encryption.org/v1): {actual_bytes[:25]!r}"

    actual_size = len(actual_bytes)
    if actual_size != manifest["size"]:
        return 5, None, f"несовпадение размера: в файле {actual_size}, в манифесте {manifest['size']}"

    actual_sha256 = hashlib.sha256(actual_bytes).hexdigest()
    if actual_sha256 != manifest["sha256"]:
        return 5, None, f"несовпадение sha256: вычислено {actual_sha256}, в манифесте {manifest['sha256']}"

    return 0, manifest, None


def cmd_export(args: argparse.Namespace, now: float | None = None) -> int:
    """Выполняет экспорт БД в зашифрованный файл."""
    age_bin = find_age_bin()
    if not age_bin:
        print("ОШИБКА: утилита age не найдена в PATH; установите age (apt install age).", file=sys.stderr)
        return 1

    recipients = args.recipient or []
    recipients_file = args.recipients_file

    if not recipients and not recipients_file:
        print("ОШИБКА: укажите хотя бы одного получателя (--recipient) или файл (--recipients-file).", file=sys.stderr)
        return 2

    recipient_list = list(recipients)
    if recipients_file:
        rf = Path(recipients_file)
        if not rf.is_file():
            print(f"ОШИБКА: файл получателей не найден: {recipients_file}", file=sys.stderr)
            return 1
        try:
            for line in rf.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    recipient_list.append(line)
        except OSError as exc:
            print(f"ОШИБКА: чтение файла получателей {rf}: {exc}", file=sys.stderr)
            return 1

    recipients_count = len(recipient_list)
    if recipients_count == 0:
        print("ОШИБКА: список получателей пуст.", file=sys.stderr)
        return 2

    db_path = Path(args.db)
    if not db_path.is_file():
        print(f"ОШИБКА: файл базы данных не найден: {db_path}", file=sys.stderr)
        return 1

    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.tag):
        print("ОШИБКА: некорректный --tag (допустимы только [A-Za-z0-9_-])", file=sys.stderr)
        return 2

    # Создаём снимок через sqlite3.Connection.backup в память (:memory:)
    try:
        with closing(sqlite3.connect(str(db_path))) as source, closing(sqlite3.connect(":memory:")) as mem_conn:
            source.backup(mem_conn)
            verdict = mem_conn.execute("PRAGMA quick_check").fetchone()[0]
            if verdict != "ok":
                print(f"ОШИБКА: PRAGMA quick_check снимка в памяти: {verdict}", file=sys.stderr)
                return 1
            tables, user_version, app_schema = extract_db_meta(mem_conn)
            has_app_meta = (
                mem_conn.execute(
                    "SELECT count(*) FROM sqlite_master WHERE type = 'table' AND name = 'app_meta'"
                ).fetchone()[0]
                > 0
            )
            if tables == 0 or not has_app_meta:
                print(
                    "ОШИБКА: исходная БД пуста (0 таблиц) или не является базой бота (нет таблицы app_meta).",
                    file=sys.stderr,
                )
                return 1
            plaintext_bytes = mem_conn.serialize()
            plaintext_size = len(plaintext_bytes)
            if plaintext_size == 0:
                print("ОШИБКА: сериализованный снимок пуст (0 байт).", file=sys.stderr)
                return 1
    except Exception as exc:
        print(f"ОШИБКА: не удалось создать снимок базы в памяти: {exc}", file=sys.stderr)
        return 1

    # Запускаем age для шифрования потоком без plaintext на диске
    cmd = [age_bin]
    for r in recipients:
        cmd.extend(["-r", r])
    if recipients_file:
        cmd.extend(["-R", str(recipients_file)])

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        ciphertext, stderr_bytes = proc.communicate(input=plaintext_bytes)
        if proc.returncode != 0:
            err_msg = stderr_bytes.decode("utf-8", errors="replace").strip()
            print(f"ОШИБКА: сбой age при шифровании (код {proc.returncode}): {err_msg}", file=sys.stderr)
            return 1
    except OSError as exc:
        print(f"ОШИБКА: запуск age: {exc}", file=sys.stderr)
        return 1

    if not ciphertext:
        print("ОШИБКА: age вернул пустой шифротекст.", file=sys.stderr)
        return 1

    if not ciphertext.startswith(b"age-encryption.org/v1"):
        print(
            "ОШИБКА: вывод age не содержит заголовок age-encryption.org/v1 (возможна ошибка AGE_BIN или открытый текст).",
            file=sys.stderr,
        )
        return 1

    out_dir = Path(args.out)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"ОШИБКА: не удалось создать каталог вывода {out_dir}: {exc}", file=sys.stderr)
        return 1

    cur_time = args.now if args.now is not None else (time.time() if now is None else now)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(cur_time))
    created_iso = datetime.fromtimestamp(cur_time, tz=timezone.utc).isoformat()

    final_name = f"{db_path.stem}-{stamp}-{args.tag}.db.age"
    final_path = out_dir / final_name
    if final_path.exists():
        print(f"ОШИБКА: файл экспорта уже существует: {final_path}", file=sys.stderr)
        return 1
    partial_path = out_dir / f"{final_name}.partial"

    cipher_sha256 = hashlib.sha256(ciphertext).hexdigest()
    cipher_size = len(ciphertext)

    try:
        partial_path.write_bytes(ciphertext)
        os.chmod(partial_path, 0o600)
        os.replace(partial_path, final_path)
        os.chmod(final_path, 0o600)
    except OSError as exc:
        partial_path.unlink(missing_ok=True)
        print(f"ОШИБКА: запись зашифрованного файла {final_path}: {exc}", file=sys.stderr)
        return 1

    manifest_data = {
        "format_version": 1,
        "created_at": created_iso,
        "sha256": cipher_sha256,
        "size": cipher_size,
        "plaintext_size": plaintext_size,
        "sqlite_user_version": user_version,
        "tables": tables,
        "app_schema": app_schema,
        "age_recipients_count": recipients_count,
    }

    manifest_path = out_dir / f"{final_name}.json"
    manifest_partial = out_dir / f"{final_name}.json.partial"
    try:
        manifest_partial.write_text(json.dumps(manifest_data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.chmod(manifest_partial, 0o600)
        os.replace(manifest_partial, manifest_path)
        os.chmod(manifest_path, 0o600)
    except OSError as exc:
        manifest_partial.unlink(missing_ok=True)
        print(f"ОШИБКА: запись манифеста {manifest_path}: {exc}", file=sys.stderr)
        return 1

    print(f"export ok path={final_path} size={cipher_size} sha256={cipher_sha256}")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    """Проверяет sha256 и формат зашифрованного файла без ключа."""
    in_file = Path(args.input)
    code, manifest, err = verify_export_file(in_file)
    if code != 0:
        print(f"ОШИБКА проверки: {err}", file=sys.stderr)
        return code
    print(f"verify ok path={in_file} size={manifest['size']} sha256={manifest['sha256']}")
    return 0


def cmd_restore(args: argparse.Namespace, now: float | None = None) -> int:
    """Восстанавливает БД из зашифрованного экспорта."""
    print(
        "НАПОМИНАНИЕ: перед восстановлением контейнер unagi-bot должен быть остановлен (docker compose stop unagi-bot).",
        file=sys.stderr,
    )

    target_db = Path(args.db)
    wal_file = target_db.with_name(f"{target_db.name}-wal")
    shm_file = target_db.with_name(f"{target_db.name}-shm")
    if (wal_file.exists() and wal_file.stat().st_size > 0) or shm_file.exists():
        active = []
        if wal_file.exists() and wal_file.stat().st_size > 0:
            active.append(f"{wal_file.name} ({wal_file.stat().st_size} байт)")
        if shm_file.exists():
            active.append(f"{shm_file.name}")
        print(
            f"ОШИБКА: обнаружен активный WAL-файл / SHM-файл ({', '.join(active)}). "
            "Контейнер запущен или БД не была корректно закрыта. "
            "Остановите контейнер (docker compose stop unagi-bot) и дождитесь сброса WAL перед восстановлением.",
            file=sys.stderr,
        )
        return 1

    age_bin = find_age_bin()
    if not age_bin:
        print("ОШИБКА: утилита age не найдена в PATH; установите age (apt install age).", file=sys.stderr)
        return 1

    identity_path = Path(args.identity)
    if not identity_path.is_file():
        print(f"ОШИБКА: файл ключа identity не найден: {identity_path}", file=sys.stderr)
        return 1

    in_file = Path(args.input)

    # (1) verify
    v_code, manifest, v_err = verify_export_file(in_file)
    if v_code != 0:
        print(f"ОШИБКА верификации: {v_err}", file=sys.stderr)
        return v_code

    # (2) расшифровка потоком в память
    try:
        ciphertext = in_file.read_bytes()
    except OSError as exc:
        print(f"ОШИБКА: чтение файла экспорта: {exc}", file=sys.stderr)
        return 1

    cmd = [age_bin, "-d", "-i", str(identity_path)]
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        plaintext_bytes, stderr_bytes = proc.communicate(input=ciphertext)
        if proc.returncode != 0:
            err_msg = stderr_bytes.decode("utf-8", errors="replace").strip()
            print(f"ОШИБКА: сбой age при расшифровке (код {proc.returncode}): {err_msg}", file=sys.stderr)
            return 1
    except OSError as exc:
        print(f"ОШИБКА: запуск age: {exc}", file=sys.stderr)
        return 1

    if len(plaintext_bytes) != manifest["plaintext_size"]:
        print(
            f"ОШИБКА: размер расшифрованных данных ({len(plaintext_bytes)}) "
            f"не совпадает с манифестом ({manifest['plaintext_size']})",
            file=sys.stderr,
        )
        return 5

    # Запись во временный файл в ЦЕЛЕВОМ каталоге с правами 0600 и немедленный quick_check
    target_dir = target_db.parent
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"ОШИБКА: не удалось создать целевой каталог {target_dir}: {exc}", file=sys.stderr)
        return 1

    tmp_path = None
    try:
        try:
            with tempfile.NamedTemporaryFile(
                dir=target_dir, prefix=f"{target_db.stem}-restore-", suffix=".tmp", delete=False
            ) as tmp_file:
                tmp_path = Path(tmp_file.name)
                tmp_file.write(plaintext_bytes)
            os.chmod(tmp_path, 0o600)
        except OSError as exc:
            print(f"ОШИБКА: запись временного файла восстановления: {exc}", file=sys.stderr)
            return 1

        # Немедленный PRAGMA quick_check
        try:
            with closing(sqlite3.connect(str(tmp_path))) as conn:
                verdict = conn.execute("PRAGMA quick_check").fetchone()[0]
                if verdict != "ok":
                    print(f"ОШИБКА: PRAGMA quick_check расшифрованной БД: {verdict}", file=sys.stderr)
                    return 5
                restored_tables, restored_user_version, restored_app_schema = extract_db_meta(conn)
                has_app_meta = (
                    conn.execute(
                        "SELECT count(*) FROM sqlite_master WHERE type = 'table' AND name = 'app_meta'"
                    ).fetchone()[0]
                    > 0
                )
                if restored_tables == 0 or not has_app_meta:
                    print(
                        "ОШИБКА: расшифрованная БД пуста (0 таблиц) или не является базой бота (нет таблицы app_meta).",
                        file=sys.stderr,
                    )
                    return 5
                restored_structure = get_db_schema_structure(conn)
        except sqlite3.Error as exc:
            print(f"ОШИБКА: расшифрованный файл повреждён: {exc}", file=sys.stderr)
            return 5

        # (3) сравнение версии схемы
        target_structure = None
        if target_db.is_file():
            try:
                with closing(sqlite3.connect(str(target_db))) as tconn:
                    _, target_user_version, target_app_schema = extract_db_meta(tconn)
                    target_structure = get_db_schema_structure(tconn)
            except sqlite3.Error:
                target_user_version = 0
                target_app_schema = "unknown"
                target_structure = get_code_schema_structure()
        else:
            target_user_version = 0
            target_app_schema = "code"
            code_struct = get_code_schema_structure()
            if code_struct and any(tbl in code_struct for tbl in restored_structure if tbl != "app_meta"):
                target_structure = code_struct

        cmp_res = compare_schemas(
            restored_app_schema,
            restored_user_version,
            target_app_schema,
            target_user_version,
            export_structure=restored_structure,
            target_structure=target_structure,
        )
        is_newer, is_older = cmp_res

        if is_newer:
            diff_suffix = f" [{cmp_res.reason}]" if cmp_res.reason else ""
            if not args.force_schema:
                print(
                    f"ОШИБКА: схема экспорта ({restored_app_schema}, user_version={restored_user_version}) "
                    f"НОВЕЕ схемы целевой БД/кода ({target_app_schema}, user_version={target_user_version}){diff_suffix}. "
                    "Отказ. Используйте --force-schema для принудительного восстановления.",
                    file=sys.stderr,
                )
                return 3
            print(
                f"ПРЕДУПРЕЖДЕНИЕ: схема экспорта ({restored_app_schema}) новее целевой БД ({target_app_schema}){diff_suffix}; "
                "восстановление разрешено через --force-schema.",
                file=sys.stderr,
            )
        elif is_older:
            diff_suffix = f" [{cmp_res.reason}]" if cmp_res.reason else ""
            print(
                f"ПРЕДУПРЕЖДЕНИЕ: схема экспорта ({restored_app_schema}) старее схемы целевой БД ({target_app_schema}){diff_suffix}.",
                file=sys.stderr,
            )

        if args.dry_run:
            print(f"restore ok (dry-run) path={target_db} size={len(plaintext_bytes)}")
            return 0

        # (4) если целевая БД существует — ОБЯЗАТЕЛЬНЫЙ снимок через backup_db.snapshot
        if target_db.is_file():
            if snapshot is None:
                print("ОШИБКА: не удалось импортировать snapshot из ops.aiqu.backup_db", file=sys.stderr)
                return 4
            backup_dir = Path(args.backup_dir) if args.backup_dir else target_db.parent / "backups"
            cur_time = args.now if args.now is not None else (time.time() if now is None else now)
            try:
                snap_path, snap_size, snap_tables = snapshot(target_db, backup_dir, tag="prerestore", now=cur_time)
                print(f"prerestore backup ok path={snap_path} size={snap_size} tables={snap_tables}")
                try:
                    if rotate and compress_old_snapshots:
                        rotate(target_db, backup_dir, "prerestore", keep=14)
                        compress_old_snapshots(target_db, backup_dir, "prerestore", keep_raw=2)
                except Exception as clean_exc:
                    print(f"ПРЕДУПРЕЖДЕНИЕ: уборка снимков prerestore не завершена: {clean_exc}", file=sys.stderr)
            except Exception as exc:
                print(f"ОШИБКА: обязательный снимок перед заменой не создан: {exc}", file=sys.stderr)
                return 4

        # (5) атомарная замена os.replace
        try:
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, target_db)
            os.chmod(target_db, 0o600)
            for sfx in ("-wal", "-shm"):
                target_db.with_name(f"{target_db.name}{sfx}").unlink(missing_ok=True)
        except OSError as exc:
            print(f"ОШИБКА: замена целевого файла БД: {exc}", file=sys.stderr)
            return 1

        print(f"restore ok path={target_db} size={target_db.stat().st_size}")
        return 0

    finally:
        if tmp_path and tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


def build_parser() -> argparse.ArgumentParser:
    """Создаёт парсер аргументов командной строки."""
    parser = argparse.ArgumentParser(
        description="Зашифрованный экспорт и восстановление БД SQLite для переноса между серверами",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # export
    p_export = subparsers.add_parser("export", help="Зашифрованный экспорт БД в память и файл .age")
    p_export.add_argument("--db", default="storage/bot_data.db", help="Путь к исходной БД")
    p_export.add_argument(
        "--recipient",
        action="append",
        default=[],
        help="Публичный ключ age (age1...), можно указывать несколько раз",
    )
    p_export.add_argument("--recipients-file", default=None, help="Файл со списком публичных ключей age")
    p_export.add_argument("--out", default="storage/exports/", help="Каталог вывода")
    p_export.add_argument("--tag", default="migrate", help="Тег для имени файла экспорта")
    p_export.add_argument("--now", type=float, default=None, help="Временная метка Unix (для тестов)")

    # verify
    p_verify = subparsers.add_parser("verify", help="Проверка sha256 и формата файла .age по манифесту")
    p_verify.add_argument("--in", dest="input", required=True, help="Путь к зашифрованному файлу .age")

    # restore
    p_restore = subparsers.add_parser("restore", help="Восстановление БД из файла .age")
    p_restore.add_argument("--in", dest="input", required=True, help="Путь к зашифрованному файлу .age")
    p_restore.add_argument("--identity", required=True, help="Путь к файлу приватного ключа age")
    p_restore.add_argument("--db", default="storage/bot_data.db", help="Путь к целевой БД")
    p_restore.add_argument("--backup-dir", default=None, help="Каталог для резервных копий")
    p_restore.add_argument("--dry-run", action="store_true", help="Проверка без замены целевой БД")
    p_restore.add_argument(
        "--force-schema",
        action="store_true",
        help="Разрешить восстановление, если схема экспорта новее целевой",
    )
    p_restore.add_argument("--now", type=float, default=None, help="Временная метка Unix (для тестов)")

    return parser


def main(argv: list[str] | None = None, now: float | None = None) -> int:
    """Точка входа скрипта."""
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2

    if args.command == "export":
        return cmd_export(args, now=now)
    if args.command == "verify":
        return cmd_verify(args)
    if args.command == "restore":
        return cmd_restore(args, now=now)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
