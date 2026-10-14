import asyncio
from datetime import datetime, timedelta, timezone
import os
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

os.environ.setdefault("BOT_TOKEN", "123456:TEST_TOKEN_FOR_UNIT_TESTS")

from channel_source import HybridChannelSource  # noqa: E402
from data import database  # noqa: E402
import feed  # noqa: E402


class FakeTelethonClient:
    def __init__(self, messages=None, views_responses=None):
        self.messages = messages or []
        self.views_responses = views_responses or []
        self.call_history = []

    async def iter_messages(self, _username, limit=1000):
        for msg in self.messages[:limit]:
            yield msg

    async def __call__(self, request):
        self.call_history.append(request)
        if isinstance(self.views_responses, Exception):
            raise self.views_responses
        if callable(self.views_responses):
            return self.views_responses(request)
        if isinstance(self.views_responses, list):
            return self.views_responses.pop(0) if self.views_responses else SimpleNamespace(views=[])
        if self.views_responses is not None:
            return self.views_responses
        return SimpleNamespace(views=[])


class FeedIngestAndMetricsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = os.path.join(self.temp_dir.name, "test_feed_ingest.db")
        database.init_db()

    async def asyncTearDown(self):
        await feed.cancel_backfill_tasks()
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    async def test_refresh_metrics_batches_by_100(self):
        """refresh_metrics requests in batches of <= 100 post IDs."""
        source = HybridChannelSource()
        channel = "batch_chan"
        now = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
        post_ids = list(range(1, 251))  # 250 posts

        # Seed posts in DB
        posts_to_insert = [
            {
                "id": pid,
                "time": now - timedelta(hours=1),
                "text": f"Post {pid}",
                "link": f"https://t.me/{channel}/{pid}",
                "views": 10,
                "forwards": 1,
                "replies": 0,
                "metrics_at": now - timedelta(hours=2),  # Stale
            }
            for pid in post_ids
        ]
        database.upsert_channel_posts(channel, posts_to_insert, "mtproto")

        call_batches = []

        def mock_responder(req):
            call_batches.append(list(req.id))
            views = [
                SimpleNamespace(views=100 + pid, forwards=pid, replies=SimpleNamespace(replies=2))
                for pid in req.id
            ]
            return SimpleNamespace(views=views)

        client = FakeTelethonClient(views_responses=mock_responder)
        source._client = client

        updated_count = await source.refresh_metrics(channel, post_ids, period="day", now=now)

        self.assertEqual(updated_count, 250)
        self.assertEqual(len(call_batches), 3)
        self.assertEqual(len(call_batches[0]), 100)
        self.assertEqual(len(call_batches[1]), 100)
        self.assertEqual(len(call_batches[2]), 50)

        # Check DB updated
        stale = database.get_stale_metric_post_ids(channel, post_ids, cutoff=now - timedelta(minutes=30))
        self.assertEqual(stale, [])

    async def test_refresh_metrics_staleness_thresholds(self):
        """Only posts older than threshold (day: 30m, week: 3h, month: 12h) are refreshed."""
        source = HybridChannelSource()
        channel = "stale_chan"
        now = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)

        posts = [
            {
                "id": 1,
                "time": now - timedelta(hours=1),
                "text": "Post 1",
                "link": f"https://t.me/{channel}/1",
                "metrics_at": now - timedelta(minutes=10),  # Fresh (<30m)
            },
            {
                "id": 2,
                "time": now - timedelta(hours=2),
                "text": "Post 2",
                "link": f"https://t.me/{channel}/2",
                "metrics_at": now - timedelta(minutes=45),  # Stale for day (>30m), but fresh for week (<3h)
            },
            {
                "id": 3,
                "time": now - timedelta(hours=5),
                "text": "Post 3",
                "link": f"https://t.me/{channel}/3",
                "metrics_at": None,  # Stale for everything
            },
        ]
        database.upsert_channel_posts(channel, posts, "mtproto")

        # Day test (threshold 30 min) -> posts 2 and 3 stale
        stale_day = database.get_stale_metric_post_ids(channel, [1, 2, 3], now - timedelta(seconds=1800))
        self.assertEqual(stale_day, [2, 3])

        # Week test (threshold 3 hours) -> only post 3 stale
        stale_week = database.get_stale_metric_post_ids(channel, [1, 2, 3], now - timedelta(seconds=10800))
        self.assertEqual(stale_week, [3])

        # Month test (threshold 12 hours) -> only post 3 stale
        stale_month = database.get_stale_metric_post_ids(channel, [1, 2, 3], now - timedelta(seconds=43200))
        self.assertEqual(stale_month, [3])

        # Execute refresh with period='day'
        called_ids = []

        def mock_responder(req):
            called_ids.extend(req.id)
            return SimpleNamespace(views=[SimpleNamespace(views=99, forwards=5, replies=None) for _ in req.id])

        source._client = FakeTelethonClient(views_responses=mock_responder)
        updated = await source.refresh_metrics(channel, [1, 2, 3], period="day", now=now)
        self.assertEqual(updated, 2)
        self.assertEqual(called_ids, [2, 3])

    async def test_refresh_metrics_flood_wait_and_errors(self):
        """Errors (e.g. FloodWaitError) stop refresh for the channel with WARNING and return count so far."""
        source = HybridChannelSource()
        channel = "err_chan"
        now = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
        post_ids = list(range(1, 151))  # 150 posts -> 2 batches (100 and 50)

        posts = [
            {
                "id": pid,
                "time": now - timedelta(hours=1),
                "text": f"P {pid}",
                "link": f"https://t.me/{channel}/{pid}",
                "views": 10,
                "metrics_at": None,
            }
            for pid in post_ids
        ]
        database.upsert_channel_posts(channel, posts, "mtproto")

        batch_count = 0

        def mock_responder(req):
            nonlocal batch_count
            batch_count += 1
            if batch_count == 1:
                return SimpleNamespace(views=[SimpleNamespace(views=50, forwards=1, replies=None) for _ in req.id])
            raise RuntimeError("Telegram FloodWait simulated")

        source._client = FakeTelethonClient(views_responses=mock_responder)
        updated = await source.refresh_metrics(channel, post_ids, period="day", now=now)

        self.assertEqual(updated, 100)
        self.assertEqual(batch_count, 2)

    async def test_refresh_metrics_web_mode_skips(self):
        """Without MTProto client (web mode), refresh_metrics returns 0 immediately."""
        source = HybridChannelSource()
        source._client = None
        updated = await source.refresh_metrics("any_chan", [1, 2, 3])
        self.assertEqual(updated, 0)

    async def test_refresh_metrics_preserves_reactions(self):
        """Invariant: update_post_metrics and refresh_metrics MUST NOT overwrite reaction metrics."""
        source = HybridChannelSource()
        channel = "react_chan"
        now = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)

        post = {
            "id": 88,
            "time": now - timedelta(hours=1),
            "text": "Post with reactions",
            "link": f"https://t.me/{channel}/88",
            "views": 20,
            "forwards": 2,
            "replies": 1,
            "reactions_total": 42,
            "reactions_json": '[{"reaction": "👍", "count": 42}]',
            "metrics_at": now - timedelta(hours=2),
        }
        database.upsert_channel_posts(channel, [post], "mtproto")

        source._client = FakeTelethonClient(
            views_responses=SimpleNamespace(
                views=[SimpleNamespace(views=500, forwards=50, replies=SimpleNamespace(replies=10))]
            )
        )

        await source.refresh_metrics(channel, [88], period="day", now=now)

        with database.get_connection() as conn:
            row = conn.execute(
                "SELECT views, forwards, replies, reactions_total, reactions_json FROM channel_posts WHERE channel_username = ? AND post_id = 88",
                (channel,),
            ).fetchone()

        self.assertEqual(row["views"], 500)
        self.assertEqual(row["forwards"], 50)
        self.assertEqual(row["replies"], 10)
        self.assertEqual(row["reactions_total"], 42)
        self.assertEqual(row["reactions_json"], '[{"reaction": "👍", "count": 42}]')

    async def test_mtproto_fetches_fwd_from_and_channel_id(self):
        """_fetch_mtproto populates channel_id, fwd_from_channel_id, fwd_from_post_id."""
        source = HybridChannelSource()
        now = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)

        msg = SimpleNamespace(
            id=101,
            date=now - timedelta(minutes=5),
            message="Forwarded text",
            views=100,
            forwards=10,
            replies=None,
            reactions=None,
            peer_id=SimpleNamespace(channel_id=999888),
            fwd_from=SimpleNamespace(
                from_id=SimpleNamespace(channel_id=777666),
                channel_post=42,
            ),
        )
        source._client = FakeTelethonClient([msg])

        posts = await source._fetch_mtproto("forward_chan", "2026-10-14 10:00:00")
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["fwd_from_channel_id"], 777666)
        self.assertEqual(posts[0]["fwd_from_post_id"], 42)

        # Check channel_id saved in channels table
        cid = database.get_channel_id("forward_chan")
        self.assertEqual(cid, 999888)

        # Save to DB and check candidate query
        database.upsert_channel_posts("forward_chan", posts, "mtproto")
        database.add_feed_subscription(user_id=1, channel_username="forward_chan", channel_title="Fwd Chan")

        candidates = database.get_feed_candidate_posts(
            user_id=1,
            window_start=now - timedelta(hours=1),
            window_end=now,
        )
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["channel_id"], 999888)
        self.assertEqual(candidates[0]["fwd_from_channel_id"], 777666)
        self.assertEqual(candidates[0]["fwd_from_post_id"], 42)

    async def test_rule_1_7_cursor_not_advanced_when_gap_exists(self):
        """Rule 1.7: If feed load starts after channel cursor (marker > last_scanned_at), cursor must not move."""
        source = HybridChannelSource()
        channel = "gap_chan"
        now = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)

        # Cursor was scanned 7 days ago
        cursor_time = now - timedelta(days=7)
        database.update_channel_scan_cursor(channel, cursor_time, 10)

        # Feed loads day window (24h ago). History is marked backfilled.
        database.set_channel_feed_backfilled_at(channel, now - timedelta(days=1))
        # Feed marker will be max(24h ago, 7d ago) = 24h ago.
        # marker (24h ago) > last_scanned_at (7d ago) -> gap of 6 days!

        new_post_time = now - timedelta(hours=2)
        source.fetch = AsyncMock(
            return_value=(
                [{"id": 20, "time": new_post_time, "text": "New post", "link": f"https://t.me/{channel}/20"}],
                "mtproto",
            )
        )

        window_start = now - timedelta(hours=24)
        await feed.ingest_window([channel], window_start=window_start, now=now, channel_source=source)

        # Invariant: cursor MUST NOT move!
        scanned_at_str, scanned_id = database.get_channel_scan_cursor(channel)
        self.assertEqual(database.parse_db_datetime(scanned_at_str), cursor_time)
        self.assertEqual(scanned_id, 10)

    async def test_mandatory_digest_receives_all_posts_after_feed_ingest(self):
        """Mandatory invariant test: Digest receives all its posts after Feed ingestion."""
        channel = "shared_chan"
        now = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)

        # User has digest subscription created 7 days ago, last_scraped_at = 7 days ago
        seven_days_ago = now - timedelta(days=7)
        database.add_subscription(
            user_id=1,
            channel_username=channel,
            channel_title="Shared",
            period="daily",
            last_scraped_at=database.serialize_datetime(seven_days_ago),
            next_send_at=database.serialize_datetime(now),
        )
        database.update_channel_scan_cursor(channel, seven_days_ago, 100)

        # History is backfilled for feed
        database.set_channel_feed_backfilled_at(channel, now)

        # Posts available from Telegram:
        # Post 101 at 5 days ago (should be read by digest)
        # Post 102 at 1 day ago (read by feed day ingest and digest)
        post_101 = {
            "id": 101,
            "time": now - timedelta(days=5),
            "text": "Post 101 5d ago",
            "link": f"https://t.me/{channel}/101",
        }
        post_102 = {
            "id": 102,
            "time": now - timedelta(hours=5),
            "text": "Post 102 5h ago",
            "link": f"https://t.me/{channel}/102",
        }

        # Step 1: User runs Feed for day (24h window)
        # Feed only fetches post 102
        feed_source = HybridChannelSource()
        feed_source.fetch = AsyncMock(return_value=([post_102], "mtproto"))

        await feed.ingest_window(
            [channel],
            window_start=now - timedelta(hours=24),
            now=now,
            channel_source=feed_source,
        )

        # Cursor must still be 7 days ago because marker (24h ago) > cursor (7d ago)
        cursor_str, _ = database.get_channel_scan_cursor(channel)
        self.assertEqual(database.parse_db_datetime(cursor_str), seven_days_ago)

        # Step 2: Digest now runs!
        # Digest checks scan_marker: max(sub.last_scraped_at, cursor) = max(7d ago, 7d ago) = 7d ago
        sub = database.get_subscription_by_id(1, 1)
        channel_scanned_at, _ = database.get_channel_scan_cursor(channel)
        scan_marker = database.serialize_datetime(
            max(database.parse_db_datetime(sub["last_scraped_at"]), database.parse_db_datetime(channel_scanned_at))
        )
        self.assertEqual(database.parse_db_datetime(scan_marker), seven_days_ago)

        # Telegram returns posts since 7 days ago:
        digest_source = HybridChannelSource()
        digest_source.fetch = AsyncMock(return_value=([post_101, post_102], "mtproto"))
        fetched_posts, sname = await digest_source.fetch(channel, scan_marker)
        database.upsert_channel_posts(channel, fetched_posts, sname)

        # Digest queries available posts since sub["last_scraped_at"]
        available = database.get_channel_posts_since(channel, sub["last_scraped_at"])
        available_ids = [p["id"] for p in available]
        self.assertIn(101, available_ids)
        self.assertIn(102, available_ids)
        self.assertEqual(len(available), 2)

    async def test_rule_1_7_cursor_advanced_when_no_hole(self):
        """When marker <= last_scanned_at, there is no hole and cursor advances to latest post."""
        source = HybridChannelSource()
        channel = "contig_chan"
        now = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)

        # Cursor was scanned 1 hour ago
        one_hour_ago = now - timedelta(hours=1)
        database.update_channel_scan_cursor(channel, one_hour_ago, 50)
        database.set_channel_feed_backfilled_at(channel, now)

        # Feed loads day window (24h ago).
        # Since backfilled and cursor exists:
        # marker = max(24h ago, 1h ago) = 1h ago.
        # marker (1h ago) <= cursor (1h ago) -> NO HOLE!
        latest_post_time = now - timedelta(minutes=10)
        source.fetch = AsyncMock(
            return_value=(
                [{"id": 51, "time": latest_post_time, "text": "Fresh", "link": f"https://t.me/{channel}/51"}],
                "mtproto",
            )
        )

        await feed.ingest_window(
            [channel],
            window_start=now - timedelta(hours=24),
            now=now,
            channel_source=source,
        )

        scanned_at_str, scanned_id = database.get_channel_scan_cursor(channel)
        self.assertEqual(database.parse_db_datetime(scanned_at_str), latest_post_time)
        self.assertEqual(scanned_id, 51)

    async def test_feed_backfilled_at_behavior(self):
        """Initial load ignores cursor until backfilled_at is set by 30-day ingest."""
        source = HybridChannelSource()
        channel = "backfill_chan"
        now = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)

        # Digest had a cursor from 2 hours ago, but history is NOT backfilled yet
        database.update_channel_scan_cursor(channel, now - timedelta(hours=2), 10)
        self.assertIsNone(database.get_channel_feed_backfilled_at(channel))

        # Feed loads for month (30d ago) without backfill flag yet:
        # It should query from 30 days ago, not 2 hours ago!
        captured_markers = []

        async def fake_fetch(username, marker_str, **kwargs):
            captured_markers.append(marker_str)
            return [], "mtproto"

        source.fetch = fake_fetch

        # Ingest 30 days
        thirty_days_ago = now - timedelta(days=30)
        await feed.ingest_window([channel], window_start=thirty_days_ago, now=now, channel_source=source)

        # Verify marker was 30 days ago (ignoring 2h cursor!)
        self.assertEqual(database.parse_db_datetime(captured_markers[0]), thirty_days_ago)

        # feed_backfilled_at is now set!
        self.assertIsNotNone(database.get_channel_feed_backfilled_at(channel))

        # Next time: backfilled is set, so marker = max(window_start, cursor)
        database.update_channel_scan_cursor(channel, now - timedelta(hours=1), 15)
        await feed.ingest_window([channel], window_start=thirty_days_ago, now=now, channel_source=source)
        # Should use max(30d ago, 1h ago) = 1h ago
        self.assertEqual(database.parse_db_datetime(captured_markers[1]), now - timedelta(hours=1))

    async def test_schedule_channel_backfill_deduplication(self):
        """schedule_channel_backfill deduplicates simultaneous requests for the same channel."""
        feed._backfill_tasks.clear()
        feed._backfill_hook = None

        started_events = {}
        finish_events = {}

        async def slow_fetch(username, marker_str, **kwargs):
            started_events[username].set()
            await finish_events[username].wait()
            return [], "mtproto"

        source = HybridChannelSource()
        source.fetch = slow_fetch
        feed.configure(channel_source=source)

        started_events["chan_dedup"] = asyncio.Event()
        finish_events["chan_dedup"] = asyncio.Event()

        task1 = feed.schedule_channel_backfill("chan_dedup")
        self.assertIsNotNone(task1)

        # Wait until runner starts
        await started_events["chan_dedup"].wait()

        # Try to schedule again while task1 is running
        task2 = feed.schedule_channel_backfill("chan_dedup")
        self.assertIs(task1, task2)  # Same task returned, no 2nd task created!

        # Complete task
        finish_events["chan_dedup"].set()
        await task1

        # After task completes, dictionary is cleaned up
        self.assertNotIn("chan_dedup", feed._backfill_tasks)


if __name__ == "__main__":
    unittest.main()
