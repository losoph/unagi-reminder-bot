import os
import tempfile
import unittest
from datetime import datetime, timezone

os.environ.setdefault("BOT_TOKEN", "123456:TEST_TOKEN_FOR_UNIT_TESTS")

from data import database as db


class DigestExecutionModelTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = db.DB_PATH
        db.DB_PATH = os.path.join(self.temp_dir.name, "test_exec.db")
        db.init_db()

    def tearDown(self):
        db.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def test_record_digest_execution_start_and_delivered(self):
        user_id = 101
        period = "daily"
        scheduled_at = "2026-09-29 07:00:00"

        exec_id = db.record_digest_execution_start(user_id, period, scheduled_at)
        self.assertIsInstance(exec_id, int)

        rec = db.get_digest_execution(user_id, period, scheduled_at)
        self.assertIsNotNone(rec)
        self.assertEqual(rec["status"], "retrying")
        self.assertEqual(rec["retry_count"], 0)

        db.record_digest_execution_finish(
            exec_id,
            status="delivered",
            posts_count=10,
            channels_count=3,
            channels_failed=0,
            channels_partial=0,
        )

        rec_after = db.get_digest_execution(user_id, period, scheduled_at)
        self.assertEqual(rec_after["status"], "delivered")
        self.assertEqual(rec_after["posts_count"], 10)
        self.assertEqual(rec_after["channels_count"], 3)
        self.assertIsNotNone(rec_after["finished_at"])

    def test_empty_edition_is_empty_status_not_error(self):
        user_id = 102
        period = "weekly"
        scheduled_at = "2026-09-29 08:00:00"

        exec_id = db.record_digest_execution_start(user_id, period, scheduled_at)
        db.record_digest_execution_finish(
            exec_id,
            status="empty",
            posts_count=0,
            channels_count=5,
            channels_failed=0,
            channels_partial=0,
        )

        rec = db.get_digest_execution(user_id, period, scheduled_at)
        self.assertEqual(rec["status"], "empty")
        self.assertEqual(rec["posts_count"], 0)
        self.assertEqual(rec["retry_count"], 0)
        self.assertIsNone(rec["error_message"])

    def test_partial_edition_status(self):
        user_id = 103
        period = "daily"
        scheduled_at = "2026-09-29 07:00:00"

        exec_id = db.record_digest_execution_start(user_id, period, scheduled_at)
        db.record_digest_execution_finish(
            exec_id,
            status="partial",
            posts_count=4,
            channels_count=2,
            channels_failed=0,
            channels_partial=1,
        )

        rec = db.get_digest_execution(user_id, period, scheduled_at)
        self.assertEqual(rec["status"], "partial")
        self.assertEqual(rec["channels_partial"], 1)

    def test_retrying_and_failed_increments_retry_count(self):
        user_id = 104
        period = "daily"
        scheduled_at = "2026-09-29 07:00:00"

        exec_id = db.record_digest_execution_start(user_id, period, scheduled_at)
        db.record_digest_execution_finish(
            exec_id,
            status="retrying",
            channels_count=2,
            channels_failed=2,
            error_message="Network timeout",
        )

        rec = db.get_digest_execution(user_id, period, scheduled_at)
        self.assertEqual(rec["status"], "retrying")
        self.assertEqual(rec["retry_count"], 1)
        self.assertIn("Network timeout", rec["error_message"])

        # Another attempt finishes with failed
        db.record_digest_execution_finish(
            exec_id,
            status="failed",
            channels_count=2,
            channels_failed=2,
            error_message="Retries exhausted",
        )

        rec2 = db.get_digest_execution(user_id, period, scheduled_at)
        self.assertEqual(rec2["status"], "failed")
        self.assertEqual(rec2["retry_count"], 2)


class ChannelCursorAndBacklogTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = db.DB_PATH
        db.DB_PATH = os.path.join(self.temp_dir.name, "test_cursors.db")
        db.init_db()

    def tearDown(self):
        db.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def test_update_and_get_channel_scan_cursor(self):
        self.assertEqual(db.get_channel_scan_cursor("test_chan"), (None, None))
        now_dt = datetime(2026, 9, 29, 10, 0, tzinfo=timezone.utc)
        db.update_channel_scan_cursor("test_chan", now_dt, 150)

        scanned_at, scanned_id = db.get_channel_scan_cursor("test_chan")
        self.assertEqual(scanned_at, "2026-09-29 10:00:00")
        self.assertEqual(scanned_id, 150)

    def test_channel_backlog_counting(self):
        posts = [
            {"id": 1, "text": "p1", "time": datetime(2026, 9, 29, 8, 0, tzinfo=timezone.utc)},
            {"id": 2, "text": "p2", "time": datetime(2026, 9, 29, 8, 10, tzinfo=timezone.utc)},
            {"id": 3, "text": "p3", "time": datetime(2026, 9, 29, 8, 20, tzinfo=timezone.utc)},
        ]
        db.upsert_channel_posts("my_chan", posts, "test")

        self.assertEqual(db.get_channel_backlog_count("my_chan", last_post_id=0), 3)
        self.assertEqual(db.get_channel_backlog_count("my_chan", last_post_id=2), 1)
        self.assertEqual(db.get_channel_backlog_count("my_chan", last_post_id=3), 0)

    def test_channel_backpressure_overload_detection(self):
        # Create subscription
        db.add_subscription(
            user_id=1,
            channel_username="busy_chan",
            channel_title="Busy Channel",
            period="daily",
            last_scraped_at="2026-09-29 07:00:00",
            next_send_at="2026-09-30 07:00:00",
        )

        posts = [
            {"id": i, "text": f"p{i}", "time": datetime(2026, 9, 29, 8, 0, tzinfo=timezone.utc)}
            for i in range(1, 15)
        ]
        db.upsert_channel_posts("busy_chan", posts, "test")

        # Threshold 10: 14 posts > 10 -> overloaded
        self.assertTrue(db.is_channel_backlog_overloaded("busy_chan", threshold=10))
        # Threshold 20: 14 posts < 20 -> not overloaded
        self.assertFalse(db.is_channel_backlog_overloaded("busy_chan", threshold=20))


class SubscriptionBacklogDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = db.DB_PATH
        db.DB_PATH = os.path.join(self.temp_dir.name, "test_sub_backlog.db")
        db.init_db()

    def tearDown(self):
        db.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    async def test_subscription_reads_batch_and_leaves_backlog(self):
        import asyncio
        from main import fetch_subscription_posts

        sub_id = db.add_subscription(
            user_id=42,
            channel_username="batch_chan",
            channel_title="Batch Chan",
            period="daily",
            last_scraped_at="2026-09-29 07:00:00",
            next_send_at="2026-09-30 07:00:00",
        )

        # Insert 60 posts into channel_posts
        posts = [
            {"id": i, "text": f"Post {i}", "time": datetime(2026, 9, 29, 8, i % 60, tzinfo=timezone.utc)}
            for i in range(1, 65)
        ]
        db.upsert_channel_posts("batch_chan", posts, "test")

        sub_tuple = (sub_id, 42, "batch_chan", "Batch Chan", "daily", "2026-09-29 07:00:00", 0, None)
        sem = asyncio.Semaphore(5)

        class DummySession:
            pass

        prefetched = {
            "batch_chan": {
                "posts": posts,
                "source": "test",
                "error": None,
            }
        }

        # Original default CHANNEL_DELIVERY_BATCH_SIZE is 50
        result = await fetch_subscription_posts(
            sub_tuple,
            session=DummySession(),
            semaphore=sem,
            now_str="2026-09-29 09:00:00",
            prefetched_channels=prefetched,
        )

        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["has_backlog"])
        self.assertTrue(result["is_partial"])
        # Delivered post ID moved to 50
        self.assertEqual(result["last_post_id"], 50)

        # In next cycle, reading with last_post_id=50 should pick up remaining 14 posts
        sub_tuple_next = (sub_id, 42, "batch_chan", "Batch Chan", "daily", result["last_scraped_str"], 0, 50)
        result_next = await fetch_subscription_posts(
            sub_tuple_next,
            session=DummySession(),
            semaphore=sem,
            now_str="2026-09-29 09:30:00",
            prefetched_channels=prefetched,
        )

        self.assertFalse(result_next["has_backlog"])
        self.assertFalse(result_next["is_partial"])
        self.assertEqual(result_next["last_post_id"], 64)


if __name__ == "__main__":
    unittest.main()
