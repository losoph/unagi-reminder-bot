"""OPS1: запросы цифр для решений владельца — только чтение и верные числа."""

import contextlib
import hashlib
import os

os.environ.setdefault("BOT_TOKEN", "123456:TEST_TOKEN_FOR_UNIT_TESTS")

import sqlite3  # noqa: E402
import tempfile  # noqa: E402
import unittest  # noqa: E402
from datetime import timedelta  # noqa: E402
from pathlib import Path  # noqa: E402

from data import database as db  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
STATS_SQL = ROOT / "ops" / "aiqu" / "prod_stats.sql"


def sql_statements() -> list[str]:
    return [
        line.strip()
        for line in STATS_SQL.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("--")
    ]


class ProdStatsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = os.path.join(self.tmp.name, "prod_stats.db")
        original_db_path = db.DB_PATH
        db.DB_PATH = self.db_path
        self.addCleanup(setattr, db, "DB_PATH", original_db_path)
        db.init_db()

        now = db.utc_now()

        def at(days_ago: int) -> str:
            return db.serialize_datetime(now - timedelta(days=days_ago))

        reminders = [
            (1, at(5), "sent", 1),
            (1, at(5), "failed_permanent", 0),
            (1, at(60), "failed_permanent", 0),
        ]
        subscriptions = [
            # Один канал в двух тегах (и периодах) у пользователя 1.
            (1, "alpha", "Alpha", "daily", "work", 0),
            (1, "alpha", "Alpha", "weekly", "personal", 0),
            # Один канал в daily и weekly с одинаковым тегом: конфликт по периоду.
            (1, "beta", "Beta", "daily", "work", 0),
            (1, "beta", "Beta", "weekly", "work", 0),
            # Нормальные активные подписки, в том числе тот же канал у другого пользователя.
            (2, "gamma", "Gamma", "daily", None, 0),
            (3, "alpha", "Alpha", "daily", "work", 0),
            (3, "delta", "Delta", "monthly", "news", 0),
            # Отключённая подписка не должна попадать в цифры.
            (1, "alpha", "Alpha", "monthly", "archive", 1),
        ]
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            conn.executemany(
                "INSERT INTO scheduled_messages (user_id, created_at, delivery_status, is_sent) VALUES (?, ?, ?, ?)",
                reminders,
            )
            conn.executemany(
                """
                INSERT INTO subscriptions
                    (user_id, channel_username, channel_title, period, tag, is_disabled, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [(*row, at(1)) for row in subscriptions],
            )
            conn.commit()
            # Читаем уже основной файл без хвоста WAL, чтобы хеш до/после был показателен.
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def file_hash(self) -> str:
        return hashlib.sha256(Path(self.db_path).read_bytes()).hexdigest()

    def test_each_query_returns_expected_numbers_and_does_not_change_db(self):
        expected = {
            "reminders_failed_30d": 1,
            "reminders_total_30d": 2,
            "subs_active": 7,
            "users_with_subs": 3,
            "channels_multi_tag_or_period": 2,
        }
        statements = sql_statements()
        self.assertEqual(len(statements), len(expected))
        self.assertTrue(all(statement.upper().startswith("SELECT ") for statement in statements))

        before = self.file_hash()
        actual = {}
        uri = f"file:{Path(self.db_path).as_posix()}?mode=ro"
        with contextlib.closing(sqlite3.connect(uri, uri=True)) as conn:
            for statement in statements:
                row = conn.execute(statement).fetchone()
                self.assertIsNotNone(row)
                name, separator, value = row[0].partition("=")
                self.assertEqual(separator, "=")
                self.assertNotIn(name, actual)
                actual[name] = int(value)
        after = self.file_hash()

        self.assertEqual(actual, expected)
        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
