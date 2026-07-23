import os
import tempfile
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

from channel_source import HybridChannelSource
from scraper import ChannelFetchError, _parse_channel_html


def message_html(post_id: int, timestamp: str, text: str = "Post") -> str:
    return f"""
    <div class="tgme_widget_message">
      <div class="tgme_widget_message_text">{text}</div>
      <a class="tgme_widget_message_date" href="https://t.me/example/{post_id}">
        <time class="time" datetime="{timestamp}">10:00</time>
      </a>
    </div>
    """


class ScraperValidationTests(unittest.TestCase):
    def test_extracts_stable_post_id(self):
        posts = _parse_channel_html(
            message_html(42, "2026-07-24T10:00:00+00:00"),
            "example",
            datetime(2026, 7, 24, 9, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(posts[0]["id"], 42)

    def test_http_200_without_message_blocks_is_temporary_failure(self):
        with self.assertRaises(ChannelFetchError) as raised:
            _parse_channel_html(
                "<html><title>Log in to Telegram</title></html>",
                "example",
                datetime.min.replace(tzinfo=timezone.utc),
            )
        self.assertFalse(raised.exception.permanent)

    def test_valid_page_with_no_new_posts_is_success(self):
        posts = _parse_channel_html(
            message_html(42, "2026-07-24T08:00:00+00:00"),
            "example",
            datetime(2026, 7, 24, 9, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(posts, [])


class DigestCursorTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        from data import database

        self.database = database
        self.original_db_path = database.DB_PATH
        database.DB_PATH = os.path.join(self.temp_dir.name, "test.db")
        database.init_db()

    def tearDown(self):
        self.database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def test_schedule_update_does_not_advance_delivery_cursor(self):
        db = self.database
        marker = "2026-07-23 07:00:00"
        sub_id = db.add_subscription(
            100,
            "example",
            "Example",
            "daily",
            marker,
            "2026-07-24 07:00:00",
        )
        db.update_subscription_schedule(
            sub_id,
            "2026-07-25 07:00:00",
            "2026-07-24 07:00:00",
        )
        sub = db.get_subscription_by_id(100, sub_id)
        self.assertEqual(sub["last_scraped_at"], marker)

    def test_shared_channel_posts_are_selected_by_post_id(self):
        db = self.database
        db.upsert_channel_posts(
            "Example",
            [
                {
                    "id": 41,
                    "time": datetime(2026, 7, 24, 8, 0, tzinfo=timezone.utc),
                    "text": "Old",
                    "link": "https://t.me/example/41",
                },
                {
                    "id": 42,
                    "time": datetime(2026, 7, 24, 9, 0, tzinfo=timezone.utc),
                    "text": "New",
                    "link": "https://t.me/example/42",
                },
            ],
            "web",
        )
        posts = db.get_channel_posts_since("example", None, last_post_id=41)
        self.assertEqual([post["id"] for post in posts], [42])

    def test_init_reactivates_legacy_network_failures(self):
        db = self.database
        sub_id = db.add_subscription(
            100,
            "example",
            "Example",
            "daily",
            "2026-07-23 07:00:00",
            "2026-07-24 07:00:00",
        )
        db.mark_subscription_delivery_error(
            sub_id,
            "Сетевая ошибка при чтении канала @example: DNS failure",
            "2026-07-24 07:00:00",
            5,
            True,
        )
        db.init_db()
        sub = db.get_subscription_by_id(100, sub_id)
        self.assertIsNotNone(sub)
        self.assertEqual(sub["digest_status"], "active")


class FakeMessageClient:
    def __init__(self, messages):
        self.messages = messages

    async def iter_messages(self, _username, limit):
        for message in self.messages[:limit]:
            yield message


class MtprotoSourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_mtproto_reads_new_posts_and_stops_at_marker(self):
        source = HybridChannelSource()
        source._client = FakeMessageClient(
            [
                SimpleNamespace(
                    id=42,
                    date=datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc),
                    message="New",
                ),
                SimpleNamespace(
                    id=41,
                    date=datetime(2026, 7, 24, 8, 0, tzinfo=timezone.utc),
                    message="Old",
                ),
            ]
        )
        posts = await source._fetch_mtproto("example", "2026-07-24 09:00:00")
        self.assertEqual([post["id"] for post in posts], [42])


if __name__ == "__main__":
    unittest.main()
