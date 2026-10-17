"""DB1 — соединение с БД без лишних PRAGMA и очистка короткими порциями."""

import os
import sqlite3
import tempfile
import unittest
from datetime import timedelta, timezone
from unittest.mock import patch

import data.database as database

UTC = timezone.utc


class DbTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp_dir.name, "test.db")
        self.orig_db_path = database.DB_PATH
        database.DB_PATH = self.db_path

    def tearDown(self):
        database._SECURED_DB_PATHS.discard(self.db_path)
        database.DB_PATH = self.orig_db_path
        self.tmp_dir.cleanup()

    def _trace_connect(self, statements):
        real_connect = sqlite3.connect

        def connect(*args, **kwargs):
            conn = real_connect(*args, **kwargs)
            conn.set_trace_callback(statements.append)
            return conn

        return patch.object(database.sqlite3, "connect", side_effect=connect)


class ConnectionPragmaTests(DbTestBase):
    def test_journal_mode_only_in_init_db(self):
        statements = []
        with self._trace_connect(statements):
            database.init_db()

        self.assertTrue(
            any("journal_mode" in statement.lower() for statement in statements),
            statements,
        )

        statements.clear()
        with self._trace_connect(statements):
            for _ in range(3):
                with database.get_connection() as conn:
                    conn.execute("SELECT 1").fetchone()

        self.assertFalse(
            any("journal_mode" in statement.lower() for statement in statements),
            statements,
        )

        # WAL персистентен в файле: последующие соединения его уже видят.
        with database.get_connection() as conn:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            cache_size = conn.execute("PRAGMA cache_size").fetchone()[0]

        self.assertEqual(mode.lower(), "wal")
        self.assertEqual(cache_size, -16000)

    def test_db_files_are_0600_and_chmod_is_not_per_connection(self):
        database.init_db()

        for suffix in ("", "-wal", "-shm"):
            path = f"{self.db_path}{suffix}"
            if os.path.exists(path):
                mode = os.stat(path).st_mode & 0o777
                self.assertEqual(mode, 0o600, f"{path} has {oct(mode)}")

        with patch.object(database.os, "chmod", wraps=os.chmod) as chmod_mock:
            for _ in range(5):
                with database.get_connection() as conn:
                    conn.execute("SELECT 1").fetchone()

        # init_db уже закрыл права; соединения не должны повторять chmod.
        self.assertEqual(chmod_mock.call_count, 0)

        for suffix in ("", "-wal", "-shm"):
            path = f"{self.db_path}{suffix}"
            if os.path.exists(path):
                mode = os.stat(path).st_mode & 0o777
                self.assertEqual(mode, 0o600, f"{path} has {oct(mode)}")

    def test_chmod_happens_once_for_path_without_init_db(self):
        with patch.object(database.os, "chmod", wraps=os.chmod) as chmod_mock:
            with database.get_connection() as conn:
                conn.execute("SELECT 1").fetchone()
            first_calls = chmod_mock.call_count
            self.assertGreater(first_calls, 0)

            for _ in range(3):
                with database.get_connection() as conn:
                    conn.execute("SELECT 1").fetchone()

        self.assertEqual(chmod_mock.call_count, first_calls)


