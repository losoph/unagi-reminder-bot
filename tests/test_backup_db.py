"""Проверки снимка SQLite перед деплоем (ops/aiqu/backup_db.py)."""

import contextlib
import importlib.util
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path

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
        with sqlite3.connect(self.db) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("CREATE TABLE reminders (id INTEGER PRIMARY KEY, text TEXT)")
            conn.executemany("INSERT INTO reminders (text) VALUES (?)", [("a",), ("b",), ("c",)])
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

    def test_main_prints_summary_and_rotates(self):
        self.dir.mkdir()
        for index in range(5):
            (self.dir / f"bot_data-2026010{index + 1}-000000-predeploy.db").write_bytes(b"x")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = backup_db.main(["--db", str(self.db), "--dir", str(self.dir), "--keep", "3"])
        self.assertEqual(code, 0)
        self.assertIn("backup ok", out.getvalue())
        self.assertEqual(len(list(self.dir.glob("*predeploy.db"))), 3)

    def test_bad_arguments_exit_2(self):
        self.assertEqual(backup_db.main(["--db", str(self.db), "--keep", "0"]), 2)
        self.assertEqual(backup_db.main(["--db", str(self.db), "--tag", "../evil"]), 2)


if __name__ == "__main__":
    unittest.main()
