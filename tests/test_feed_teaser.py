"""CT1: сообщение со ссылкой на выпуск Ленты сразу показывает «Главное» — три первых заголовка."""

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("BOT_TOKEN", "123456:TEST_TOKEN_FOR_UNIT_TESTS")

import feed  # noqa: E402
from data import database  # noqa: E402


class HeadlineHelpersTests(unittest.TestCase):
    def test_first_sentence_stops_at_sentence_end(self):
        self.assertEqual(
            feed._headline_from_text("Первое предложение. Второе предложение."),
            "Первое предложение.",
        )

    def test_newlines_become_spaces(self):
        self.assertEqual(
            feed._headline_from_text("Строка\nвторая\tстрока  третья"),
            "Строка вторая строка третья",
        )

    def test_empty_texts_produce_no_headline(self):
        for value in ("", None, "   \n\t "):
            with self.subTest(value=value):
                self.assertEqual(feed._headline_from_text(value), "")

    def test_long_sentence_is_cut_by_word_with_ellipsis(self):
        result = feed._headline_from_text("слово " * 40)
        self.assertLessEqual(len(result), feed.FEED_HEADLINE_MAX_CHARS)
        self.assertTrue(result.endswith("…"))
        self.assertNotIn("  ", result)

    def test_single_long_word_is_cut_with_ellipsis(self):
        result = feed._headline_from_text("я" * 200)
        self.assertLessEqual(len(result), feed.FEED_HEADLINE_MAX_CHARS)
        self.assertTrue(result.endswith("…"))

    def test_headlines_take_first_three_non_empty_posts(self):
        items = [
            SimpleNamespace(post={"text": ""}),
            SimpleNamespace(post={"text": "Первый"}),
            SimpleNamespace(post={"text": "  \n "}),
            SimpleNamespace(post={"text": "Второй"}),
            SimpleNamespace(post={"text": "Третий"}),
            SimpleNamespace(post={"text": "Четвёртый"}),
        ]
        self.assertEqual(feed._headlines_from_items(items), ["Первый", "Второй", "Третий"])
        self.assertIsNone(feed._headlines_from_items([SimpleNamespace(post={"text": ""})]))


class MessageFormatTests(unittest.TestCase):
    def test_message_without_headlines_is_unchanged(self):
        text = feed.format_feed_edition_message("day", 5, 3, "https://t.me/x")
        self.assertEqual(
            text,
            '📰 <b>Лента за день · 5 постов из 3 каналов</b>\n\n'
            '📖 <a href="https://t.me/x">Открыть</a>',
        )
        self.assertNotIn("Главное:", text)

    def test_message_shows_main_block_between_title_and_link(self):
        text = feed.format_feed_edition_message(
            "week", 2, 1, "https://t.me/x", ["Первый", "Второй"]
        )
        self.assertLess(text.index("📰"), text.index("Главное:"))
        self.assertLess(text.index("Главное:"), text.index("📖"))
        self.assertIn("• Первый", text)
        self.assertIn("• Второй", text)

    def test_headlines_are_html_escaped(self):
        text = feed.format_feed_edition_message(
            "day", 1, 1, "https://t.me/x", ["<b>&опасно</b>"]
        )
        self.assertIn("&lt;b&gt;&amp;опасно&lt;/b&gt;", text)
        self.assertNotIn("<b>&опасно", text)

    def test_message_fits_under_telegram_limit(self):
        headlines = ["Я" * feed.FEED_HEADLINE_MAX_CHARS for _ in range(feed.FEED_HEADLINES_LIMIT)]
        text = feed.format_feed_edition_message("month", 100, 10, "https://t.me/x", headlines)
        self.assertLess(len(text), 1000)


class BuildHeadlinesTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = os.path.join(self.temp_dir.name, "test_feed_teaser.db")
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

    def _configure_source(self):
        mock_source = MagicMock()
        mock_source.fetch = AsyncMock(return_value=([], "mtproto"))
        mock_source.refresh_metrics = AsyncMock(return_value=0)
        feed.configure(channel_source=mock_source)
        return mock_source

    def _seed_posts(self, channel: str, now: datetime) -> None:
        database.upsert_channel_posts(
            channel,
            [
                {"id": 1, "time": now - timedelta(hours=1), "text": "Первый заголовок выпуска. Продолжение.", "views": 5000, "forwards": 100, "reactions_total": 50, "replies": 5},
                {"id": 2, "time": now - timedelta(hours=2), "text": "Второй заголовок выпуска", "views": 4000, "forwards": 80, "reactions_total": 40, "replies": 4},
                {"id": 3, "time": now - timedelta(hours=3), "text": "Третий заголовок выпуска", "views": 3000, "forwards": 60, "reactions_total": 30, "replies": 3},
                {"id": 4, "time": now - timedelta(hours=4), "text": "Четвёртый заголовок", "views": 100, "forwards": 1, "reactions_total": 1, "replies": 0},
            ],
            "mtproto",
        )

    async def test_build_fills_headlines_from_three_top_posts(self):
        user_id = 70001
        now = datetime.now(timezone.utc)
        database.add_feed_subscription(user_id, "teaser_chan", "Teaser")
        self._seed_posts("teaser_chan", now)
        self._configure_source()

        with patch("feed.publish_page", new_callable=AsyncMock) as mock_pub:
            mock_pub.return_value = "https://telegra.ph/teaser"
            res = await feed.build_feed_edition(user_id, "day", now=now)

        self.assertEqual(res.status, "ok")
        self.assertFalse(res.cached)
        self.assertEqual(
            res.headlines,
            ["Первый заголовок выпуска.", "Второй заголовок выпуска", "Третий заголовок выпуска"],
        )

    async def test_cache_hit_recomputes_the_same_headlines(self):
        user_id = 70002
        now = datetime.now(timezone.utc)
        database.add_feed_subscription(user_id, "teaser_cache", "Teaser Cache")
        self._seed_posts("teaser_cache", now)
        self._configure_source()

        with patch("feed.publish_page", new_callable=AsyncMock) as mock_pub:
            mock_pub.return_value = "https://telegra.ph/teaser-cache"
            first = await feed.build_feed_edition(user_id, "day", now=now)
        self.assertIsNotNone(first.headlines)

        with patch("feed.publish_page", new_callable=AsyncMock) as mock_pub2:
            second = await feed.build_feed_edition(user_id, "day", now=now)
        self.assertTrue(second.cached)
        mock_pub2.assert_not_called()
        self.assertEqual(second.headlines, first.headlines)


if __name__ == "__main__":
    unittest.main()


class StoredHeadlinesTests(unittest.TestCase):
    """Заголовки «Главного» хранятся вместе с выпуском: кеш-попадание не пересчитывает ранжирование."""

    def setUp(self):
        import os
        import tempfile
        from data import database
        self.database = database
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        original = database.DB_PATH
        database.DB_PATH = os.path.join(self.tmp.name, "t.db")
        self.addCleanup(setattr, database, "DB_PATH", original)
        database.init_db()
        database.init_db()  # миграция колонки идемпотентна

    def test_headlines_roundtrip_through_edition_cache(self):
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        self.database.save_feed_edition(1, "day", now, now, "h", "https://telegra.ph/x", 3, 2, headlines=["Первый", "Второй <b>"])
        import feed
        cached = self.database.get_feed_edition(1, "day", "h")
        self.assertEqual(feed._stored_headlines(cached), ["Первый", "Второй <b>"])
        self.assertEqual(feed._stored_headlines(self.database.get_latest_feed_edition(1, "day")), ["Первый", "Второй <b>"])

    def test_old_or_broken_rows_give_no_headlines(self):
        import feed
        self.assertIsNone(feed._stored_headlines(None))
        self.assertIsNone(feed._stored_headlines({"headlines": None}))
        self.assertIsNone(feed._stored_headlines({"headlines": "{не json"}))
        self.assertIsNone(feed._stored_headlines({"headlines": "[]"}))
