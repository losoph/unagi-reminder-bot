#!/usr/bin/env python3
"""Тесты для ops/aiqu/export_db.py."""

import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from ops.aiqu import export_db


class FakeAgeHelper:
    """Создаёт исполняемый скрипт фейкового age для тестирования без установки бинарника age."""

    @staticmethod
    def create_fake_age_script(target_dir: Path) -> Path:
        script_path = target_dir / "fake_age"
        content = f"""#!{sys.executable}
import sys
from pathlib import Path

args = sys.argv[1:]

if "-d" in args:
    if "-i" not in args:
        sys.stderr.write("fake_age: -i identity file required\\n")
        sys.exit(1)
    i_idx = args.index("-i")
    if i_idx + 1 >= len(args):
        sys.stderr.write("fake_age: missing identity argument\\n")
        sys.exit(1)
    key_file = Path(args[i_idx + 1])
    if not key_file.is_file():
        sys.stderr.write(f"fake_age: identity file not found: {{key_file}}\\n")
        sys.exit(1)
    key_text = key_file.read_text(encoding="utf-8").strip()
    if not key_text:
        sys.stderr.write("fake_age: empty identity key\\n")
        sys.exit(1)

    data = sys.stdin.buffer.read()
    prefix = b"age-encryption.org/v1\\n-> fake-x25519\\n\\n"
    if not data.startswith(prefix):
        sys.stderr.write("fake_age: invalid format or corrupted ciphertext\\n")
        sys.exit(1)
    payload = data[len(prefix):]
    plain = bytes(b ^ 0x5A for b in payload)
    sys.stdout.buffer.write(plain)
    sys.exit(0)
else:
    has_recip = False
    for idx, arg in enumerate(args):
        if arg == "-r" and idx + 1 < len(args):
            has_recip = True
        elif arg == "-R" and idx + 1 < len(args):
            rf = Path(args[idx + 1])
            if not rf.is_file():
                sys.stderr.write(f"fake_age: recipients file not found: {{rf}}\\n")
                sys.exit(1)
            has_recip = True
    if not has_recip:
        sys.stderr.write("fake_age: missing recipients (-r or -R)\\n")
        sys.exit(1)

    data = sys.stdin.buffer.read()
    prefix = b"age-encryption.org/v1\\n-> fake-x25519\\n\\n"
    payload = bytes(b ^ 0x5A for b in data)
    sys.stdout.buffer.write(prefix + payload)
    sys.exit(0)
"""
        script_path.write_text(content, encoding="utf-8")
        script_path.chmod(0o755)
        return script_path


class ExportDbTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.tmp_dir.name)

        # Создаём фейковый age
        self.fake_age_path = FakeAgeHelper.create_fake_age_script(self.base_dir)
        self.orig_age_bin = os.environ.get("AGE_BIN")
        os.environ["AGE_BIN"] = str(self.fake_age_path)

        # Создаём ключ identity
        self.key_file = self.base_dir / "recovery.key"
        self.key_file.write_text("AGE-SECRET-KEY-TEST1234567890", encoding="utf-8")
        self.key_file.chmod(0o600)

        # Создаём тестовую БД
        self.src_db = self.base_dir / "source.db"
        self._populate_db(self.src_db, tables=3, schema="utc_v1", user_version=0)

        self.export_dir = self.base_dir / "exports"
        self.backup_dir = self.base_dir / "backups"
        self.dest_db = self.base_dir / "target.db"

    def tearDown(self):
        if self.orig_age_bin is not None:
            os.environ["AGE_BIN"] = self.orig_age_bin
        else:
            os.environ.pop("AGE_BIN", None)
        self.tmp_dir.cleanup()

    def _populate_db(self, path: Path, tables: int = 3, schema: str = "utc_v1", user_version: int = 0) -> None:
        with sqlite3.connect(str(path)) as conn:
            conn.execute("CREATE TABLE app_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            conn.execute("INSERT INTO app_meta (key, value) VALUES ('timestamps_storage', ?)", (schema,))
            conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, email TEXT)")
            conn.execute("INSERT INTO users VALUES (1, 'alice', 'alice@example.com')")
            conn.execute("INSERT INTO users VALUES (2, 'bob', 'bob@example.com')")
            if tables >= 3:
                conn.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, text TEXT)")
                conn.execute("INSERT INTO notes VALUES (1, 'важная заметка')")
            if user_version > 0:
                conn.execute(f"PRAGMA user_version = {user_version}")
            conn.commit()

    def test_export_verify_restore_roundtrip(self):
        """Полный цикл: экспорт, проверка и восстановление возвращают ту же БД."""
        now = 1700000000.0
        out_buf = io.StringIO()
        err_buf = io.StringIO()
        with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
            code = export_db.main(
                [
                    "export",
                    "--db",
                    str(self.src_db),
                    "--recipient",
                    "age1testrecipient",
                    "--out",
                    str(self.export_dir),
                    "--tag",
                    "migrate",
                ],
                now=now,
            )
        self.assertEqual(code, 0)
        export_out = out_buf.getvalue().strip()
        self.assertTrue(export_out.startswith("export ok path="))
        self.assertIn("size=", export_out)
        self.assertIn("sha256=", export_out)

        export_files = list(self.export_dir.glob("*.db.age"))
        self.assertEqual(len(export_files), 1)
        age_file = export_files[0]
        manifest_file = age_file.with_name(f"{age_file.name}.json")
        self.assertTrue(manifest_file.is_file())

        # Verify
        out_buf = io.StringIO()
        with contextlib.redirect_stdout(out_buf):
            v_code = export_db.main(["verify", "--in", str(age_file)])
        self.assertEqual(v_code, 0)
        self.assertTrue(out_buf.getvalue().strip().startswith("verify ok path="))

        # Restore
        out_buf = io.StringIO()
        with contextlib.redirect_stdout(out_buf):
            r_code = export_db.main(
                [
                    "restore",
                    "--in",
                    str(age_file),
                    "--identity",
                    str(self.key_file),
                    "--db",
                    str(self.dest_db),
                    "--backup-dir",
                    str(self.backup_dir),
                ],
                now=now + 60,
            )
        self.assertEqual(r_code, 0)
        self.assertTrue(out_buf.getvalue().strip().startswith("restore ok path="))

        # Сверяем данные
        with sqlite3.connect(str(self.dest_db)) as conn:
            verdict = conn.execute("PRAGMA quick_check").fetchone()[0]
            self.assertEqual(verdict, "ok")
            users = conn.execute("SELECT id, name FROM users ORDER BY id").fetchall()
            self.assertEqual(users, [(1, "alice"), (2, "bob")])
            notes = conn.execute("SELECT id, text FROM notes").fetchall()
            self.assertEqual(notes, [(1, "важная заметка")])

    def test_no_plaintext_leak(self):
        """Plaintext snapshot во временных каталогах не остаётся; в экспорте только шифротекст."""
        now = 1700000000.0
        code = export_db.main(
            [
                "export",
                "--db",
                str(self.src_db),
                "--recipient",
                "age1testrecipient",
                "--out",
                str(self.export_dir),
            ],
            now=now,
        )
        self.assertEqual(code, 0)

        # Сканируем весь каталог base_dir (кроме исходного src_db)
        sqlite_header = b"SQLite format 3\x00"
        for p in self.base_dir.rglob("*"):
            if p.is_file() and p.resolve() != self.src_db.resolve():
                content = p.read_bytes()
                self.assertFalse(
                    content.startswith(sqlite_header),
                    f"Обнаружен незашифрованный файл SQLite: {p}",
                )

    def test_manifest_structure_and_values(self):
        """Манифест содержит все обязательные поля с корректными типами и значениями."""
        now = 1700000000.0
        code = export_db.main(
            [
                "export",
                "--db",
                str(self.src_db),
                "--recipient",
                "age1test1",
                "--out",
                str(self.export_dir),
                "--tag",
                "testmanifest",
            ],
            now=now,
        )
        self.assertEqual(code, 0)

        age_file = list(self.export_dir.glob("*.db.age"))[0]
        manifest_file = age_file.with_name(f"{age_file.name}.json")
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))

        self.assertEqual(manifest["format_version"], 1)
        self.assertEqual(manifest["created_at"], "2023-11-14T22:13:20+00:00")
        self.assertEqual(manifest["sha256"], hashlib.sha256(age_file.read_bytes()).hexdigest())
        self.assertEqual(manifest["size"], age_file.stat().st_size)
        self.assertGreater(manifest["plaintext_size"], 0)
        self.assertEqual(manifest["sqlite_user_version"], 0)
        self.assertEqual(manifest["tables"], 3)
        self.assertEqual(manifest["app_schema"], "utc_v1")
        self.assertEqual(manifest["age_recipients_count"], 1)

    def test_corrupted_ciphertext_fails_verify_and_restore(self):
        """Порча шифротекста вызывает ошибку sha256/формата с кодом 5."""
        code = export_db.main(
            [
                "export",
                "--db",
                str(self.src_db),
                "--recipient",
                "age1test",
                "--out",
                str(self.export_dir),
            ]
        )
        self.assertEqual(code, 0)
        age_file = list(self.export_dir.glob("*.db.age"))[0]

        # Портим байт в шифротексте
        raw = bytearray(age_file.read_bytes())
        raw[-1] ^= 0xFF
        age_file.write_bytes(bytes(raw))

        # verify -> код 5
        err_buf = io.StringIO()
        with contextlib.redirect_stderr(err_buf):
            v_code = export_db.main(["verify", "--in", str(age_file)])
        self.assertEqual(v_code, 5)

        # restore -> код 5
        err_buf = io.StringIO()
        with contextlib.redirect_stderr(err_buf):
            r_code = export_db.main(
                [
                    "restore",
                    "--in",
                    str(age_file),
                    "--identity",
                    str(self.key_file),
                    "--db",
                    str(self.dest_db),
                ]
            )
        self.assertEqual(r_code, 5)

    def test_corrupted_manifest_fails_verify(self):
        """Битый манифест или отсутствие обязательных полей дают код 5."""
        code = export_db.main(
            [
                "export",
                "--db",
                str(self.src_db),
                "--recipient",
                "age1test",
                "--out",
                str(self.export_dir),
            ]
        )
        self.assertEqual(code, 0)
        age_file = list(self.export_dir.glob("*.db.age"))[0]
        manifest_file = age_file.with_name(f"{age_file.name}.json")

        # Невалидный JSON
        manifest_file.write_text("not a json", encoding="utf-8")
        err_buf = io.StringIO()
        with contextlib.redirect_stderr(err_buf):
            self.assertEqual(export_db.main(["verify", "--in", str(age_file)]), 5)

        # Удаление обязательного поля sha256
        manifest_file.write_text(json.dumps({"format_version": 1}), encoding="utf-8")
        err_buf = io.StringIO()
        with contextlib.redirect_stderr(err_buf):
            self.assertEqual(export_db.main(["verify", "--in", str(age_file)]), 5)

    def test_schema_newer_fails_without_force(self):
        """Если схема экспорта новее схемы целевой БД/кода — отказ с кодом 3, с --force-schema — успех."""
        # Создаём исходную БД с более новой версией схемы "utc_v2"
        newer_db = self.base_dir / "newer.db"
        self._populate_db(newer_db, tables=3, schema="utc_v2", user_version=0)

        # Целевая БД со старой схемой "utc_v1"
        self._populate_db(self.dest_db, tables=2, schema="utc_v1", user_version=0)
        dest_db_hash_before = hashlib.sha256(self.dest_db.read_bytes()).hexdigest()

        code = export_db.main(
            [
                "export",
                "--db",
                str(newer_db),
                "--recipient",
                "age1test",
                "--out",
                str(self.export_dir),
            ]
        )
        self.assertEqual(code, 0)
        age_file = list(self.export_dir.glob("*.db.age"))[0]

        # restore без --force-schema -> код 3
        err_buf = io.StringIO()
        with contextlib.redirect_stderr(err_buf):
            r_code = export_db.main(
                [
                    "restore",
                    "--in",
                    str(age_file),
                    "--identity",
                    str(self.key_file),
                    "--db",
                    str(self.dest_db),
                ]
            )
        self.assertEqual(r_code, 3)
        self.assertIn("НОВЕЕ схемы", err_buf.getvalue())
        # Целевая БД не тронута
        self.assertEqual(hashlib.sha256(self.dest_db.read_bytes()).hexdigest(), dest_db_hash_before)

        # restore c --force-schema -> код 0
        err_buf = io.StringIO()
        with contextlib.redirect_stderr(err_buf):
            r_code_force = export_db.main(
                [
                    "restore",
                    "--in",
                    str(age_file),
                    "--identity",
                    str(self.key_file),
                    "--db",
                    str(self.dest_db),
                    "--force-schema",
                ]
            )
        self.assertEqual(r_code_force, 0)
        self.assertIn("force-schema", err_buf.getvalue())
        # База обновлена
        with sqlite3.connect(str(self.dest_db)) as conn:
            val = conn.execute("SELECT value FROM app_meta WHERE key = 'timestamps_storage'").fetchone()[0]
            self.assertEqual(val, "utc_v2")

    def test_schema_older_warns_and_proceeds(self):
        """Если схема экспорта старее целевой БД — выводится предупреждение, но restore успешен."""
        older_db = self.base_dir / "older.db"
        self._populate_db(older_db, tables=2, schema="utc_v1", user_version=0)

        # Целевая база новее: "utc_v2"
        self._populate_db(self.dest_db, tables=3, schema="utc_v2", user_version=0)

        code = export_db.main(
            [
                "export",
                "--db",
                str(older_db),
                "--recipient",
                "age1test",
                "--out",
                str(self.export_dir),
            ]
        )
        self.assertEqual(code, 0)
        age_file = list(self.export_dir.glob("*.db.age"))[0]

        err_buf = io.StringIO()
        with contextlib.redirect_stderr(err_buf):
            r_code = export_db.main(
                [
                    "restore",
                    "--in",
                    str(age_file),
                    "--identity",
                    str(self.key_file),
                    "--db",
                    str(self.dest_db),
                ]
            )
        self.assertEqual(r_code, 0)
        self.assertIn("старее", err_buf.getvalue())

    def test_backup_failure_aborts_restore_code_4(self):
        """Если целевая БД существует, но снимок создать не удалось — отказ с кодом 4, БД не тронута."""
        # Создаём существующую целевую БД с совместимой схемой
        self._populate_db(self.dest_db, tables=3, schema="utc_v1", user_version=0)
        dest_hash_before = hashlib.sha256(self.dest_db.read_bytes()).hexdigest()

        code = export_db.main(
            [
                "export",
                "--db",
                str(self.src_db),
                "--recipient",
                "age1test",
                "--out",
                str(self.export_dir),
            ]
        )
        self.assertEqual(code, 0)
        age_file = list(self.export_dir.glob("*.db.age"))[0]

        # Мокаем snapshot так, чтобы он падал
        with patch("ops.aiqu.export_db.snapshot", side_effect=RuntimeError("сбой диска")):
            err_buf = io.StringIO()
            with contextlib.redirect_stderr(err_buf):
                r_code = export_db.main(
                    [
                        "restore",
                        "--in",
                        str(age_file),
                        "--identity",
                        str(self.key_file),
                        "--db",
                        str(self.dest_db),
                    ]
                )
            self.assertEqual(r_code, 4)
            self.assertIn("обязательный снимок", err_buf.getvalue())

        # Целевая БД не тронута
        self.assertEqual(hashlib.sha256(self.dest_db.read_bytes()).hexdigest(), dest_hash_before)

    def test_dry_run_does_not_modify_target_or_make_backup(self):
        """Флаг --dry-run выполняет шаги 1-3, не заменяет БД и не создаёт снимок."""
        self._populate_db(self.dest_db, tables=3, schema="utc_v1", user_version=0)
        dest_hash_before = hashlib.sha256(self.dest_db.read_bytes()).hexdigest()

        code = export_db.main(
            [
                "export",
                "--db",
                str(self.src_db),
                "--recipient",
                "age1test",
                "--out",
                str(self.export_dir),
            ]
        )
        self.assertEqual(code, 0)
        age_file = list(self.export_dir.glob("*.db.age"))[0]

        out_buf = io.StringIO()
        with contextlib.redirect_stdout(out_buf):
            r_code = export_db.main(
                [
                    "restore",
                    "--in",
                    str(age_file),
                    "--identity",
                    str(self.key_file),
                    "--db",
                    str(self.dest_db),
                    "--backup-dir",
                    str(self.backup_dir),
                    "--dry-run",
                ]
            )
        self.assertEqual(r_code, 0)
        self.assertIn("dry-run", out_buf.getvalue())
        # Хеш не изменился
        self.assertEqual(hashlib.sha256(self.dest_db.read_bytes()).hexdigest(), dest_hash_before)
        # Бэкап не создан
        self.assertFalse(self.backup_dir.exists() and any(self.backup_dir.iterdir()))

    def test_file_permissions_0600(self):
        """Экспортированные файлы и восстановленная БД имеют права 0600."""
        code = export_db.main(
            [
                "export",
                "--db",
                str(self.src_db),
                "--recipient",
                "age1test",
                "--out",
                str(self.export_dir),
            ]
        )
        self.assertEqual(code, 0)
        age_file = list(self.export_dir.glob("*.db.age"))[0]
        manifest_file = age_file.with_name(f"{age_file.name}.json")

        self.assertEqual(age_file.stat().st_mode & 0o777, 0o600)
        self.assertEqual(manifest_file.stat().st_mode & 0o777, 0o600)

        r_code = export_db.main(
            [
                "restore",
                "--in",
                str(age_file),
                "--identity",
                str(self.key_file),
                "--db",
                str(self.dest_db),
            ]
        )
        self.assertEqual(r_code, 0)
        self.assertEqual(self.dest_db.stat().st_mode & 0o777, 0o600)

    def test_missing_age_bin_returns_code_1(self):
        """Отсутствие age в PATH возвращает код 1 с понятным сообщением."""
        os.environ["AGE_BIN"] = "/nonexistent/binary/age"

        err_buf = io.StringIO()
        with contextlib.redirect_stderr(err_buf):
            code_exp = export_db.main(
                [
                    "export",
                    "--db",
                    str(self.src_db),
                    "--recipient",
                    "age1test",
                    "--out",
                    str(self.export_dir),
                ]
            )
        self.assertEqual(code_exp, 1)
        self.assertIn("установите age (apt install age)", err_buf.getvalue())

        err_buf = io.StringIO()
        with contextlib.redirect_stderr(err_buf):
            code_rest = export_db.main(
                [
                    "restore",
                    "--in",
                    "nonexistent.db.age",
                    "--identity",
                    str(self.key_file),
                    "--db",
                    str(self.dest_db),
                ]
            )
        self.assertEqual(code_rest, 1)
        self.assertIn("установите age (apt install age)", err_buf.getvalue())

    def test_active_wal_fails_restore_code_1(self):
        """Наличие непустого -wal файла блокирует восстановление с кодом 1."""
        code = export_db.main(
            [
                "export",
                "--db",
                str(self.src_db),
                "--recipient",
                "age1test",
                "--out",
                str(self.export_dir),
            ]
        )
        self.assertEqual(code, 0)
        age_file = list(self.export_dir.glob("*.db.age"))[0]

        # Создаём живой -wal
        self.dest_db.touch()
        wal_file = self.dest_db.with_name(f"{self.dest_db.name}-wal")
        wal_file.write_bytes(b"active wal data 12345")

        err_buf = io.StringIO()
        with contextlib.redirect_stderr(err_buf):
            r_code = export_db.main(
                [
                    "restore",
                    "--in",
                    str(age_file),
                    "--identity",
                    str(self.key_file),
                    "--db",
                    str(self.dest_db),
                ]
            )
        self.assertEqual(r_code, 1)
        self.assertIn("активный WAL-файл", err_buf.getvalue())

    def test_recipients_file_and_multiple_recipients(self):
        """Поддержка нескольких получателей и файла со списком ключей."""
        rf = self.base_dir / "recipients.txt"
        rf.write_text("# comment\nage1key2\nage1key3\n", encoding="utf-8")

        code = export_db.main(
            [
                "export",
                "--db",
                str(self.src_db),
                "--recipient",
                "age1key1",
                "--recipients-file",
                str(rf),
                "--out",
                str(self.export_dir),
            ]
        )
        self.assertEqual(code, 0)
        age_file = list(self.export_dir.glob("*.db.age"))[0]
        manifest = json.loads(age_file.with_name(f"{age_file.name}.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["age_recipients_count"], 3)

    def test_invalid_cli_arguments_returns_code_2(self):
        """Некорректные аргументы CLI возвращают код 2."""
        # Без получателя
        self.assertEqual(
            export_db.main(["export", "--db", str(self.src_db), "--out", str(self.export_dir)]),
            2,
        )
        # Недопустимый тег
        self.assertEqual(
            export_db.main(
                [
                    "export",
                    "--db",
                    str(self.src_db),
                    "--recipient",
                    "age1key",
                    "--tag",
                    "bad tag!",
                ]
            ),
            2,
        )
        # restore без identity
        self.assertEqual(
            export_db.main(["restore", "--in", "file.age", "--db", "bot.db"]),
            2,
        )

    @unittest.skipUnless(
        shutil.which("age") and shutil.which("age-keygen"),
        "Настоящие утилиты age и age-keygen не найдены в системе",
    )
    def test_real_age_binary_roundtrip(self):
        """Интеграционный тест с реальным бинарником age и age-keygen."""
        # Сбрасываем подмену AGE_BIN, чтобы использовался реальный age
        os.environ.pop("AGE_BIN", None)

        real_key_file = self.base_dir / "real_identity.txt"
        keygen_res = subprocess.run(
            ["age-keygen", "-o", str(real_key_file)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        # Извлекаем публичный ключ
        pub_key = None
        for line in keygen_res.stderr.decode("utf-8").splitlines():
            if line.startswith("Public key:"):
                pub_key = line.split(":", 1)[1].strip()
                break
        if not pub_key:
            # Читаем из комментариев файла
            for line in real_key_file.read_text(encoding="utf-8").splitlines():
                if line.startswith("# public key:"):
                    pub_key = line.split(":", 1)[1].strip()
                    break

        self.assertIsNotNone(pub_key)
        self.assertTrue(pub_key.startswith("age1"))

        real_export_dir = self.base_dir / "real_exports"
        code = export_db.main(
            [
                "export",
                "--db",
                str(self.src_db),
                "--recipient",
                pub_key,
                "--out",
                str(real_export_dir),
            ]
        )
        self.assertEqual(code, 0)
        real_age_file = list(real_export_dir.glob("*.db.age"))[0]

        # Verify
        self.assertEqual(export_db.main(["verify", "--in", str(real_age_file)]), 0)

        # Restore
        real_dest_db = self.base_dir / "real_restored.db"
        r_code = export_db.main(
            [
                "restore",
                "--in",
                str(real_age_file),
                "--identity",
                str(real_key_file),
                "--db",
                str(real_dest_db),
            ]
        )
        self.assertEqual(r_code, 0)
        with sqlite3.connect(str(real_dest_db)) as conn:
            verdict = conn.execute("PRAGMA quick_check").fetchone()[0]
            self.assertEqual(verdict, "ok")
            users = conn.execute("SELECT id, name FROM users").fetchall()
            self.assertEqual(users, [(1, "alice"), (2, "bob")])

    def test_empty_or_foreign_db_rejected_code_5(self):
        """Пункт 1: пустая база (0 таблиц) или чужая SQLite-БД (без app_meta) отвергаются при restore (код 5)."""
        self._populate_db(self.dest_db, tables=3, schema="utc_v1", user_version=0)
        dest_hash_before = hashlib.sha256(self.dest_db.read_bytes()).hexdigest()

        # Создаём БД без app_meta ("чужая" БД)
        foreign_db = self.base_dir / "foreign.db"
        with sqlite3.connect(str(foreign_db)) as conn:
            conn.execute("CREATE TABLE foreign_table (id INT)")
            conn.commit()

        # Попытка экспорта БД без app_meta должна отвергаться с кодом 1
        code_exp = export_db.main(
            ["export", "--db", str(foreign_db), "--recipient", "age1test", "--out", str(self.export_dir)]
        )
        self.assertEqual(code_exp, 1)

        # Создаём искусственный экспорт пустой БД с 0 таблиц
        empty_mem = sqlite3.connect(":memory:")
        empty_bytes = empty_mem.serialize()
        empty_mem.close()

        # Шифруем фейковым age
        prefix = b"age-encryption.org/v1\n-> fake-x25519\n\n"
        enc_empty = prefix + bytes(b ^ 0x5A for b in empty_bytes)
        self.export_dir.mkdir(parents=True, exist_ok=True)
        empty_age_path = self.export_dir / "empty-20260101-000000-migrate.db.age"
        empty_age_path.write_bytes(enc_empty)
        empty_manifest = {
            "format_version": 1,
            "created_at": "2026-01-01T00:00:00Z",
            "sha256": hashlib.sha256(enc_empty).hexdigest(),
            "size": len(enc_empty),
            "plaintext_size": len(empty_bytes),
            "sqlite_user_version": 0,
            "tables": 0,
            "app_schema": "empty",
            "age_recipients_count": 1,
        }
        empty_age_path.with_name(f"{empty_age_path.name}.json").write_text(
            json.dumps(empty_manifest, indent=2), encoding="utf-8"
        )

        err_buf = io.StringIO()
        with contextlib.redirect_stderr(err_buf):
            r_code = export_db.main(
                [
                    "restore",
                    "--in",
                    str(empty_age_path),
                    "--identity",
                    str(self.key_file),
                    "--db",
                    str(self.dest_db),
                ]
            )
        self.assertEqual(r_code, 5)
        self.assertIn("пуста (0 таблиц) или не является базой бота", err_buf.getvalue())
        # Целевая БД не тронута
        self.assertEqual(hashlib.sha256(self.dest_db.read_bytes()).hexdigest(), dest_hash_before)

    def test_newer_schema_with_same_utc_v1_fails_code_3(self):
        """Пункт 2: новые таблицы/колонки при том же utc_v1 блокируют restore с кодом 3 без --force-schema."""
        # Целевая БД: 2 таблицы (app_meta, users) с utc_v1
        self._populate_db(self.dest_db, tables=2, schema="utc_v1", user_version=0)
        dest_hash_before = hashlib.sha256(self.dest_db.read_bytes()).hexdigest()

        # Исходная БД: те же utc_v1, но 3 таблицы (notes)
        newer_db = self.base_dir / "newer_structure.db"
        self._populate_db(newer_db, tables=3, schema="utc_v1", user_version=0)

        code = export_db.main(
            ["export", "--db", str(newer_db), "--recipient", "age1test", "--out", str(self.export_dir)]
        )
        self.assertEqual(code, 0)
        age_file = list(self.export_dir.glob("newer_structure-*.db.age"))[0]

        # Без --force-schema -> код 3
        err_buf = io.StringIO()
        with contextlib.redirect_stderr(err_buf):
            r_code = export_db.main(
                [
                    "restore",
                    "--in",
                    str(age_file),
                    "--identity",
                    str(self.key_file),
                    "--db",
                    str(self.dest_db),
                ]
            )
        self.assertEqual(r_code, 3)
        self.assertIn("НОВЕЕ", err_buf.getvalue())
        self.assertIn("новые таблицы", err_buf.getvalue())
        self.assertEqual(hashlib.sha256(self.dest_db.read_bytes()).hexdigest(), dest_hash_before)

        # С --force-schema -> код 0
        r_force = export_db.main(
            [
                "restore",
                "--in",
                str(age_file),
                "--identity",
                str(self.key_file),
                "--db",
                str(self.dest_db),
                "--force-schema",
            ]
        )
        self.assertEqual(r_force, 0)
        with sqlite3.connect(str(self.dest_db)) as conn:
            tables = conn.execute(
                "SELECT count(*) FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            ).fetchone()[0]
            self.assertEqual(tables, 3)

    def test_active_shm_without_wal_blocks_restore_code_1(self):
        """Пункт 3: наличие файла -shm (при 0-байтном -wal) указывает на живой бот и блокирует restore."""
        self._populate_db(self.dest_db, tables=3, schema="utc_v1", user_version=0)
        dest_hash_before = hashlib.sha256(self.dest_db.read_bytes()).hexdigest()

        code = export_db.main(
            ["export", "--db", str(self.src_db), "--recipient", "age1test", "--out", str(self.export_dir)]
        )
        self.assertEqual(code, 0)
        age_file = list(self.export_dir.glob("source-*.db.age"))[0]

        # Создаём -wal размером 0 и непустой -shm
        wal_file = self.dest_db.with_name(f"{self.dest_db.name}-wal")
        wal_file.touch()
        shm_file = self.dest_db.with_name(f"{self.dest_db.name}-shm")
        shm_file.write_bytes(b"x" * 32768)

        err_buf = io.StringIO()
        with contextlib.redirect_stderr(err_buf):
            r_code = export_db.main(
                [
                    "restore",
                    "--in",
                    str(age_file),
                    "--identity",
                    str(self.key_file),
                    "--db",
                    str(self.dest_db),
                ]
            )
        self.assertEqual(r_code, 1)
        self.assertIn("активный WAL-файл / SHM-файл", err_buf.getvalue())
        self.assertIn("docker compose stop unagi-bot", err_buf.getvalue())
        self.assertEqual(hashlib.sha256(self.dest_db.read_bytes()).hexdigest(), dest_hash_before)

    def test_format_version_and_age_v1_header_validated_code_5(self):
        """Пункт 5: проверка format_version != 1 и заголовка отличного от age-encryption.org/v1 возвращает код 5."""
        code = export_db.main(
            ["export", "--db", str(self.src_db), "--recipient", "age1test", "--out", str(self.export_dir)]
        )
        self.assertEqual(code, 0)
        age_file = list(self.export_dir.glob("source-*.db.age"))[0]
        manifest_file = age_file.with_name(f"{age_file.name}.json")

        # 1. format_version: 2 -> код 5
        m = json.loads(manifest_file.read_text(encoding="utf-8"))
        m["format_version"] = 2
        manifest_file.write_text(json.dumps(m), encoding="utf-8")
        self.assertEqual(export_db.main(["verify", "--in", str(age_file)]), 5)

        # 2. Восстанавливаем format_version, портим заголовок age
        m["format_version"] = 1
        manifest_file.write_text(json.dumps(m), encoding="utf-8")
        raw = age_file.read_bytes()
        bad_header_raw = raw.replace(b"age-encryption.org/v1", b"age-encryption.org/v2")
        age_file.write_bytes(bad_header_raw)
        # Обновляем хеш и размер, чтобы проверить именно заголовок
        m["sha256"] = hashlib.sha256(bad_header_raw).hexdigest()
        m["size"] = len(bad_header_raw)
        manifest_file.write_text(json.dumps(m), encoding="utf-8")
        self.assertEqual(export_db.main(["verify", "--in", str(age_file)]), 5)

    def test_export_rejects_unencrypted_age_bin_output_code_1(self):
        """Пункт 6: если AGE_BIN выдаёт незашифрованные данные, экспорт отклоняется с кодом 1."""
        # Создаём обёртку-имитатор сломанного age (возвращающую входной поток без шифрования)
        broken_wrapper = self.base_dir / "broken_age"
        broken_wrapper.write_text(f"#!{sys.executable}\nimport sys\nsys.stdout.buffer.write(sys.stdin.buffer.read())\n")
        broken_wrapper.chmod(0o755)

        with patch.dict(os.environ, {"AGE_BIN": str(broken_wrapper)}):
            err_buf = io.StringIO()
            with contextlib.redirect_stderr(err_buf):
                code = export_db.main(
                    [
                        "export",
                        "--db",
                        str(self.src_db),
                        "--recipient",
                        "age1test",
                        "--out",
                        str(self.export_dir),
                        "--tag",
                        "brokenage",
                    ]
                )
            self.assertEqual(code, 1)
            self.assertIn("не содержит заголовок age-encryption.org/v1", err_buf.getvalue())
            # Файл не был записан
            broken_files = list(self.export_dir.glob("*brokenage*.db.age"))
            self.assertEqual(len(broken_files), 0)

    def test_no_plaintext_or_restore_tmp_leak_across_operations(self):
        """Пункт 7: отсутствие утечек plaintext и очистка *-restore-*.tmp при успешном restore, dry-run и сбоях."""
        code = export_db.main(
            ["export", "--db", str(self.src_db), "--recipient", "age1test", "--out", str(self.export_dir)]
        )
        self.assertEqual(code, 0)
        age_file = list(self.export_dir.glob("source-*.db.age"))[0]

        target_dir = self.base_dir / "restore_leak_test"
        target_dir.mkdir(parents=True, exist_ok=True)
        test_db = target_dir / "target.db"

        # 1. --dry-run
        self.assertEqual(
            export_db.main(
                [
                    "restore",
                    "--in",
                    str(age_file),
                    "--identity",
                    str(self.key_file),
                    "--db",
                    str(test_db),
                    "--dry-run",
                ]
            ),
            0,
        )
        self.assertFalse(list(target_dir.glob("*-restore-*.tmp")))
        self.assertFalse(test_db.exists())

        # 2. Успешный restore
        self.assertEqual(
            export_db.main(
                [
                    "restore",
                    "--in",
                    str(age_file),
                    "--identity",
                    str(self.key_file),
                    "--db",
                    str(test_db),
                ]
            ),
            0,
        )
        self.assertFalse(list(target_dir.glob("*-restore-*.tmp")))

        # 3. Сбой quick_check во временном файле: проверяем cleanup
        corrupt_age_file = self.export_dir / "corrupt-20260101-000000-corrupt.db.age"
        garbage_plain = b"this is not a valid sqlite database at all 1234567890"
        enc_garbage = b"age-encryption.org/v1\n-> fake-x25519\n\n" + bytes(b ^ 0x5A for b in garbage_plain)
        corrupt_age_file.write_bytes(enc_garbage)
        corrupt_manifest = {
            "format_version": 1,
            "created_at": "2026-01-01T00:00:00Z",
            "sha256": hashlib.sha256(enc_garbage).hexdigest(),
            "size": len(enc_garbage),
            "plaintext_size": len(garbage_plain),
            "sqlite_user_version": 0,
            "tables": 0,
            "app_schema": "empty",
            "age_recipients_count": 1,
        }
        corrupt_age_file.with_name(f"{corrupt_age_file.name}.json").write_text(
            json.dumps(corrupt_manifest), encoding="utf-8"
        )

        r_corrupt = export_db.main(
            [
                "restore",
                "--in",
                str(corrupt_age_file),
                "--identity",
                str(self.key_file),
                "--db",
                str(test_db),
            ]
        )
        self.assertEqual(r_corrupt, 5)
        self.assertFalse(list(target_dir.glob("*-restore-*.tmp")))

    def test_error_handling_and_prerestore_rotation(self):
        """Пункт 10: ошибки параметров не дают traceback, повторный экспорт не затирает файл, prerestore ротируются."""
        # 1. --out указывает на существующий файл (FileExistsError) -> код 1 без traceback
        existing_file = self.base_dir / "regular_file.txt"
        existing_file.touch()
        err_buf = io.StringIO()
        with contextlib.redirect_stderr(err_buf):
            code_out = export_db.main(
                ["export", "--db", str(self.src_db), "--recipient", "age1test", "--out", str(existing_file)]
            )
        self.assertEqual(code_out, 1)
        self.assertIn("не удалось создать каталог вывода", err_buf.getvalue())

        # 2. Манифест в виде JSON-списка [] -> код 5 без TypeError traceback
        self.export_dir.mkdir(parents=True, exist_ok=True)
        list_age = self.export_dir / "listmanifest-20260101-000000-tag.db.age"
        list_age.write_bytes(b"age-encryption.org/v1\n...")
        list_manifest = list_age.with_name(f"{list_age.name}.json")
        list_manifest.write_text("[]", encoding="utf-8")
        v_code, _, v_err = export_db.verify_export_file(list_age)
        self.assertEqual(v_code, 5)
        self.assertIn("JSON-объектом", v_err)

        # 3. Повторный экспорт в ту же секунду с тем же тегом завершается с кодом 1
        now_fixed = 1700000000.0
        code1 = export_db.main(
            ["export", "--db", str(self.src_db), "--recipient", "age1test", "--out", str(self.export_dir), "--tag", "dup"],
            now=now_fixed,
        )
        self.assertEqual(code1, 0)
        err_buf = io.StringIO()
        with contextlib.redirect_stderr(err_buf):
            code2 = export_db.main(
                ["export", "--db", str(self.src_db), "--recipient", "age1test", "--out", str(self.export_dir), "--tag", "dup"],
                now=now_fixed,
            )
        self.assertEqual(code2, 1)
        self.assertIn("файл экспорта уже существует", err_buf.getvalue())

        # 4. Пре-рестор снимки ротируются и сжимаются (тег prerestore)
        target = self.base_dir / "rotation_target.db"
        self._populate_db(target, tables=3, schema="utc_v1", user_version=0)
        age_file = list(self.export_dir.glob("source-*.db.age"))[0]
        snap_dir = self.base_dir / "prerestore_backups"

        # Восстанавливаем 3 раза с разными отметками времени
        for i in range(3):
            r_code = export_db.main(
                [
                    "restore",
                    "--in",
                    str(age_file),
                    "--identity",
                    str(self.key_file),
                    "--db",
                    str(target),
                    "--backup-dir",
                    str(snap_dir),
                ],
                now=now_fixed + (i * 100),
            )
            self.assertEqual(r_code, 0)

        # Проверяем, что снимки prerestore были сжаты: должно остаться 2 .db и 1 .db.gz
        raw_snaps = list(snap_dir.glob("*.db"))
        gz_snaps = list(snap_dir.glob("*.db.gz"))
        self.assertEqual(len(raw_snaps), 2)
        self.assertEqual(len(gz_snaps), 1)


if __name__ == "__main__":
    unittest.main()
