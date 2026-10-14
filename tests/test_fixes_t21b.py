import asyncio
from datetime import datetime, timedelta, timezone
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("BOT_TOKEN", "123456:TEST_TOKEN_FOR_UNIT_TESTS")

from channel_source import HybridChannelSource
from data import database
import feed
import main


class FixesT21bTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = os.path.join(self.temp_dir.name, "test_fixes_t21b.db")
        database.init_db()
        self.now = datetime(2026, 10, 14, 12, 0, 0, tzinfo=timezone.utc)

    def tearDown(self):
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    # -------------------------------------------------------------------------
    # 1. Existing feed channel during import: not overwritten, reported
    # -------------------------------------------------------------------------
    def test_item1_import_existing_feed_subscription_not_overwritten(self):
        user_id = 101
        sub_id = database.add_feed_subscription(user_id, "tech_chan", "Tech Title", tag="tech")
        database.set_feed_paused(user_id, sub_id, True)

        # Calling add_feed_subscription on existing channel returns same ID and does not overwrite tag
        same_id = database.add_feed_subscription(user_id, "tech_chan", "New Tech Title", tag="new_tag")
        self.assertEqual(sub_id, same_id)
        existing_sub = database.get_feed_subscription(user_id, sub_id)
        self.assertEqual(existing_sub["channel_title"], "Tech Title")
        self.assertEqual(existing_sub["tag"], "tech")
        self.assertEqual(existing_sub["is_paused"], 1)

        payload = {
            "version": 2,
            "feed": [
                {"channel_username": "tech_chan", "channel_title": "Import Title", "tag": "import_tag", "is_paused": False},
                {"channel_username": "science_chan", "channel_title": "Science Title", "tag": "science", "is_paused": False},
            ],
        }

        report = main._import_user_payload(user_id, payload)
        self.assertIn("• Лента: +1 (уже были в Ленте: 1)", report)

        sub_tech = database.get_feed_subscription_by_username(user_id, "tech_chan")
        self.assertEqual(sub_tech["tag"], "tech")
        self.assertEqual(sub_tech["is_paused"], 1)

        sub_science = database.get_feed_subscription_by_username(user_id, "science_chan")
        self.assertIsNotNone(sub_science)
        self.assertEqual(sub_science["tag"], "science")

    # -------------------------------------------------------------------------
    # 2. Cleanup feed_editions older than 2 days
    # -------------------------------------------------------------------------
    def test_item2_cleanup_old_records_deletes_old_feed_editions(self):
        user_id = 201
        now_str = database.serialize_datetime(self.now)

        # Insert edition 3 days old
        old_created = database.serialize_datetime(self.now - timedelta(days=3))
        # Insert edition 1 day old
        new_created = database.serialize_datetime(self.now - timedelta(days=1))

        with database.get_connection() as conn:
            conn.execute(
                """
                INSERT INTO feed_editions (user_id, period, window_start, window_end, subs_hash, telegraph_url, post_count, channel_count, created_at)
                VALUES (?, 'day', ?, ?, 'hash_old', 'https://telegra.ph/old', 5, 1, ?)
                """,
                (user_id, old_created, old_created, old_created),
            )
            conn.execute(
                """
                INSERT INTO feed_editions (user_id, period, window_start, window_end, subs_hash, telegraph_url, post_count, channel_count, created_at)
                VALUES (?, 'day', ?, ?, 'hash_new', 'https://telegra.ph/new', 5, 1, ?)
                """,
                (user_id, new_created, new_created, new_created),
            )
            conn.commit()

        stats = database.cleanup_old_records(now_str)
        self.assertEqual(stats.get("feed_editions_deleted"), 1)

        with database.get_connection() as conn:
            remaining = conn.execute("SELECT subs_hash FROM feed_editions WHERE user_id = ?", (user_id,)).fetchall()
            hashes = [r["subs_hash"] for r in remaining]
            self.assertEqual(hashes, ["hash_new"])

    # -------------------------------------------------------------------------
    # 3. Only timezone-aware datetimes accepted (ValueError on naive or invalid)
    # -------------------------------------------------------------------------
    def test_item3_window_bounds_datetime_normalization(self):
        user_id = 301
        valid_aware = datetime(2026, 10, 14, 12, 0, 0, tzinfo=timezone.utc)
        valid_iso_str = "2026-10-14T15:00:00+03:00"  # Equivalent to 12:00:00 UTC
        valid_db_str = "2026-10-14 12:00:00"
        naive_dt = datetime(2026, 10, 14, 12, 0, 0)
        invalid_str = "invalid-date-string"

        # normalize_feed_datetime unit checks
        self.assertEqual(database.normalize_feed_datetime(valid_aware), "2026-10-14 12:00:00")
        self.assertEqual(database.normalize_feed_datetime(valid_iso_str), "2026-10-14 12:00:00")
        self.assertEqual(database.normalize_feed_datetime(valid_db_str), "2026-10-14 12:00:00")

        with self.assertRaises(ValueError):
            database.normalize_feed_datetime(naive_dt)

        with self.assertRaises(ValueError):
            database.normalize_feed_datetime(invalid_str)

        # get_feed_candidate_posts rejects naive datetime or invalid str
        with self.assertRaises(ValueError):
            database.get_feed_candidate_posts(user_id, naive_dt, valid_aware)

        with self.assertRaises(ValueError):
            database.get_feed_candidate_posts(user_id, valid_aware, invalid_str)

        # save_feed_edition rejects naive datetime or invalid str
        with self.assertRaises(ValueError):
            database.save_feed_edition(user_id, "day", naive_dt, valid_aware, "h", "http://u", 1, 1)

        with self.assertRaises(ValueError):
            database.save_feed_edition(user_id, "day", valid_aware, invalid_str, "h", "http://u", 1, 1)

    # -------------------------------------------------------------------------
    # 4. Atomic MAX_FEED_CHANNELS check in BEGIN IMMEDIATE
    # -------------------------------------------------------------------------
    def test_item4_atomic_feed_channel_limit_check(self):
        user_id = 401
        with patch.object(database, "MAX_FEED_CHANNELS", 2):
            database.add_feed_subscription(user_id, "chan1")
            database.add_feed_subscription(user_id, "chan2")

            with self.assertRaises(ValueError) as ctx:
                database.add_feed_subscription(user_id, "chan3")
            self.assertIn("Превышен лимит каналов Ленты", str(ctx.exception))

            # Existing channel can still be queried/called idempotently without error
            same_id = database.add_feed_subscription(user_id, "chan1")
            self.assertIsNotNone(same_id)

    # -------------------------------------------------------------------------
    # 5. normalize_feed_tag strips dangling emoji modifiers / joiners
    # -------------------------------------------------------------------------
    def test_item5_normalize_feed_tag_strips_dangling_emoji_modifiers(self):
        # 23 letters + 1 emoji with skin tone -> truncation cuts modifier in half or leaves modifier at end
        base_prefix = "a" * 23
        tag_with_tone = base_prefix + "👍🏿"
        normalized = database.normalize_feed_tag(tag_with_tone)
        self.assertLessEqual(len(normalized), 24)
        for char in {"\u200d", "\ufe0f", "\U0001f3fb", "\U0001f3fc", "\U0001f3fd", "\U0001f3fe", "\U0001f3ff"}:
            self.assertFalse(normalized.endswith(char), f"Tag should not end with dangling char {char!r}")

        # Explicit test of trailing modifier chars
        for dangler in ("\u200d", "\ufe0f", "\U0001f3fb", "\U0001f3fc", "\U0001f3fd", "\U0001f3fe", "\U0001f3ff"):
            tag_dangling = ("tag" + dangler)[:24]
            cleaned = database.normalize_feed_tag(tag_dangling)
            self.assertEqual(cleaned, "tag")

    # -------------------------------------------------------------------------
    # 6. Duplicate index removed
    # -------------------------------------------------------------------------
    def test_item6_no_duplicate_index_definition(self):
        with database.get_connection() as conn:
            indexes = conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND name = 'idx_channel_posts_channel_time'"
            ).fetchall()
            self.assertEqual(len(indexes), 1)

    # -------------------------------------------------------------------------
    # 7. feed.py process_feed_tag_text uses message.from_user.id
    # -------------------------------------------------------------------------
    async def test_item7_process_feed_tag_text_uses_from_user_id(self):
        user_id = 701
        other_chat_id = 999999
        sub_id = database.add_feed_subscription(user_id, "chan7", "Title")

        state = MagicMock()
        state_data = {"tag_sub_id": sub_id, "page": 0}
        state.get_data = AsyncMock(return_value=state_data)
        state.clear = AsyncMock()

        msg = MagicMock()
        msg.from_user = MagicMock()
        msg.from_user.id = user_id
        msg.chat.id = other_chat_id
        msg.text = "tech"
        msg.answer = AsyncMock()

        await feed.process_feed_tag_text(msg, state)

        sub = database.get_feed_subscription(user_id, sub_id)
        self.assertEqual(sub["tag"], "tech")

    # -------------------------------------------------------------------------
    # 8. Deleting channel returns to current or previous page
    # -------------------------------------------------------------------------
    async def test_item8_delete_returns_to_current_or_prev_page(self):
        user_id = 801
        # Add 9 channels (page 0 has 8 channels: 0..7, page 1 has 1 channel: 8)
        sub_ids = []
        for i in range(9):
            s_id = database.add_feed_subscription(user_id, f"channel_{i:02d}")
            sub_ids.append(s_id)

        # Deleting 9th channel (from page 1): remaining count = 8, so page 1 is now empty -> return to page 0
        state = AsyncMock()
        cb = MagicMock()
        cb.from_user.id = user_id
        cb.data = f"feed_del:{sub_ids[-1]}:1"
        cb.answer = AsyncMock()
        cb.message = MagicMock()
        cb.message.edit_text = AsyncMock()

        await feed.cb_feed_delete(cb, state)
        self.assertEqual(cb.data, "feed_channels_0")

        # Now add 2 more channels to have 10 channels total (page 0: 8, page 1: 2)
        s9 = database.add_feed_subscription(user_id, "channel_09")
        database.add_feed_subscription(user_id, "channel_10")

        # Deleting s9 from page 1: remaining count = 9, so page 1 still has channel_10 -> stays on page 1
        cb.data = f"feed_del:{s9}:1"
        await feed.cb_feed_delete(cb, state)
        self.assertEqual(cb.data, "feed_channels_1")

    # -------------------------------------------------------------------------
    # 9. feed_backfilled_at set only if source == "mtproto"
    # -------------------------------------------------------------------------
    async def test_item9_feed_backfilled_at_only_set_for_mtproto(self):
        chan_web = "chan_web_source"
        chan_mt = "chan_mt_source"
        source = HybridChannelSource()

        # Web source returns posts
        async def fake_fetch_web(username, marker_str, **kwargs):
            return [{"id": 1, "text": "Post 1", "time": self.now, "link": "http://t.me/1"}], "web"

        source.fetch = fake_fetch_web
        await feed.ingest_window([chan_web], window_start=self.now - timedelta(days=30), now=self.now, channel_source=source)
        self.assertIsNone(database.get_channel_feed_backfilled_at(chan_web))

        # MTProto source returns posts
        async def fake_fetch_mtproto(username, marker_str, **kwargs):
            return [{"id": 2, "text": "Post 2", "time": self.now, "link": "http://t.me/2"}], "mtproto"

        source.fetch = fake_fetch_mtproto
        await feed.ingest_window([chan_mt], window_start=self.now - timedelta(days=30), now=self.now, channel_source=source)
        self.assertIsNotNone(database.get_channel_feed_backfilled_at(chan_mt))

    # -------------------------------------------------------------------------
    # 10. Shared semaphore between main and feed
    # -------------------------------------------------------------------------
    def test_item10_shared_semaphore(self):
        source = HybridChannelSource()
        sem = source.fetch_semaphore
        self.assertIsInstance(sem, asyncio.Semaphore)

        feed.configure(channel_source=source)
        feed_sem = feed.get_fetch_semaphore()
        self.assertIs(feed_sem, sem)

    # -------------------------------------------------------------------------
    # 11. update_channel_scan_cursor strictly monotonic
    # -------------------------------------------------------------------------
    def test_item11_update_channel_scan_cursor_monotonicity(self):
        channel = "mono_channel"
        t1 = self.now
        t2 = self.now - timedelta(hours=2)
        t3 = self.now + timedelta(hours=1)

        # Initial set
        database.update_channel_scan_cursor(channel, t1, 100)
        cur_str, cur_id = database.get_channel_scan_cursor(channel)
        self.assertEqual(database.parse_db_datetime(cur_str), t1)
        self.assertEqual(cur_id, 100)

        # Attempt to set to older timestamp -> MUST NOT move backward
        database.update_channel_scan_cursor(channel, t2, 50)
        cur_str, cur_id = database.get_channel_scan_cursor(channel)
        self.assertEqual(database.parse_db_datetime(cur_str), t1)
        self.assertEqual(cur_id, 100)

        # Update with newer timestamp -> MUST advance
        database.update_channel_scan_cursor(channel, t3, 150)
        cur_str, cur_id = database.get_channel_scan_cursor(channel)
        self.assertEqual(database.parse_db_datetime(cur_str), t3)
        self.assertEqual(cur_id, 150)

    # -------------------------------------------------------------------------
    # 12. refresh_metrics aborts on views length mismatch
    # -------------------------------------------------------------------------
    async def test_item12_refresh_metrics_views_length_mismatch_aborts(self):
        source = HybridChannelSource()
        channel = "mismatch_chan"

        # Populate a channel post
        database.upsert_channel_posts(
            channel,
            [
                {"id": 1, "text": "Post 1", "time": self.now, "link": "http://t.me/1"},
                {"id": 2, "text": "Post 2", "time": self.now, "link": "http://t.me/2"},
            ],
            "mtproto",
        )

        mock_client = AsyncMock()
        # Return 1 view object for 2 requested post IDs (length mismatch)
        mock_views_result = MagicMock()
        mock_views_result.views = [{"views": 100, "forwards": 10, "replies": 2}]
        mock_client.return_value = mock_views_result

        source._client = mock_client

        with self.assertLogs("channel_source", level="WARNING") as log_cm:
            updated = await source.refresh_metrics(channel, [1, 2], stale_threshold_seconds=0)

        self.assertEqual(updated, 0)
        self.assertTrue(any("Length mismatch in GetMessagesViewsRequest" in msg for msg in log_cm.output))

    # -------------------------------------------------------------------------
    # 13. cancel_backfill_tasks cancels and awaits background tasks on shutdown
    # -------------------------------------------------------------------------
    async def test_item13_cancel_backfill_tasks(self):
        cancelled = False

        async def dummy_coro():
            nonlocal cancelled
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                cancelled = True
                raise

        loop = asyncio.get_running_loop()
        task = loop.create_task(dummy_coro())
        feed._backfill_tasks["test_chan"] = task
        await asyncio.sleep(0)

        await feed.cancel_backfill_tasks()

        self.assertTrue(cancelled)
        self.assertTrue(task.done())
        self.assertEqual(len(feed._backfill_tasks), 0)


if __name__ == "__main__":
    unittest.main()
