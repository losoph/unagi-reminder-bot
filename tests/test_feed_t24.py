import asyncio
from datetime import datetime, timedelta, timezone
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("BOT_TOKEN", "123456:TEST_TOKEN_FOR_UNIT_TESTS")

from data import database  # noqa: E402
import feed  # noqa: E402
import ranking  # noqa: E402


class FeedT24Tests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = os.path.join(self.temp_dir.name, "test_feed_t24.db")
        database.init_db()

        feed._personal_channels_cache.clear()
        feed._backfill_tasks.clear()
        feed._user_build_locks.clear()
        feed._building_users.clear()
        feed._last_build_time.clear()

    async def asyncTearDown(self):
        await feed.cancel_backfill_tasks()
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()
        feed._personal_channels_cache.clear()
        feed._backfill_tasks.clear()
        feed._user_build_locks.clear()
        feed._building_users.clear()
        feed._last_build_time.clear()

    def _make_callback(self, user_id: int, data: str) -> MagicMock:
        cb = MagicMock()
        cb.from_user.id = user_id
        cb.message = MagicMock()
        cb.message.chat.id = user_id
        cb.message.edit_text = AsyncMock()
        cb.message.answer = AsyncMock()
        cb.answer = AsyncMock()
        cb.data = data
        return cb

    def test_compute_subs_hash_determinism_and_invalidation(self):
        """subs_hash changes when tag, pause state, or channel set changes."""
        subs1 = [
            {"channel_username": "chan_b", "tag": "tech", "is_paused": 0},
            {"channel_username": "chan_a", "tag": "news", "is_paused": 0},
        ]
        # Order independence
        subs2 = [
            {"channel_username": "chan_a", "tag": "news", "is_paused": 0},
            {"channel_username": "chan_b", "tag": "tech", "is_paused": 0},
        ]
        self.assertEqual(feed.compute_subs_hash(subs1), feed.compute_subs_hash(subs2))

        # Tag changed
        subs_tag_changed = [
            {"channel_username": "chan_a", "tag": "crypto", "is_paused": 0},
            {"channel_username": "chan_b", "tag": "tech", "is_paused": 0},
        ]
        self.assertNotEqual(feed.compute_subs_hash(subs1), feed.compute_subs_hash(subs_tag_changed))

        # Pause changed
        subs_paused = [
            {"channel_username": "chan_a", "tag": "news", "is_paused": 1},
            {"channel_username": "chan_b", "tag": "tech", "is_paused": 0},
        ]
        self.assertNotEqual(feed.compute_subs_hash(subs1), feed.compute_subs_hash(subs_paused))

        # Channel added
        subs_added = list(subs1) + [{"channel_username": "chan_c", "tag": None, "is_paused": 0}]
        self.assertNotEqual(feed.compute_subs_hash(subs1), feed.compute_subs_hash(subs_added))

    def test_get_channel_medians_min_age_hours(self):
        """get_channel_medians with min_age_hours=24 excludes immature posts younger than 24h."""
        now = datetime.now(timezone.utc)
        chan = "median_chan"

        # 1. Old posts (3 days ago, 2 days ago, 25 hours ago)
        old_posts = [
            {"id": 1, "time": now - timedelta(days=3), "text": "Old 1", "views": 1000, "forwards": 20, "replies": 5, "reactions_total": 50},
            {"id": 2, "time": now - timedelta(days=2), "text": "Old 2", "views": 1200, "forwards": 24, "replies": 6, "reactions_total": 60},
            {"id": 3, "time": now - timedelta(hours=25), "text": "Old 3", "views": 1100, "forwards": 22, "replies": 5, "reactions_total": 55},
        ]
        # 2. Young immature post (2 hours ago, views very low)
        young_posts = [
            {"id": 4, "time": now - timedelta(hours=2), "text": "Young 4", "views": 100, "forwards": 1, "replies": 0, "reactions_total": 2},
        ]

        database.upsert_channel_posts(chan, old_posts + young_posts, "mtproto")

        # Without min_age_hours (default 0) -> includes all 4 posts, median views is (1000+1100)/2 = 1050
        all_med = ranking.get_channel_medians(chan, min_age_hours=0, now=now)
        self.assertEqual(all_med["post_count"], 4)
        self.assertEqual(all_med["median_views"], 1050.0)

        # With min_age_hours=24 -> excludes post 4, median views of [1000, 1100, 1200] is 1100
        mature_med = ranking.get_channel_medians(chan, min_age_hours=24, now=now)
        self.assertEqual(mature_med["post_count"], 3)
        self.assertEqual(mature_med["median_views"], 1100.0)

    async def test_build_feed_edition_empty_channels(self):
        """User with no feed channels receives empty_channels status."""
        user_id = 1001
        res = await feed.build_feed_edition(user_id, "week")
        self.assertEqual(res.status, "empty_channels")

        # UI handler renders prompt
        cb = self._make_callback(user_id, "feed_period_week")
        await feed.cb_feed_period(cb)
        cb.message.edit_text.assert_called_once()
        text = cb.message.edit_text.call_args[0][0]
        self.assertIn("У тебя пока нет каналов в Ленте", text)

    async def test_build_feed_edition_all_paused(self):
        """User with all channels paused receives all_paused status."""
        user_id = 1002
        sub_id = database.add_feed_subscription(user_id, "chan1", "Chan 1")
        database.set_feed_paused(user_id, sub_id, True)

        res = await feed.build_feed_edition(user_id, "week")
        self.assertEqual(res.status, "all_paused")

        cb = self._make_callback(user_id, "feed_period_week")
        await feed.cb_feed_period(cb)
        cb.message.edit_text.assert_called_once()
        text = cb.message.edit_text.call_args[0][0]
        self.assertIn("Все твои каналы в Ленте на паузе", text)

    async def test_build_feed_edition_empty_posts(self):
        """User has channel but no posts exist for the selected window."""
        user_id = 1003
        database.add_feed_subscription(user_id, "empty_chan", "Empty Chan")

        mock_source = MagicMock()
        mock_source.fetch = AsyncMock(return_value=([], "mtproto"))
        feed.configure(channel_source=mock_source)

        cb = self._make_callback(user_id, "feed_period_week")
        await feed.cb_feed_period(cb)

        cb.message.edit_text.assert_called()
        last_text = cb.message.edit_text.call_args[0][0]
        self.assertIn("За неделю в каналах Ленты постов не было.", last_text)

    async def test_build_feed_edition_cache_hit_and_miss_with_tag_invalidation(self):
        """Orchestration test: cache miss -> builds & publishes -> cache hit -> tag change -> cache miss."""
        user_id = 2001
        now = datetime.now(timezone.utc)
        sub_id = database.add_feed_subscription(user_id, "tech_chan", "Tech Channel", tag="tech")

        # Seed channel posts in DB within 7 days
        posts = [
            {
                "id": 101,
                "time": now - timedelta(days=2),
                "text": "Отличная новость технологий для Ленты.",
                "link": "https://t.me/tech_chan/101",
                "views": 5000,
                "forwards": 100,
                "reactions_total": 200,
                "replies": 10,
            }
        ]
        database.upsert_channel_posts("tech_chan", posts, "mtproto")

        mock_source = MagicMock()
        mock_source.fetch = AsyncMock(return_value=([], "mtproto"))
        mock_source.refresh_metrics = AsyncMock(return_value=0)
        feed.configure(channel_source=mock_source)

        published_url = "https://telegra.ph/Lenta-za-nedelyu-test"
        with patch("feed.publish_page", new_callable=AsyncMock) as mock_pub, \
             patch("feed.logger.info") as mock_log_info:
            mock_pub.return_value = published_url

            # 1. First run: CACHE MISS
            res1 = await feed.build_feed_edition(user_id, "week", now=now)
            self.assertEqual(res1.status, "ok")
            self.assertFalse(res1.cached)
            self.assertEqual(res1.telegraph_url, published_url)
            self.assertEqual(res1.post_count, 1)
            self.assertEqual(res1.channel_count, 1)
            mock_pub.assert_called_once()

            # Verify log single line with cache=miss
            miss_logs = [call[0][0] % call[0][1:] for call in mock_log_info.call_args_list if "feed_build" in str(call[0][0])]
            self.assertTrue(any("cache=miss" in l for l in miss_logs))

        # 2. Second run: CACHE HIT (same period, same subs)
        with patch("feed.publish_page", new_callable=AsyncMock) as mock_pub, \
             patch("feed.logger.info") as mock_log_info:
            res2 = await feed.build_feed_edition(user_id, "week", now=now)
            self.assertEqual(res2.status, "ok")
            self.assertTrue(res2.cached)
            self.assertEqual(res2.telegraph_url, published_url)
            # publish_page MUST NOT be called again
            mock_pub.assert_not_called()

            hit_logs = [call[0][0] % call[0][1:] for call in mock_log_info.call_args_list if "feed_build" in str(call[0][0])]
            self.assertTrue(any("cache=hit" in l for l in hit_logs))

        # 3. Change tag: invalidates cache!
        database.set_feed_tag(user_id, sub_id, "crypto")

        with patch("feed.publish_page", new_callable=AsyncMock) as mock_pub, \
             patch("feed.logger.info") as mock_log_info:
            mock_pub.return_value = "https://telegra.ph/Lenta-new-tag"
            # ignore_rate_limit to test cache invalidation cleanly
            res3 = await feed.build_feed_edition(user_id, "week", ignore_rate_limit=True, now=now)
            self.assertEqual(res3.status, "ok")
            self.assertFalse(res3.cached)
            self.assertEqual(res3.telegraph_url, "https://telegra.ph/Lenta-new-tag")
            mock_pub.assert_called_once()

    async def test_rate_limiting_on_cache_miss(self):
        """Repeated build request on cache miss within 60s triggers rate limit."""
        user_id = 3001
        database.add_feed_subscription(user_id, "chan_rt", "Chan RT")

        mock_source = MagicMock()
        mock_source.fetch = AsyncMock(return_value=([], "mtproto"))
        feed.configure(channel_source=mock_source)

        # Simulate previous build completed recently
        feed._last_build_time[user_id] = feed.time.time()

        # Second call immediately after:
        cb2 = self._make_callback(user_id, "feed_period_day")
        await feed.cb_feed_period(cb2)

        cb2.answer.assert_called_with("Подожди минуту", show_alert=True)

    async def test_timeout_handling(self):
        """Build exceeding FEED_BUILD_TIMEOUT_SECONDS returns polite error and does not publish."""
        user_id = 4001
        database.add_feed_subscription(user_id, "timeout_chan", "Timeout Chan")

        # Mock ingest_window to hang
        async def slow_ingest(*args, **kwargs):
            await asyncio.sleep(5)

        with patch("feed.ingest_window", side_effect=slow_ingest), \
             patch("feed.publish_page", new_callable=AsyncMock) as mock_pub, \
             patch("feed.FEED_BUILD_TIMEOUT_SECONDS", 0.05):
            res = await feed.build_feed_edition(user_id, "day", ignore_rate_limit=True)
            self.assertEqual(res.status, "timeout")
            self.assertIn("Сборка заняла слишком много времени", res.error_message)
            mock_pub.assert_not_called()

    async def test_user_timezone_passed_to_render_and_title(self):
        """User's configured timezone in user_prefs is passed to render_feed and build_feed_title."""
        user_id = 5001
        now = datetime.now(timezone.utc)
        database.add_feed_subscription(user_id, "tz_chan", "TZ Channel")
        database.upsert_user_prefs(user_id, timezone="Asia/Vladivostok")  # UTC+10

        posts = [
            {
                "id": 1,
                "time": now - timedelta(hours=2),
                "text": "Владивостокский пост",
                "link": "https://t.me/tz_chan/1",
                "views": 100,
            }
        ]
        database.upsert_channel_posts("tz_chan", posts, "mtproto")

        mock_source = MagicMock()
        mock_source.fetch = AsyncMock(return_value=([], "mtproto"))
        mock_source.refresh_metrics = AsyncMock(return_value=0)
        feed.configure(channel_source=mock_source)

        with patch("feed.build_feed_title", wraps=feed.build_feed_title) as mock_title, \
             patch("feed.render_feed", wraps=feed.render_feed) as mock_render, \
             patch("feed.publish_page", new_callable=AsyncMock) as mock_pub:
            mock_pub.return_value = "https://telegra.ph/tz-test"

            res = await feed.build_feed_edition(user_id, "day", now=now)
            self.assertEqual(res.status, "ok")

            # Check tz passed to build_feed_title
            mock_title.assert_called_once()
            _, kwargs_title = mock_title.call_args
            tz_title = kwargs_title.get("tz")
            self.assertIsNotNone(tz_title)
            # Must correspond to Asia/Vladivostok (+10)
            self.assertEqual(tz_title.utcoffset(now), timedelta(hours=10))

            # Check tz passed to render_feed
            mock_render.assert_called_once()
            _, kwargs_render = mock_render.call_args
            tz_render = kwargs_render.get("tz")
            self.assertEqual(tz_render.utcoffset(now), timedelta(hours=10))


if __name__ == "__main__":
    unittest.main()
