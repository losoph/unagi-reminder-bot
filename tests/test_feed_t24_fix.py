import asyncio
from datetime import datetime, timedelta, timezone
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("BOT_TOKEN", "123456:TEST_TOKEN_FOR_UNIT_TESTS")

from aiogram.exceptions import TelegramAPIError  # noqa: E402
from data import database  # noqa: E402
import feed  # noqa: E402
from feed_render import render_feed  # noqa: E402


class FeedT24FixTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = os.path.join(self.temp_dir.name, "feed_t24_fix.db")
        database.init_db()
        feed._user_build_locks.clear()
        feed._building_users.clear()
        feed._last_build_time.clear()

    async def asyncTearDown(self):
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()
        feed._user_build_locks.clear()
        feed._building_users.clear()
        feed._last_build_time.clear()

    async def test_ingest_reports_fetch_and_upsert_failures_but_not_cached_channels(self):
        now = datetime.now(timezone.utc)
        source = MagicMock()
        async def fetch(username, *_args, **_kwargs):
            if username == "fetch_failed":
                raise RuntimeError("fetch failed")
            return [], "mtproto"

        source.fetch = AsyncMock(side_effect=fetch)

        def failing_upsert(username, *_args):
            if username == "upsert_failed":
                raise RuntimeError("db failed")

        with patch.object(feed, "upsert_channel_posts", side_effect=failing_upsert):
            result = await feed.ingest_window(
                ["fetch_failed", "upsert_failed", "cached"], now - timedelta(days=1),
                now=now, channel_source=source,
            )

        self.assertEqual(result["failed_channels"], ["fetch_failed", "upsert_failed"])
        self.assertEqual(set(result["posts_by_channel"]), {"fetch_failed", "upsert_failed", "cached"})

    def test_incomplete_channel_notice_is_rendered_as_plain_text(self):
        nodes = render_feed(
            [], period="day", now=datetime.now(timezone.utc), incomplete_channels=["R&D", "Канал <2>"]
        )
        self.assertIn(
            "ℹ️ Часть каналов не загрузилась: R&D, Канал <2>", repr(nodes)
        )

    async def test_all_channels_failed_prevents_publish_and_cache(self):
        user_id = 73001
        subs = [
            {"channel_username": "one", "channel_title": "Первый", "is_paused": 0},
            {"channel_username": "two", "channel_title": "Второй", "is_paused": 0},
        ]
        with patch("feed.ingest_window", new_callable=AsyncMock, return_value={"failed_channels": ["one", "two"]}), \
             patch("feed.publish_page", new_callable=AsyncMock) as publish, \
             patch("feed.get_feed_edition", return_value=None), \
             patch("feed.save_feed_edition") as save:
            result = await feed.build_feed_edition(
                user_id, "day", subs=subs, active_subs=subs, ignore_rate_limit=True
            )

        self.assertEqual(result.status, "error")
        self.assertIn("ни из одного канала", result.error_message)
        self.assertEqual(result.error_channels, ["Первый", "Второй"])
        publish.assert_not_called()
        save.assert_not_called()

    async def test_missing_channel_source_counts_as_all_channels_failed(self):
        user_id = 73004
        subs = [{"channel_username": "offline", "channel_title": "Офлайн", "is_paused": 0}]
        with patch("feed._channel_source", None), patch("feed.get_feed_edition", return_value=None), \
             patch("feed.publish_page", new_callable=AsyncMock) as publish, \
             patch("feed.save_feed_edition") as save:
            result = await feed.build_feed_edition(
                user_id, "day", subs=subs, active_subs=subs, ignore_rate_limit=True
            )

        self.assertEqual(result.status, "error")
        publish.assert_not_called()
        save.assert_not_called()

    async def test_partial_failure_is_shown_and_not_cached(self):
        user_id = 73002
        now = datetime.now(timezone.utc)
        subs = [
            {"channel_username": "ok", "channel_title": "Хороший", "is_paused": 0},
            {"channel_username": "bad", "channel_title": "Сбойный", "is_paused": 0},
        ]
        candidate = {
            "post_id": 1, "channel_username": "ok", "channel_title": "Хороший", "tag": None,
            "text": "Тестовый пост", "link": "https://t.me/ok/1", "time": now - timedelta(hours=1),
            "views": 100, "forwards": 2, "reactions_total": 1, "replies": 0, "metrics_at": now,
        }
        with patch("feed.ingest_window", new_callable=AsyncMock, return_value={"failed_channels": ["bad"]}), \
             patch("feed.get_feed_edition", return_value=None), \
             patch("feed.get_feed_candidate_posts", return_value=[candidate]), \
             patch("feed.get_channel_medians", return_value={}), \
             patch("feed.user_tz", return_value=timezone.utc), \
             patch("feed.publish_page", new_callable=AsyncMock, return_value="https://telegra.ph/x"), \
             patch("feed.save_feed_edition") as save, \
             patch("feed.render_feed", wraps=feed.render_feed) as render:
            result = await feed.build_feed_edition(
                user_id, "day", subs=subs, active_subs=subs, ignore_rate_limit=True, now=now
            )

        self.assertEqual(result.status, "ok")
        self.assertEqual(render.call_args.kwargs["incomplete_channels"], ["Сбойный"])
        save.assert_called_once()

    async def test_publish_overall_timeout_does_not_lose_successful_page_or_cache(self):
        user_id = 73003
        now = datetime.now(timezone.utc)
        subs = [{"channel_username": "ok", "channel_title": "Хороший", "is_paused": 0}]
        candidate = {
            "post_id": 1, "channel_username": "ok", "channel_title": "Хороший", "tag": None,
            "text": "Тестовый пост", "link": "https://t.me/ok/1", "time": now - timedelta(hours=1),
            "views": 100, "forwards": 2, "reactions_total": 1, "replies": 0, "metrics_at": now,
        }

        async def slow_publish(*_args):
            await asyncio.sleep(0.06)
            return "https://telegra.ph/published"

        with patch("feed.ingest_window", new_callable=AsyncMock, return_value={"failed_channels": []}), \
             patch("feed.get_feed_edition", return_value=None), \
             patch("feed.get_feed_candidate_posts", return_value=[candidate]), \
             patch("feed.get_channel_medians", return_value={}), \
             patch("feed.user_tz", return_value=timezone.utc), \
             patch("feed.publish_page", side_effect=slow_publish), \
             patch("feed.save_feed_edition") as save, \
             patch("feed.FEED_BUILD_TIMEOUT_SECONDS", 0.03), \
             patch("feed.FEED_PUBLISH_TIMEOUT_SECONDS", 0.2):
            result = await feed.build_feed_edition(
                user_id, "day", subs=subs, active_subs=subs, ignore_rate_limit=True, now=now
            )

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.telegraph_url, "https://telegra.ph/published")
        save.assert_called_once()

    async def test_failed_edit_sends_new_message_for_each_terminal_status(self):
        statuses = ["ok", "timeout", "empty_posts", "error", "rate_limited", "already_building"]
        for index, status in enumerate(statuses):
            with self.subTest(status=status):
                user_id = 74000 + index
                callback = MagicMock()
                callback.from_user.id = user_id
                callback.data = "feed_period_week"
                callback.message = MagicMock()
                callback.message.edit_text = AsyncMock()
                callback.message.answer = AsyncMock()
                callback.answer = AsyncMock()
                subscription = {"channel_username": "chan", "channel_title": "Канал", "is_paused": 0}
                result = feed.FeedBuildResult(
                    status=status,
                    telegraph_url="https://telegra.ph/x" if status == "ok" else None,
                    post_count=1,
                    channel_count=1,
                    error_message="Ошибка сборки",
                )
                if status in {"ok", "empty_posts", "timeout", "error"}:
                    callback.message.edit_text.side_effect = [None, TelegramAPIError(method=MagicMock(), message="old message")]
                else:
                    callback.message.edit_text.side_effect = TelegramAPIError(method=MagicMock(), message="old message")
                with patch("feed.list_feed_subscriptions", return_value=[subscription]), \
                     patch("feed.get_feed_edition", return_value=None), \
                     patch("feed.build_feed_edition", new_callable=AsyncMock, return_value=result), \
                     patch.dict(feed._last_build_time, {}, clear=True), \
                     patch("feed.time.time", return_value=10_000):
                    if status == "rate_limited":
                        feed._last_build_time[user_id] = 9_999
                    if status == "already_building":
                        feed._building_users.add(user_id)
                    await feed.cb_feed_period(callback)
                    if status == "already_building":
                        feed._building_users.discard(user_id)
                self.assertGreaterEqual(callback.message.answer.await_count, 1)
                self.assertTrue(any(
                    call.args and (
                        "Открыть" in call.args[0]
                        or "постов не было" in call.args[0]
                        or "сборка" in call.args[0].lower()
                        or "ошибка" in call.args[0].lower()
                        or "Подожди" in call.args[0]
                        or "Сборка уже" in call.args[0]
                    )
                    for call in callback.message.answer.await_args_list
                ))


if __name__ == "__main__":
    unittest.main()
