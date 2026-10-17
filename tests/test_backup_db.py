"""Проверки снимка SQLite перед деплоем (ops/aiqu/backup_db.py)."""

import contextlib
import gzip
import importlib.util
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("backup_db", ROOT / "ops" / "aiqu" / "backup_db.py")
backup_db = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup_db)


class BackupDbTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / "bot_data.db"
        # `with sqlite3.connect()` только коммитит, но не закрывает: живое соединение делит WAL-индекс внутри процесса и маскирует порчу файла.
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("CREATE TABLE reminders (id INTEGER PRIMARY KEY, text TEXT)")
            conn.executemany("INSERT INTO reminders (text) VALUES (?)", [("a",), ("b",), ("c",)])
            conn.commit()
        self.dir = self.root / "backups"

    def test_snapshot_is_consistent_copy_with_private_mode(self):
        path, size, tables = backup_db.snapshot(self.db, self.dir, "predeploy")
        self.assertEqual((tables, path.parent), (1, self.dir))
        self.assertGreater(size, 0)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        with sqlite3.connect(path) as copy:
            self.assertEqual(copy.execute("SELECT count(*) FROM reminders").fetchone()[0], 3)
        self.assertEqual(list(self.dir.glob("*.partial")), [])

    def test_missing_database_is_not_created_and_fails(self):
        missing = self.root / "nope.db"
        self.assertEqual(backup_db.main(["--db", str(missing)]), 1)
        self.assertFalse(missing.exists())

    def test_corrupt_database_leaves_no_snapshot(self):
        for suffix in ("-wal", "-shm"):  # иначе SQLite восстановит страницу из хвоста WAL
            Path(f"{self.db}{suffix}").unlink(missing_ok=True)
        self.db.write_bytes(b"not a sqlite file" * 100)
        self.assertEqual(backup_db.main(["--db", str(self.db), "--dir", str(self.dir)]), 1)
        self.assertEqual(list(self.dir.glob("*")), [])

    def test_rotation_keeps_newest_of_same_tag_and_spares_manual_copies(self):
        self.dir.mkdir()
        for stamp in ("20260101-000000", "20260102-000000", "20260103-000000"):
            (self.dir / f"bot_data-{stamp}-predeploy.db").write_bytes(b"x")
        manual = self.dir / "bot_data-20260101-0000-pre-lenta.db"
        manual.write_bytes(b"manual")
        other_tag = self.dir / "bot_data-20260101-000000-weekly.db"
        other_tag.write_bytes(b"weekly")
        self.assertEqual(backup_db.rotate(self.db, self.dir, "predeploy", 2), 1)
        left = sorted(p.name for p in self.dir.glob("*predeploy.db"))
        self.assertEqual(left, ["bot_data-20260102-000000-predeploy.db", "bot_data-20260103-000000-predeploy.db"])
        self.assertTrue(manual.exists() and other_tag.exists())

    def test_rotation_counts_both_suffixes_together(self):
        self.dir.mkdir()
        files = [
            "bot_data-20260101-000000-predeploy.db.gz",
            "bot_data-20260102-000000-predeploy.db.gz",
            "bot_data-20260103-000000-predeploy.db.gz",
            "bot_data-20260104-000000-predeploy.db",
            "bot_data-20260105-000000-predeploy.db",
        ]
        for name in files:
            (self.dir / name).write_bytes(b"dummy")
        manual = self.dir / "bot_data-manual.db"
        manual.write_bytes(b"manual")

        removed = backup_db.rotate(self.db, self.dir, "predeploy", keep=3)
        self.assertEqual(removed, 2)
        remaining = sorted(p.name for p in self.dir.iterdir() if "predeploy" in p.name)
        self.assertEqual(
            remaining,
            [
                "bot_data-20260103-000000-predeploy.db.gz",
                "bot_data-20260104-000000-predeploy.db",
                "bot_data-20260105-000000-predeploy.db",
            ],
        )
        self.assertTrue(manual.exists())

    def test_five_snapshots_series_two_raw_three_gz_and_newest_is_raw(self):
        base_time = 1768000000.0
        for i in range(5):
            code = backup_db.main(
                ["--db", str(self.db), "--dir", str(self.dir)],
                now=base_time + i * 100,
            )
            self.assertEqual(code, 0)

        raw_files = sorted(self.dir.glob("bot_data-*-predeploy.db"))
        gz_files = sorted(self.dir.glob("bot_data-*-predeploy.db.gz"))
        self.assertEqual(len(raw_files), 2)
        self.assertEqual(len(gz_files), 3)

        # prod_check.sh берёт newest="$(ls -1t storage/backups/*.db 2>/dev/null | head -n 1)"
        all_snapshots = sorted(self.dir.glob("bot_data-*-predeploy.db*"))
        self.assertEqual(len(all_snapshots), 5)
        # Самый последний снимок по времени — несжатый .db
        newest_overall = all_snapshots[-1]
        self.assertEqual(newest_overall, raw_files[-1])
        self.assertTrue(newest_overall.name.endswith(".db"))
        self.assertFalse(newest_overall.name.endswith(".gz"))

    def test_unpacked_db_gz_is_valid_sqlite_with_same_data(self):
        base_time = 1768000000.0
        for i in range(3):
            code = backup_db.main(
                ["--db", str(self.db), "--dir", str(self.dir)],
                now=base_time + i * 100,
            )
            self.assertEqual(code, 0)

        gz_files = sorted(self.dir.glob("bot_data-*-predeploy.db.gz"))
        self.assertEqual(len(gz_files), 1)
        gz_path = gz_files[0]
        unpacked_path = self.root / "unpacked.db"
        with gzip.open(gz_path, "rb") as f_in, open(unpacked_path, "wb") as f_out:
            f_out.write(f_in.read())

        with contextlib.closing(sqlite3.connect(unpacked_path)) as conn:
            verdict = conn.execute("PRAGMA quick_check").fetchone()[0]
            self.assertEqual(verdict, "ok")
            rows = conn.execute("SELECT text FROM reminders ORDER BY id").fetchall()
            self.assertEqual(rows, [("a",), ("b",), ("c",)])

    def test_compression_failure_exits_0_and_source_intact_and_no_partial(self):
        base_time = 1768000000.0
        # 2 снимка остаются несжатыми (keep-raw=2)
        for i in range(2):
            self.assertEqual(
                backup_db.main(["--db", str(self.db), "--dir", str(self.dir)], now=base_time + i * 100),
                0,
            )
        initial_raw = sorted(self.dir.glob("*.db"))
        self.assertEqual(len(initial_raw), 2)
        oldest_raw = initial_raw[0]

        # 3-й снимок пытается сжать самый старый, но gzip ломается
        err = io.StringIO()
        with (
            patch("ops.aiqu.backup_db.gzip.open", side_effect=OSError("симуляция сбоя диска при сжатии")),
            contextlib.redirect_stderr(err),
        ):
            code = backup_db.main(["--db", str(self.db), "--dir", str(self.dir)], now=base_time + 200)

        self.assertEqual(code, 0)
        self.assertIn("ПРЕДУПРЕЖДЕНИЕ", err.getvalue())
        self.assertTrue(oldest_raw.exists())
        self.assertEqual(list(self.dir.glob("*.partial")), [])

    def test_unexpected_cleanup_error_after_a_good_snapshot_does_not_fail_the_deploy(self):
        # Снимок создан и проверен; любой сбой уборки (ротация, сжатие, подсчёт) не должен давать код 1 и останавливать деплой.
        err = io.StringIO()
        with (
            patch.object(backup_db, "rotate", side_effect=PermissionError("нет доступа к каталогу")),  # модуль загружен по пути, не как ops.aiqu.backup_db
            contextlib.redirect_stderr(err),
            contextlib.redirect_stdout(io.StringIO()) as out,
        ):
            code = backup_db.main(["--db", str(self.db), "--dir", str(self.dir)], now=1768000000.0)
        self.assertEqual(code, 0)
        self.assertIn("уборка снимков не завершена", err.getvalue())
        self.assertIn("backup ok", out.getvalue())
        self.assertEqual(len(list(self.dir.glob("*.db"))), 1)

    def test_manual_files_are_untouched(self):
        self.dir.mkdir()
        manual1 = self.dir / "bot_data-manual.db"
        manual1.write_bytes(b"manual content 1")
        manual2 = self.dir / "custom-backup.db"
        manual2.write_bytes(b"manual content 2")

        base_time = 1768000000.0
        for i in range(4):
            self.assertEqual(
                backup_db.main(["--db", str(self.db), "--dir", str(self.dir), "--keep", "3"], now=base_time + i * 100),
                0,
            )

        self.assertTrue(manual1.exists())
        self.assertEqual(manual1.read_bytes(), b"manual content 1")
        self.assertTrue(manual2.exists())
        self.assertEqual(manual2.read_bytes(), b"manual content 2")

    def test_directory_size_noticeably_smaller_than_uncompressed_sum(self):
        # Наполняем БД синтетическими данными с повторяющимися строками
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            conn.execute("CREATE TABLE big_data (id INTEGER PRIMARY KEY, payload TEXT)")
            sample_text = "lorem ipsum dolor sit amet " * 50
            conn.executemany("INSERT INTO big_data (payload) VALUES (?)", [(sample_text,) for _ in range(300)])
            conn.commit()

        base_time = 1768000000.0
        for i in range(5):
            self.assertEqual(
                backup_db.main(["--db", str(self.db), "--dir", str(self.dir)], now=base_time + i * 100),
                0,
            )

        raw_files = list(self.dir.glob("*predeploy.db"))
        gz_files = list(self.dir.glob("*predeploy.db.gz"))
        self.assertEqual((len(raw_files), len(gz_files)), (2, 3))

        raw_size = raw_files[0].stat().st_size
        gz_size = gz_files[0].stat().st_size
        compression_ratio = raw_size / gz_size
        # SQLite с текстовыми данными в gzip сжимается минимум в 3-5 раз
        self.assertGreater(compression_ratio, 2.5)

        total_actual = sum(p.stat().st_size for p in self.dir.iterdir() if p.is_file())
        uncompressed_sum = 5 * raw_size
        self.assertLess(total_actual, uncompressed_sum * 0.6)

    def test_main_prints_summary_and_rotates(self):
        base_time = 1768000000.0
        for i in range(5):
            code = backup_db.main(
                ["--db", str(self.db), "--dir", str(self.dir), "--keep", "3"],
                now=base_time + i * 100,
            )
            self.assertEqual(code, 0)

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = backup_db.main(
                ["--db", str(self.db), "--dir", str(self.dir), "--keep", "3"],
                now=base_time + 500,
            )
        self.assertEqual(code, 0)
        output = out.getvalue().strip()
        self.assertIn("backup ok", output)
        self.assertIn("compressed=", output)
        self.assertIn("raw_kept=2", output)
        self.assertIn("rotated_out=1", output)
        self.assertIn("dir_total=", output)

        # Проверяем, что формат вывода строго парсится как key=value
        parts = output.split()
        self.assertEqual(parts[:2], ["backup", "ok"])
        kv = dict(item.split("=") for item in parts[2:])
        self.assertIn("path", kv)
        self.assertIn("size", kv)
        self.assertIn("tables", kv)
        self.assertEqual(kv["rotated_out"], "1")
        self.assertEqual(kv["raw_kept"], "2")
        self.assertTrue(int(kv["dir_total"]) > 0)

    def test_empty_database_with_zero_tables_fails(self):
        empty_db = self.root / "empty.db"
        with contextlib.closing(sqlite3.connect(empty_db)) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.commit()
        self.assertEqual(backup_db.main(["--db", str(empty_db), "--dir", str(self.dir)]), 1)
        self.assertEqual(list(self.dir.glob("*.db")), [])

    def test_bad_arguments_exit_2(self):
        self.assertEqual(backup_db.main(["--db", str(self.db), "--keep", "0"]), 2)
        self.assertEqual(backup_db.main(["--db", str(self.db), "--keep-raw", "0"]), 2)
        self.assertEqual(backup_db.main(["--db", str(self.db), "--min-tables", "0"]), 2)
        self.assertEqual(backup_db.main(["--db", str(self.db), "--tag", "../evil"]), 2)


if __name__ == "__main__":
    unittest.main()