class CleanupBatchingTests(DbTestBase):
    def setUp(self):
        super().setUp()
        database.init_db()
        self.now = database.utc_now()
        self.now_str = database.serialize_datetime(self.now)

    def _insert_cleanup_fixtures(self):
        now = self.now
        old_message = now - timedelta(days=database.MESSAGE_RETENTION_DAYS + 2)
        fresh_message = now - timedelta(hours=1)
        old_subscription = now - timedelta(
            days=database.SUBSCRIPTION_FAILURE_RETENTION_DAYS + 2
        )
        fresh_subscription = now - timedelta(hours=1)
        old_digest = now - timedelta(days=database.DIGEST_POST_RETENTION_DAYS + 2)
        fresh_digest = now - timedelta(hours=1)
        old_feed = now - timedelta(days=3)
        fresh_feed = now - timedelta(hours=1)
        old_undo = now - timedelta(days=2)
        fresh_undo = now - timedelta(hours=1)

        with database.get_connection() as conn:
            for i in range(10):
                old = database.serialize_datetime(old_message)
                conn.execute(
                    """
                    INSERT INTO scheduled_messages
                        (user_id, message_id, send_at, created_at, is_sent, delivery_status)
                    VALUES (?, ?, ?, ?, 1, 'sent')
                    """,
                    (1, 1000 + i, old, old),
                )
                conn.execute(
                    """
                    INSERT INTO scheduled_messages
                        (user_id, message_id, send_at, created_at, is_sent, delivery_status)
                    VALUES (?, ?, ?, ?, 0, 'failed_permanent')
                    """,
                    (1, 2000 + i, old, old),
                )

                old_sub = database.serialize_datetime(old_subscription)
                conn.execute(
                    """
                    INSERT INTO subscriptions
                        (user_id, channel_username, channel_title, period, created_at,
                         digest_status, last_attempt_at, is_disabled)
                    VALUES (?, ?, 'Old', 'day', ?, 'failed_permanent', ?, 1)
                    """,
                    (1, f"old_sub_{i}", old_sub, old_sub),
                )

                old_post = database.serialize_datetime(old_digest)
                old_scheduled = database.serialize_datetime(
                    old_digest + timedelta(minutes=i)
                )
                conn.execute(
                    """
                    INSERT INTO digest_posts
                        (user_id, channel_username, channel_title, post_text, post_link,
                         post_time, created_at)
                    VALUES (1, 'old_chan', 'Old', 'text', ?, ?, ?)
                    """,
                    (f"https://t.me/old/{i}", old_post, old_post),
                )
                conn.execute(
                    """
                    INSERT INTO channel_posts
                        (channel_username, post_id, post_text, post_link, post_time,
                         source, created_at)
                    VALUES ('chan', ?, 'text', ?, ?, 'web', ?)
                    """,
                    (i, f"https://t.me/chan/{i}", old_post, old_post),
                )
                conn.execute(
                    """
                    INSERT INTO digest_executions
                        (user_id, period, scheduled_at, status, created_at)
                    VALUES (1, 'day', ?, 'delivered', ?)
                    """,
                    (old_scheduled, old_post),
                )
                conn.execute(
                    """
                    INSERT INTO digest_alerts
                        (user_id, period, scheduled_at, alert_type, sent_at, created_at)
                    VALUES (1, 'day', ?, 'failure', ?, ?)
                    """,
                    (old_scheduled, old_post, old_post),
                )
                old_day = (now - timedelta(days=3 + i)).strftime("%Y-%m-%d")
                conn.execute(
                    "INSERT INTO ai_usage (user_id, usage_day, request_count) VALUES (1, ?, 1)",
                    (old_day,),
                )
                conn.execute(
                    """
                    INSERT INTO feed_editions
                        (user_id, period, window_start, window_end, subs_hash,
                         telegraph_url, post_count, channel_count, created_at)
                    VALUES (1, 'day', ?, ?, ?, ?, 1, 1, ?)
                    """,
                    (
                        database.serialize_datetime(old_feed),
                        database.serialize_datetime(old_feed),
                        f"old_hash_{i}",
                        f"https://telegra.ph/old-{i}",
                        database.serialize_datetime(old_feed),
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO reschedule_undo_history
                        (new_pending_id, user_id, old_reminder_id, created_timestamp)
                    VALUES (?, 1, ?, ?)
                    """,
                    (1000 + i, 100 + i, old_undo.timestamp()),
                )

            for i in range(2):
                fresh = database.serialize_datetime(fresh_message)
                conn.execute(
                    """
                    INSERT INTO scheduled_messages
                        (user_id, message_id, send_at, created_at, is_sent, delivery_status)
                    VALUES (?, ?, ?, ?, 1, 'sent')
                    """,
                    (1, 3000 + i, fresh, fresh),
                )
                conn.execute(
                    """
                    INSERT INTO scheduled_messages
                        (user_id, message_id, send_at, created_at, is_sent, delivery_status)
                    VALUES (?, ?, ?, ?, 0, 'failed_permanent')
                    """,
                    (1, 4000 + i, fresh, fresh),
                )

                fresh_sub = database.serialize_datetime(fresh_subscription)
                conn.execute(
                    """
                    INSERT INTO subscriptions
                        (user_id, channel_username, channel_title, period, created_at,
                         digest_status, last_attempt_at, is_disabled)
                    VALUES (?, ?, 'Fresh', 'day', ?, 'failed_permanent', ?, 1)
                    """,
                    (1, f"fresh_sub_{i}", fresh_sub, fresh_sub),
                )

                fresh_post = database.serialize_datetime(fresh_digest)
                fresh_scheduled = database.serialize_datetime(
                    fresh_digest + timedelta(minutes=i)
                )
                conn.execute(
                    """
                    INSERT INTO digest_posts
                        (user_id, channel_username, channel_title, post_text, post_link,
                         post_time, created_at)
                    VALUES (1, 'fresh_chan', 'Fresh', 'text', ?, ?, ?)
                    """,
                    (f"https://t.me/fresh/{i}", fresh_post, fresh_post),
                )
                conn.execute(
                    """
                    INSERT INTO channel_posts
                        (channel_username, post_id, post_text, post_link, post_time,
                         source, created_at)
                    VALUES ('chan', ?, 'text', ?, ?, 'web', ?)
                    """,
                    (100 + i, f"https://t.me/chan/fresh-{i}", fresh_post, fresh_post),
                )
                conn.execute(
                    """
                    INSERT INTO digest_executions
                        (user_id, period, scheduled_at, status, created_at)
                    VALUES (1, 'day', ?, 'delivered', ?)
                    """,
                    (fresh_scheduled, fresh_post),
                )
                conn.execute(
                    """
                    INSERT INTO digest_alerts
                        (user_id, period, scheduled_at, alert_type, sent_at, created_at)
                    VALUES (1, 'day', ?, 'failure', ?, ?)
                    """,
                    (fresh_scheduled, fresh_post, fresh_post),
                )
                fresh_day = (now - timedelta(days=1) + timedelta(days=i)).strftime(
                    "%Y-%m-%d"
                )
                conn.execute(
                    "INSERT INTO ai_usage (user_id, usage_day, request_count) VALUES (1, ?, 1)",
                    (fresh_day,),
                )
                fresh_feed_str = database.serialize_datetime(fresh_feed)
                conn.execute(
                    """
                    INSERT INTO feed_editions
                        (user_id, period, window_start, window_end, subs_hash,
                         telegraph_url, post_count, channel_count, created_at)
                    VALUES (1, 'day', ?, ?, ?, ?, 1, 1, ?)
                    """,
                    (
                        fresh_feed_str,
                        fresh_feed_str,
                        f"fresh_hash_{i}",
                        f"https://telegra.ph/fresh-{i}",
                        fresh_feed_str,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO reschedule_undo_history
                        (new_pending_id, user_id, old_reminder_id, created_timestamp)
                    VALUES (?, 1, ?, ?)
                    """,
                    (2000 + i, 200 + i, fresh_undo.timestamp()),
                )

            # T26: строки без created_timestamp не удаляются молча.
            conn.execute(
                """
                INSERT INTO reschedule_undo_history
                    (new_pending_id, user_id, old_reminder_id, created_timestamp)
                VALUES (9999, 1, 999, NULL)
                """
            )
            conn.commit()

    def test_batches_delete_only_expired_rows_and_count_correctly(self):
        self._insert_cleanup_fixtures()

        with patch.object(database, "CLEANUP_BATCH_SIZE", 3):
            stats = database.cleanup_old_records(self.now_str)

        self.assertEqual(
            stats,
            {
                "scheduled_sent_deleted": 10,
                "scheduled_failed_deleted": 10,
                "subscriptions_failed_deleted": 10,
                "digest_posts_deleted": 10,
                "channel_posts_deleted": 10,
                "digest_executions_deleted": 10,
                "digest_alerts_deleted": 10,
                "ai_usage_deleted": 10,
                "feed_editions_deleted": 10,
                "reschedule_undo_deleted": 10,
            },
        )

        with database.get_connection() as conn:
            for table, expected in (
                ("scheduled_messages", 4),
                ("subscriptions", 2),
                ("digest_posts", 2),
                ("channel_posts", 2),
                ("digest_executions", 2),
                ("digest_alerts", 2),
                ("ai_usage", 2),
                ("feed_editions", 2),
                ("reschedule_undo_history", 3),
            ):
                count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                self.assertEqual(count, expected, table)

            null_undo = conn.execute(
                "SELECT COUNT(*) FROM reschedule_undo_history WHERE created_timestamp IS NULL"
            ).fetchone()[0]
            self.assertEqual(null_undo, 1)

    def test_cleanup_commits_between_batches(self):
        now = self.now
        old = now - timedelta(days=database.DIGEST_POST_RETENTION_DAYS + 2)
        old_str = database.serialize_datetime(old)
        with database.get_connection() as conn:
            for i in range(10):
                conn.execute(
                    """
                    INSERT INTO digest_posts
                        (user_id, channel_username, channel_title, post_text, post_link,
                         post_time, created_at)
                    VALUES (1, 'chan', 'Chan', 'text', ?, ?, ?)
                    """,
                    (f"https://t.me/chan/{i}", old_str, old_str),
                )
            conn.commit()

        real_connect = sqlite3.connect
        seen_counts = []

        def connect(*args, **kwargs):
            conn = real_connect(*args, **kwargs)

            def trace(statement):
                if statement.lstrip().upper().startswith("DELETE FROM DIGEST_POSTS"):
                    observer = real_connect(database.DB_PATH)
                    try:
                        count = observer.execute(
                            "SELECT COUNT(*) FROM digest_posts"
                        ).fetchone()[0]
                    finally:
                        observer.close()
                    seen_counts.append(count)

            conn.set_trace_callback(trace)
            return conn

        with patch.object(database.sqlite3, "connect", side_effect=connect):
            with patch.object(database, "CLEANUP_BATCH_SIZE", 3):
                stats = database.cleanup_old_records(self.now_str)

        self.assertEqual(stats["digest_posts_deleted"], 10)
        # Второе соединение видит результат каждого коммита: 10 → 7 → 4 → 1.
        self.assertEqual(seen_counts, [10, 7, 4, 1])


class AddDigestPostsTests(DbTestBase):
    def test_deduplicates_and_returns_inserted_count(self):
        database.init_db()
        now = database.utc_now()
        posts = [
            {"time": now - timedelta(hours=1), "text": "first", "link": "https://t.me/c/1"},
            {"time": now - timedelta(hours=2), "text": "second", "link": "https://t.me/c/2"},
            {"time": None, "text": "third", "link": "https://t.me/c/3"},
        ]

        self.assertEqual(
            database.add_digest_posts(7, "@Chan", "Channel", posts),
            3,
        )
        self.assertEqual(
            database.add_digest_posts(7, "@Chan", "Channel", posts),
            0,
        )
        # Дубликат внутри одного вызова тоже игнорируется.
        self.assertEqual(
            database.add_digest_posts(
                8,
                "chan",
                "Channel",
                [
                    {"time": now, "text": "x", "link": "https://t.me/c/4"},
                    {"time": now, "text": "y", "link": "https://t.me/c/4"},
                ],
            ),
            1,
        )
        self.assertEqual(database.add_digest_posts(9, "chan", "Channel", []), 0)

        with database.get_connection() as conn:
            rows = conn.execute(
                """
                SELECT channel_username, post_text, post_link, post_time
                FROM digest_posts WHERE user_id = 7 ORDER BY post_link
                """
            ).fetchall()
            deduped_count = conn.execute(
                "SELECT COUNT(*) FROM digest_posts WHERE user_id = 8"
            ).fetchone()[0]

        self.assertEqual([row["post_text"] for row in rows], ["first", "second", "third"])
        self.assertTrue(all(row["channel_username"] == "chan" for row in rows))
        self.assertIsNotNone(rows[2]["post_time"])
        self.assertEqual(deduped_count, 1)


if __name__ == "__main__":
    unittest.main()
