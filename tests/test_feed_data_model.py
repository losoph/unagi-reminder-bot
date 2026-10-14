import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

os.environ.setdefault("BOT_TOKEN", "123456:TEST_TOKEN_FOR_UNIT_TESTS")

from data import database  # noqa: E402
import main  # noqa: E402


class FeedDataModelTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = os.path.join(self.temp_dir.name, "test_feed.db")
        database.init_db()

    def tearDown(self):
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def test_migration_on_old_schema(self):
        """Migration creates feed tables and adds missing columns if older schema existed."""
        old_db_path = os.path.join(self.temp_dir.name, "test_old_schema.db")
        # Create older database with minimal tables and feed_subscriptions without tag or is_paused
        conn = sqlite3.connect(old_db_path)
        conn.execute(
            """
            CREATE TABLE feed_subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                channel_username TEXT NOT NULL,
                channel_title TEXT,
                created_at DATETIME NOT NULL,
                UNIQUE(user_id, channel_username)
            )
            """
        )
        conn.commit()
        conn.close()

        with patch.object(database, "DB_PATH", old_db_path):
            database.init_db()
            # Verify columns exist and table works
            sub_id = database.add_feed_subscription(101, "tech_news", "Tech", tag="news")
            self.assertIsNotNone(sub_id)
            sub = database.get_feed_subscription(101, sub_id)
            self.assertIsNotNone(sub)
            self.assertEqual(sub["tag"], "news")
            self.assertEqual(sub["is_paused"], 0)

    def test_feed_unique_constraint_and_idempotency(self):
        """Adding the same channel for a user is idempotent and respects UNIQUE(user_id, channel_username)."""
        sub_id_1 = database.add_feed_subscription(201, "@TechChannel", "Old Title", tag="tech")
        sub_id_2 = database.add_feed_subscription(201, "techchannel", "New Title", tag="it")

        self.assertEqual(sub_id_1, sub_id_2)
        subs = database.list_feed_subscriptions(201)
        self.assertEqual(len(subs), 1)
        self.assertEqual(subs[0]["channel_title"], "Old Title")
        self.assertEqual(subs[0]["tag"], "tech")
        self.assertEqual(subs[0]["channel_username"], "techchannel")

    def test_feed_limit_max_channels(self):
        """add_feed_subscription enforces MAX_FEED_CHANNELS limit."""
        with patch.object(database, "MAX_FEED_CHANNELS", 2):
            database.add_feed_subscription(301, "ch1", "Ch 1")
            database.add_feed_subscription(301, "ch2", "Ch 2")

            # Updating existing channel when at limit succeeds
            database.add_feed_subscription(301, "ch1", "Ch 1 updated")

            # Adding 3rd channel raises ValueError
            with self.assertRaises(ValueError) as ctx:
                database.add_feed_subscription(301, "ch3", "Ch 3")
            self.assertIn("Превышен лимит каналов Ленты", str(ctx.exception))

    def test_tag_normalization(self):
        """Tags are normalized: whitespace trimmed, <= 24 chars, no newlines, leading # stripped."""
        self.assertEqual(database.normalize_feed_tag("  #technology  "), "technology")
        self.assertEqual(database.normalize_feed_tag("a" * 30), "a" * 24)
        self.assertEqual(database.normalize_feed_tag("tag\nwith\r\nnewlines"), "tag with  newlines"[:24])
        self.assertIsNone(database.normalize_feed_tag("   "))
        self.assertIsNone(database.normalize_feed_tag("#"))
        self.assertIsNone(database.normalize_feed_tag(None))

    def test_set_tag_and_paused_and_delete(self):
        """Setting tag, pausing, and deleting feed subscriptions works as expected."""
        sub_id = database.add_feed_subscription(401, "channel_a", "Channel A")
        self.assertIsNone(database.get_feed_subscription(401, sub_id)["tag"])

        # Set tag
        self.assertTrue(database.set_feed_tag(401, sub_id, "#dev"))
        self.assertEqual(database.get_feed_subscription(401, sub_id)["tag"], "dev")

        # Pause
        self.assertEqual(database.count_feed_subscriptions(401, include_paused=False), 1)
        self.assertTrue(database.set_feed_paused(401, sub_id, True))
        self.assertEqual(database.get_feed_subscription(401, sub_id)["is_paused"], 1)
        self.assertEqual(database.count_feed_subscriptions(401, include_paused=False), 0)
        self.assertEqual(database.count_feed_subscriptions(401, include_paused=True), 1)

        # Unpause
        self.assertTrue(database.set_feed_paused(401, sub_id, False))
        self.assertEqual(database.count_feed_subscriptions(401, include_paused=False), 1)

        # List tags
        database.add_feed_subscription(401, "channel_b", "Channel B", tag="ai")
        tags = database.list_feed_tags(401)
        self.assertEqual(tags, ["ai", "dev"])

        # Delete
        self.assertTrue(database.delete_feed_subscription(401, sub_id))
        self.assertEqual(database.count_feed_subscriptions(401), 1)

    def test_get_feed_candidate_posts(self):
        """get_feed_candidate_posts joins posts and subscriptions, respecting window, text, and pause."""
        user_id = 501
        now = datetime(2026, 10, 14, 12, 0, 0, tzinfo=timezone.utc)
        w_start = now - timedelta(days=1)
        w_end = now

        database.add_feed_subscription(user_id, "active_ch", "Active Channel", tag="science")
        paused_id = database.add_feed_subscription(user_id, "paused_ch", "Paused Channel")
        database.set_feed_paused(user_id, paused_id, True)

        # Insert posts
        posts = [
            # In window, active channel, with text -> SHOULD be included
            {
                "id": 1,
                "text": "Valid post in window",
                "time": now - timedelta(hours=5),
                "views": 1000,
                "forwards": 50,
            },
            # Before window -> excluded
            {
                "id": 2,
                "text": "Old post",
                "time": now - timedelta(hours=30),
                "views": 500,
            },
            # After window_end -> excluded
            {
                "id": 3,
                "text": "Future post",
                "time": now + timedelta(hours=1),
                "views": 200,
            },
            # Empty text -> excluded
            {
                "id": 4,
                "text": "   ",
                "time": now - timedelta(hours=2),
                "views": 300,
            },
        ]
        database.upsert_channel_posts("active_ch", posts, source="mtproto")

        # Post in paused channel -> excluded
        database.upsert_channel_posts(
            "paused_ch",
            [{"id": 10, "text": "Post in paused channel", "time": now - timedelta(hours=3), "views": 400}],
            source="mtproto",
        )

        candidates = database.get_feed_candidate_posts(user_id, w_start, w_end)
        self.assertEqual(len(candidates), 1)
        cand = candidates[0]
        self.assertEqual(cand["id"], 1)
        self.assertEqual(cand["channel_username"], "active_ch")
        self.assertEqual(cand["channel_title"], "Active Channel")
        self.assertEqual(cand["tag"], "science")
        self.assertEqual(cand["text"], "Valid post in window")
        self.assertEqual(cand["views"], 1000)
        self.assertEqual(cand["forwards"], 50)

    def test_feed_edition_cache(self):
        """save_feed_edition stores cache and get_feed_edition retrieves it until expiration."""
        user_id = 601
        now = datetime(2026, 10, 14, 12, 0, 0, tzinfo=timezone.utc)
        w_start = now - timedelta(days=7)
        subs_hash = "abc123hash"

        ed_id = database.save_feed_edition(
            user_id=user_id,
            period="week",
            window_start=w_start,
            window_end=now,
            subs_hash=subs_hash,
            telegraph_url="https://telegra.ph/digest-123",
            post_count=25,
            channel_count=5,
        )
        self.assertIsNotNone(ed_id)

        # Cache hit
        cached = database.get_feed_edition(user_id, "week", subs_hash, max_age_seconds=900)
        self.assertIsNotNone(cached)
        self.assertEqual(cached["telegraph_url"], "https://telegra.ph/digest-123")
        self.assertEqual(cached["post_count"], 25)

        # Different hash -> miss
        self.assertIsNone(database.get_feed_edition(user_id, "week", "different_hash"))

        # Different period -> miss
        self.assertIsNone(database.get_feed_edition(user_id, "day", subs_hash))

        # Expired -> miss
        self.assertIsNone(database.get_feed_edition(user_id, "week", subs_hash, max_age_seconds=-1))

    def test_export_import_v2(self):
        """Export generates version 2 with 'feed' section and import restores them."""
        user_1 = 701
        user_2 = 702

        database.add_feed_subscription(user_1, "python_news", "Python News", tag="python")
        sub_id_2 = database.add_feed_subscription(user_1, "ai_trends", "AI Trends", tag="ai")
        database.set_feed_paused(user_1, sub_id_2, True)

        exported = database.export_user_data(user_1)
        self.assertIn("feed", exported)
        self.assertEqual(len(exported["feed"]), 2)

        # Build payload as main._send_export does
        payload = {
            "app": "unagi-reminder-bot",
            "version": main.EXPORT_VERSION,
            "exported_at": database.serialize_datetime(database.utc_now()),
            **exported,
        }
        self.assertEqual(payload["version"], 2)

        # Import into user_2
        report = main._import_user_payload(user_2, payload)
        self.assertIn("Лента: +2", report)

        subs_2 = database.list_feed_subscriptions(user_2)
        self.assertEqual(len(subs_2), 2)
        subs_by_name = {s["channel_username"]: s for s in subs_2}
        self.assertEqual(subs_by_name["python_news"]["tag"], "python")
        self.assertEqual(subs_by_name["python_news"]["is_paused"], 0)
        self.assertEqual(subs_by_name["ai_trends"]["tag"], "ai")
        self.assertEqual(subs_by_name["ai_trends"]["is_paused"], 1)

    def test_import_v1_without_feed(self):
        """Import of legacy v1 payload without 'feed' key succeeds."""
        user_id = 801
        legacy_payload = {
            "app": "unagi-reminder-bot",
            "version": 1,
            "reminders": [],
            "bookmarks": [],
            "subscriptions": [],
            "digest_settings": [],
        }
        report = main._import_user_payload(user_id, legacy_payload)
        self.assertIn("Импорт завершён", report)
        self.assertIn("Лента: +0", report)
        self.assertEqual(database.count_feed_subscriptions(user_id), 0)
