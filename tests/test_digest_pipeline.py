import os
import tempfile
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

# main.py builds the Bot at import time and refuses to start without a token.
os.environ.setdefault("BOT_TOKEN", "123456:TEST_TOKEN_FOR_UNIT_TESTS")

from channel_source import HybridChannelSource
from scraper import ChannelFetchError, _parse_channel_html


def message_html(post_id: int, timestamp: str, text: str | None = "Post") -> str:
    text_block = f'<div class="tgme_widget_message_text">{text}</div>' if text is not None else ""
    return f"""
    <div class="tgme_widget_message">
      {text_block}
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

    def test_scraper_retains_full_post_text(self):
        full_text = "A" * 3000
        posts = _parse_channel_html(
            message_html(42, "2026-07-24T10:00:00+00:00", text=full_text),
            "example",
            datetime(2026, 7, 24, 9, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(len(posts[0]["text"]), 3000)
        self.assertEqual(posts[0]["text"], full_text)

    def test_scraper_skips_posts_without_text(self):
        html = (
            message_html(10, "2026-07-24T10:00:00+00:00", text=None)
            + message_html(11, "2026-07-24T10:01:00+00:00", text="   ")
            + message_html(12, "2026-07-24T10:02:00+00:00", text="Real post with text")
        )
        posts = _parse_channel_html(
            html,
            "example",
            datetime(2026, 7, 24, 9, 0, tzinfo=timezone.utc),
        )
        self.assertEqual([p["id"] for p in posts], [12])
        self.assertEqual(posts[0]["text"], "Real post with text")


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

    def test_database_stores_and_returns_full_post_text(self):
        db = self.database
        full_text = "C" * 3000
        db.upsert_channel_posts(
            "Example",
            [
                {
                    "id": 100,
                    "time": datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc),
                    "text": full_text,
                    "link": "https://t.me/example/100",
                }
            ],
            "mtproto",
        )
        posts = db.get_channel_posts_since("example", None, last_post_id=99)
        self.assertEqual(len(posts[0]["text"]), 3000)
        self.assertEqual(posts[0]["text"], full_text)

    def test_pipeline_skips_media_posts_without_caption_in_channel_posts(self):
        import asyncio

        db = self.database
        source = HybridChannelSource()
        source._client = FakeMessageClient(
            [
                SimpleNamespace(
                    id=201,
                    date=datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc),
                    message=None,  # media without caption
                ),
                SimpleNamespace(
                    id=202,
                    date=datetime(2026, 7, 24, 10, 1, tzinfo=timezone.utc),
                    message="Media with caption",
                ),
            ]
        )
        posts = asyncio.run(source._fetch_mtproto("example", "2026-07-24 09:00:00"))
        db.upsert_channel_posts("example", posts, "mtproto")
        saved = db.get_channel_posts_since("example", None, last_post_id=200)
        self.assertEqual([p["id"] for p in saved], [202])
        self.assertEqual(saved[0]["text"], "Media with caption")

    def test_database_metric_columns_and_idempotent_update(self):
        db = self.database
        now = datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc)
        post = {
            "id": 501,
            "time": now,
            "text": "Post with metrics",
            "link": "https://t.me/example/501",
            "views": 100,
            "reactions_total": 7,
            "reactions_json": '[{"emoji": "👍", "count": 3}, {"emoji": "🔥", "count": 4}]',
            "forwards": None,
            "replies": None,
            "metrics_at": now,
        }
        db.upsert_channel_posts("example", [post], "mtproto")
        saved = db.get_channel_posts_since("example", None, last_post_id=500)
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["views"], 100)
        self.assertEqual(saved[0]["reactions_total"], 7)
        self.assertIsNone(saved[0]["forwards"])
        self.assertIsNone(saved[0]["replies"])
        self.assertIsNotNone(saved[0]["metrics_at"])

        later = datetime(2026, 7, 24, 12, 0, tzinfo=timezone.utc)
        post_updated = dict(post)
        post_updated["views"] = 150
        post_updated["reactions_total"] = 12
        post_updated["forwards"] = 10
        post_updated["metrics_at"] = later
        db.upsert_channel_posts("example", [post_updated], "mtproto")
        saved_updated = db.get_channel_posts_since("example", None, last_post_id=500)
        self.assertEqual(saved_updated[0]["views"], 150)
        self.assertEqual(saved_updated[0]["reactions_total"], 12)
        self.assertEqual(saved_updated[0]["forwards"], 10)
        self.assertEqual(saved_updated[0]["metrics_at"], later)

    def test_channel_subscribers_storage_and_ttl(self):
        db = self.database
        channel = "test_subscribers_channel"
        self.assertTrue(db.needs_channel_subscribers_update(channel))
        db.update_channel_subscribers(channel, 12500)
        self.assertEqual(db.get_channel_subscribers(channel), 12500)
        self.assertFalse(db.needs_channel_subscribers_update(channel))

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

    def test_due_batch_limit_processes_oldest_first(self):
        db = self.database
        for username, next_send_at in (
            ("newest", "2026-07-24 09:00:00"),
            ("oldest", "2026-07-24 07:00:00"),
            ("middle", "2026-07-24 08:00:00"),
        ):
            db.add_subscription(
                100,
                username,
                username,
                "daily",
                "2026-07-23 07:00:00",
                next_send_at,
            )

        now = "2026-07-24 10:00:00"
        due = db.get_due_subscriptions(now, limit=2)
        self.assertEqual(db.count_due_subscriptions(now), 3)
        self.assertEqual([row["channel_username"] for row in due], ["oldest", "middle"])


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

    async def test_mtproto_retains_full_post_text(self):
        source = HybridChannelSource()
        full_text = "B" * 3000
        source._client = FakeMessageClient(
            [
                SimpleNamespace(
                    id=43,
                    date=datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc),
                    message=full_text,
                ),
            ]
        )
        posts = await source._fetch_mtproto("example", "2026-07-24 09:00:00")
        self.assertEqual(len(posts[0]["text"]), 3000)
        self.assertEqual(posts[0]["text"], full_text)

    async def test_mtproto_skips_posts_without_text(self):
        source = HybridChannelSource()
        source._client = FakeMessageClient(
            [
                SimpleNamespace(
                    id=50,
                    date=datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc),
                    message=None,
                ),
                SimpleNamespace(
                    id=51,
                    date=datetime(2026, 7, 24, 10, 1, tzinfo=timezone.utc),
                    message="   \n  ",
                ),
                SimpleNamespace(
                    id=52,
                    date=datetime(2026, 7, 24, 10, 2, tzinfo=timezone.utc),
                    message="Caption on photo",
                ),
            ]
        )
        posts = await source._fetch_mtproto("example", "2026-07-24 09:00:00")
        self.assertEqual([p["id"] for p in posts], [52])
        self.assertEqual(posts[0]["text"], "Caption on photo")

    async def test_mtproto_collects_engagement_metrics(self):
        source = HybridChannelSource()
        reactions_mock = SimpleNamespace(
            results=[
                SimpleNamespace(reaction="👍", count=3),
                SimpleNamespace(reaction="🔥", count=4),
            ]
        )
        source._client = FakeMessageClient(
            [
                SimpleNamespace(
                    id=77,
                    date=datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc),
                    message="Metric post",
                    views=100,
                    forwards=5,
                    replies=SimpleNamespace(replies=2),
                    reactions=reactions_mock,
                ),
            ]
        )
        posts = await source._fetch_mtproto("example", "2026-07-24 09:00:00")
        self.assertEqual(len(posts), 1)
        post = posts[0]
        self.assertEqual(post["views"], 100)
        self.assertEqual(post["forwards"], 5)
        self.assertEqual(post["replies"], 2)
        self.assertEqual(post["reactions_total"], 7)
        self.assertIn('"count": 3', post["reactions_json"])
        self.assertIsNotNone(post["metrics_at"])


class DigestRenderingTests(unittest.TestCase):
    def test_telegram_preview_truncates_long_post(self):
        import main

        full_text = "D" * 3000
        posts = [{"id": 1, "text": full_text, "link": "https://t.me/example/1"}]
        lines = []
        main.append_digest_channel_lines(lines, 1, "daily", "Channel", posts)
        rendered = "\n".join(lines)
        expected_preview = "D" * 297 + "..."
        self.assertIn(expected_preview, rendered)
        self.assertNotIn(full_text, rendered)
        self.assertEqual(len(main.format_post_preview(full_text)), 300)

    def test_telegraph_build_post_node_truncates_to_telegraph_post_chars(self):
        from telegraph_publisher import TELEGRAPH_POST_CHARS, _build_post_node

        full_text = "E" * 3000
        post = {"id": 1, "text": full_text, "link": "https://t.me/example/1"}
        node = _build_post_node(post)
        node_text = "".join(
            c if isinstance(c, str) else (c.get("children", [""])[0] if isinstance(c.get("children"), list) else "")
            for c in node.get("children", [])
        )
        self.assertNotIn(full_text, node_text)
        self.assertIn("...", node_text)
        self.assertLessEqual(len(node_text), TELEGRAPH_POST_CHARS + 50)

    def test_telegraph_truncate_to_limit_emits_warning_and_notice(self):
        import logging
        from telegraph_publisher import _truncate_to_limit, build_content

        sections = [
            {
                "title": f"Channel {i}",
                "posts": [
                    {"id": j, "text": f"Post {j} " + "X" * 1000, "link": f"https://t.me/ch{i}/{j}"}
                    for j in range(10)
                ],
            }
            for i in range(5)
        ]
        content = build_content(sections)
        with self.assertLogs("telegraph_publisher", level=logging.WARNING) as log:
            truncated = _truncate_to_limit(content, max_bytes=20000)

        self.assertTrue(any("posts omitted" in record.getMessage() for record in log.records))
        last_node = truncated[-1]
        self.assertEqual(last_node.get("tag"), "p")
        notice_text = str(last_node)
        self.assertIn("Выпуск усечён", notice_text)


class FakeResponse:
    def __init__(self, text, status=200):
        self._text = text
        self.status = status

    async def text(self):
        return self._text

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass


class FakeClientSession:
    def __init__(self, pages_map):
        self.pages_map = pages_map
        self.requested_urls = []

    def get(self, url, headers=None):
        self.requested_urls.append(url)
        html = self.pages_map.get(url, "<html><body></body></html>")
        return FakeResponse(html)


class WebFallbackPaginationTests(unittest.IsolatedAsyncioTestCase):
    async def test_cursor_on_page_1_makes_one_request(self):
        from scraper import get_latest_posts

        page1 = (
            message_html(41, "2026-07-24T08:00:00+00:00", text="Post 41")
            + message_html(42, "2026-07-24T10:00:00+00:00", text="Post 42")
        )
        fake_session = FakeClientSession({"https://t.me/s/example": page1})
        posts = await get_latest_posts(
            "example",
            "2026-07-24 08:30:00",
            session=fake_session,
            page_delay=0,
        )
        self.assertEqual(fake_session.requested_urls, ["https://t.me/s/example"])
        self.assertTrue(posts.cursor_reached)
        self.assertEqual([p["id"] for p in posts], [42])

    async def test_cursor_on_page_2_makes_two_requests_with_before(self):
        from scraper import get_latest_posts

        page1 = (
            message_html(43, "2026-07-24T10:00:00+00:00", text="Post 43")
            + message_html(44, "2026-07-24T11:00:00+00:00", text="Post 44")
        )
        page2 = (
            message_html(41, "2026-07-24T08:00:00+00:00", text="Post 41")
            + message_html(42, "2026-07-24T09:00:00+00:00", text="Post 42")
        )
        fake_session = FakeClientSession(
            {
                "https://t.me/s/example": page1,
                "https://t.me/s/example?before=43": page2,
            }
        )
        posts = await get_latest_posts(
            "example",
            "2026-07-24 08:30:00",
            session=fake_session,
            page_delay=0,
        )
        self.assertEqual(
            fake_session.requested_urls,
            ["https://t.me/s/example", "https://t.me/s/example?before=43"],
        )
        self.assertTrue(posts.cursor_reached)
        self.assertEqual([p["id"] for p in posts], [42, 43, 44])

    async def test_ceiling_reached_cursor_not_reached_leaves_cursor_unmoved(self):
        from scraper import get_latest_posts

        page1 = message_html(30, "2026-07-24T12:00:00+00:00", text="Post 30")
        page2 = message_html(20, "2026-07-24T11:00:00+00:00", text="Post 20")
        fake_session = FakeClientSession(
            {
                "https://t.me/s/example": page1,
                "https://t.me/s/example?before=30": page2,
            }
        )
        posts = await get_latest_posts(
            "example",
            "2026-07-24 08:00:00",
            session=fake_session,
            max_pages=2,
            page_delay=0,
        )
        self.assertEqual(len(fake_session.requested_urls), 2)
        self.assertFalse(posts.cursor_reached)
        self.assertTrue(posts.is_partial)

        cursor_reached = getattr(posts, "cursor_reached", True)
        last_scraped = "2026-07-24 08:00:00"
        last_post_id = 10
        if cursor_reached:
            delivered_marker = max(p["time"] for p in posts)
            delivered_post_id = max(p["id"] for p in posts)
        else:
            from data.database import parse_db_datetime

            delivered_marker = parse_db_datetime(last_scraped)
            delivered_post_id = last_post_id

        from data.database import serialize_datetime

        self.assertEqual(serialize_datetime(delivered_marker), last_scraped)
        self.assertEqual(delivered_post_id, last_post_id)


class FakeJsonResponse:
    def __init__(self, data, status=200):
        self._data = data
        self.status = status

    async def json(self):
        return self._data

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass


class FakeTelegraphSession:
    def __init__(self, post_handler):
        self.post_handler = post_handler
        self.calls = []

    def post(self, url, data=None):
        self.calls.append((url, data))
        return self.post_handler(url, data)


class TelegraphPublisherTokenRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        from data import database

        self.database = database
        self.original_db_path = database.DB_PATH
        database.DB_PATH = os.path.join(self.temp_dir.name, "telegraph_test.db")
        database.init_db()

    def tearDown(self):
        self.database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def test_delete_app_meta(self):
        self.database.set_app_meta("test_key", "val123")
        self.assertEqual(self.database.get_app_meta("test_key"), "val123")
        self.database.delete_app_meta("test_key")
        self.assertIsNone(self.database.get_app_meta("test_key"))

    async def test_telegraph_token_invalid_recovery_and_retry(self):
        import logging
        from telegraph_publisher import _TOKEN_META_KEY, publish_digest

        self.database.set_app_meta(_TOKEN_META_KEY, "expired_token_123")

        call_log = []

        def handler(url, data):
            call_log.append((url, dict(data) if isinstance(data, dict) else data))
            if url.endswith("/createPage"):
                if data.get("access_token") == "expired_token_123":
                    return FakeJsonResponse({"ok": False, "error": "ACCESS_TOKEN_INVALID"})
                elif data.get("access_token") == "fresh_token_456":
                    return FakeJsonResponse({"ok": True, "result": {"url": "https://telegra.ph/digest-success"}})
            elif url.endswith("/createAccount"):
                return FakeJsonResponse({"ok": True, "result": {"access_token": "fresh_token_456"}})
            return FakeJsonResponse({"ok": False, "error": "unknown"})

        fake_session = FakeTelegraphSession(handler)
        sections = [{"title": "News", "posts": [{"id": 1, "text": "Post 1", "link": "https://t.me/c/1"}]}]

        with self.assertLogs("telegraph_publisher", level=logging.WARNING) as log:
            url = await publish_digest("Test Digest", sections, session=fake_session)

        self.assertEqual(url, "https://telegra.ph/digest-success")

        warnings = [r for r in log.records if r.levelno == logging.WARNING]
        self.assertEqual(len(warnings), 1)
        self.assertIn("ACCESS_TOKEN_INVALID", warnings[0].getMessage())

        errors = [r for r in log.records if r.levelno >= logging.ERROR]
        self.assertEqual(len(errors), 0)

        self.assertEqual(self.database.get_app_meta(_TOKEN_META_KEY), "fresh_token_456")

        self.assertEqual(len(call_log), 3)
        self.assertTrue(call_log[0][0].endswith("/createPage"))
        self.assertEqual(call_log[0][1]["access_token"], "expired_token_123")
        self.assertTrue(call_log[1][0].endswith("/createAccount"))
        self.assertTrue(call_log[2][0].endswith("/createPage"))
        self.assertEqual(call_log[2][1]["access_token"], "fresh_token_456")

    async def test_telegraph_token_invalid_recovery_fails_gracefully(self):
        from telegraph_publisher import _TOKEN_META_KEY, publish_digest

        self.database.set_app_meta(_TOKEN_META_KEY, "expired_token_123")

        def handler(url, data):
            if url.endswith("/createPage"):
                return FakeJsonResponse({"ok": False, "error": "ACCESS_TOKEN_INVALID"})
            elif url.endswith("/createAccount"):
                return FakeJsonResponse({"ok": True, "result": {"access_token": "fresh_token_456"}})
            return FakeJsonResponse({"ok": False, "error": "unknown"})

        fake_session = FakeTelegraphSession(handler)
        sections = [{"title": "News", "posts": [{"id": 1, "text": "Post 1"}]}]

        url = await publish_digest("Test Digest", sections, session=fake_session)
        self.assertIsNone(url)
        self.assertEqual(len(fake_session.calls), 3)


if __name__ == "__main__":
    unittest.main()
