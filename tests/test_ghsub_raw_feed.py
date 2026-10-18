"""Tests for the weekly raw_feed export to ghsub and the ghsub weekly window for email digests.

Requirements (решения владельца 18.10.2026, контракт JSON v1, раздел raw_feed):
1. Header: schema_version 1.0, edition_id unagi:raw_feed:<recipient>:<ISO week>, kind raw_feed,
   period weekly, recipient_email, generated_at, window {from, to, timezone}.
2. Items: post_id int, published_at ISO UTC, channel, url t.me/<username>/<post_id>, plain text <= 500,
   external_urls http(s) without utm_*/fbclid/yclid, metrics {views, forwards, reactions} ints >= 0.
3. Not exported: reposts, polls, media with a caption shorter than 40 chars. <= 400 posts and 1 MB.
4. Only users with email Feed delivery; atomic write to the outbox; no ranking.
5. Export on Sunday 05:30 MSK (before ghsub's 06:15 deadline), once per ISO week, skipped after 08:00.
6. Weekly email digests under ghsub are scheduled for Sunday 05:00 MSK; Telegram Feed keeps user time.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
import tempfile
import unittest
from unittest import mock
from zoneinfo import ZoneInfo

os.environ.setdefault("BOT_TOKEN", "123456:TEST_TOKEN_FOR_UNIT_TESTS")

from channel_source import _describe_media_and_links  # noqa: E402
from config import reset_settings  # noqa: E402
from data import database  # noqa: E402
import digest_settings  # noqa: E402
import ghsub_raw_feed  # noqa: E402
from ghsub_raw_feed import (  # noqa: E402
    MAX_BYTES,
    MAX_ITEMS,
    build_raw_feed_edition,
    clean_external_url,
    export_slot,
    run_raw_feed_export_once,
)
import main  # noqa: E402
from scraper import _parse_channel_html_page  # noqa: E402

MSK = ZoneInfo("Europe/Moscow")
# Воскресенье 18.10.2026 05:30 МСК = 02:30Z, ISO-неделя 2026-W42.
SLOT = datetime(2026, 10, 18, 5, 30, tzinfo=MSK)


def _post(post_id: int, *, channel: str = "tech_news", text: str | None = None, hours_ago: int = 1, **extra) -> dict:
    return {
        "channel_username": channel,
        "channel_title": "Tech News",
        "post_id": post_id,
        "text": text if text is not None else f"Новость номер {post_id}: подробности в канале и немного контекста.",
        "post_time": SLOT.astimezone(timezone.utc) - timedelta(hours=hours_ago),
        "views": 1000 + post_id,
        "forwards": 10,
        "reactions_total": 5,
        **extra,
    }


class RawFeedEditionTests(unittest.TestCase):
    def test_header_matches_contract(self):
        generated = datetime(2026, 10, 18, 2, 31, tzinfo=timezone.utc)
        payload = build_raw_feed_edition(42, "user@example.com", [_post(1)], SLOT, generated_at=generated)

        self.assertEqual(payload["schema_version"], "1.0")
        self.assertEqual(payload["edition_id"], "unagi:raw_feed:unagi-42:2026-W42")
        self.assertEqual(payload["kind"], "raw_feed")
        self.assertEqual(payload["recipient"], "unagi-42")
        self.assertEqual(payload["period"], "weekly")
        self.assertEqual(payload["recipient_email"], "user@example.com")
        self.assertEqual(payload["generated_at"], "2026-10-18T02:31:00Z")
        self.assertEqual(
            payload["window"],
            {"from": "2026-10-11T02:30:00Z", "to": "2026-10-18T02:30:00Z", "timezone": "Europe/Moscow"},
        )

    def test_item_fields_and_types(self):
        posts = [
            _post(
                7,
                text="<b>Жирный</b> &amp; важный текст со ссылкой https://example.com/a?utm_source=tg&id=5&fbclid=x.",
                views=None,
                forwards=-3,
                reactions_total="12",
                link_urls=json.dumps(["https://hidden.example.org/p?yclid=1&utm_medium=x", "https://t.me/other/1"]),
            )
        ]
        item = build_raw_feed_edition(42, "user@example.com", posts, SLOT)["items"][0]

        self.assertEqual(set(item), {"post_id", "published_at", "channel", "url", "text", "external_urls", "metrics"})
        self.assertIsInstance(item["post_id"], int)
        self.assertEqual(item["published_at"], "2026-10-18T01:30:00Z")
        self.assertEqual(item["channel"], {"username": "tech_news", "title": "Tech News"})
        self.assertEqual(item["url"], "https://t.me/tech_news/7")
        self.assertNotIn("<b>", item["text"])
        self.assertIn("Жирный & важный", item["text"])
        self.assertEqual(item["external_urls"], ["https://example.com/a?id=5", "https://hidden.example.org/p"])
        self.assertEqual(item["metrics"], {"views": 0, "forwards": 0, "reactions": 12})

    def test_text_is_plain_single_line_and_at_most_500_chars(self):
        long_text = "Абзац первый.\n\n" + " ".join(["слово"] * 400)
        item = build_raw_feed_edition(42, "user@example.com", [_post(1, text=long_text)], SLOT)["items"][0]
        self.assertLessEqual(len(item["text"]), 500)
        self.assertNotIn("\n", item["text"])
        self.assertTrue(item["text"].endswith("…"))

    def test_clean_external_url(self):
        self.assertEqual(clean_external_url("https://a.ru/x?UTM_Campaign=1&q=2"), "https://a.ru/x?q=2")
        self.assertEqual(clean_external_url("https://a.ru/x?utm_source=1"), "https://a.ru/x")
        self.assertEqual(clean_external_url("https://ru.wikipedia.org/wiki/A_(b))."), "https://ru.wikipedia.org/wiki/A_(b)")
        self.assertIsNone(clean_external_url("javascript:alert(1)"))
        self.assertIsNone(clean_external_url("ftp://a.ru/x"))
        self.assertIsNone(clean_external_url("https://t.me/channel/5"))

    def test_skips_reposts_polls_and_media_with_short_caption(self):
        posts = [
            _post(1, is_forward=1),
            _post(2, fwd_from_channel_id=777),
            _post(3, media_kind="poll", text="Какой язык выбрать для бэкенда в новом проекте команды?"),
            _post(4, media_kind="media", text="Фото дня"),
            _post(5, media_kind="media", text="Фото дня: так выглядит новый дата-центр изнутри после запуска"),
            _post(6, media_kind=None, text="Короткий"),
            _post(7, media_kind="", text="Тоже коротко"),
        ]
        items = build_raw_feed_edition(42, "user@example.com", posts, SLOT)["items"]
        self.assertEqual([it["post_id"] for it in items], [5, 6, 7])

    def test_no_ranking_items_are_chronological(self):
        posts = [_post(1, hours_ago=1, views=10), _post(2, hours_ago=50, views=99999), _post(3, hours_ago=20)]
        items = build_raw_feed_edition(42, "user@example.com", posts, SLOT)["items"]
        self.assertEqual([it["post_id"] for it in items], [2, 3, 1])

    def test_at_most_400_items_shared_across_channels(self):
        posts = [_post(i, channel="chatty", hours_ago=1 + i % 150) for i in range(1, 501)]
        posts += [_post(i, channel="quiet", hours_ago=100) for i in range(1, 11)]
        items = build_raw_feed_edition(42, "user@example.com", posts, SLOT)["items"]
        self.assertEqual(len(items), MAX_ITEMS)
        self.assertEqual(sum(1 for it in items if it["channel"]["username"] == "quiet"), 10)

    def test_payload_at_most_1_mb(self):
        text = "Очень длинный пост с подробностями " * 20
        urls = json.dumps([f"https://example.com/{'x' * 400}/{i}" for i in range(6)])
        posts = [_post(i, channel=f"ch{i % 7}", text=text, link_urls=urls, hours_ago=1 + i % 160) for i in range(1, 401)]
        payload = build_raw_feed_edition(42, "user@example.com", posts, SLOT)
        size = len(json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))
        self.assertLessEqual(size, MAX_BYTES)
        self.assertGreater(len(payload["items"]), 0)
        self.assertLess(len(payload["items"]), 400)

    def test_bad_address_is_not_written(self):
        payload = build_raw_feed_edition(42, "a@b.c\nBcc: x@y.z", [_post(1)], SLOT)
        self.assertNotIn("recipient_email", payload)


class RawFeedExportTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = os.path.join(self.temp_dir.name, "test_raw_feed.db")
        database.init_db()
        self.outbox = os.path.join(self.temp_dir.name, "outbox")
        self.env = mock.patch.dict(os.environ, {"EMAIL_DELIVERY": "ghsub", "EMAIL_OUTBOX_DIR": self.outbox})
        self.env.start()
        reset_settings()
        self.ingest = mock.patch.object(ghsub_raw_feed.feed, "ingest_window", new=mock.AsyncMock(return_value={}))
        self.ingest_mock = self.ingest.start()

    def tearDown(self):
        self.ingest.stop()
        self.env.stop()
        reset_settings()
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def _set_email(self, user_id: int, email: str, periods: str) -> None:
        with database.get_connection() as conn:
            conn.execute(
                "INSERT INTO user_emails (user_id, email, email_periods) VALUES (?, ?, ?)",
                (user_id, email, periods),
            )
            conn.commit()

    def _store_posts(self, channel: str, posts: list[dict]) -> None:
        database.upsert_channel_posts(channel, posts, "mtproto")

    def _outbox_files(self) -> list[str]:
        return sorted(os.listdir(self.outbox)) if os.path.isdir(self.outbox) else []

    def test_ingest_stores_media_forward_and_hidden_links(self):
        base = {"time": SLOT - timedelta(hours=2), "text": "Пост", "metrics_at": SLOT}
        self._store_posts("tech_news", [{**base, "id": 1, "media_kind": "media", "is_forward": False, "link_urls": ["https://a.ru"]}])
        # Повторное чтение без этих полей (например, обновление метрик) не стирает известные значения.
        self._store_posts("tech_news", [{**base, "id": 1}])
        with database.get_connection() as conn:
            row = conn.execute("SELECT media_kind, is_forward, link_urls FROM channel_posts WHERE post_id = 1").fetchone()
        self.assertEqual((row["media_kind"], row["is_forward"], json.loads(row["link_urls"])), ("media", 0, ["https://a.ru"]))

    def test_export_writes_only_for_users_with_email_feed(self):
        database.add_feed_subscription(1, "tech_news", "Tech News")
        database.add_feed_subscription(2, "tech_news", "Tech News")
        database.add_feed_subscription(3, "tech_news", "Tech News")
        self._set_email(1, "one@example.com", "feed,weekly")
        self._set_email(2, "two@example.com", "weekly")
        self._set_email(3, "", "feed")
        self._store_posts("tech_news", [
            {"id": 10, "time": SLOT - timedelta(days=1), "text": "Пост внутри окна выгрузки за неделю.", "views": 50},
            {"id": 11, "time": SLOT - timedelta(days=8), "text": "Пост старше недели не выгружается.", "views": 50},
            {"id": 12, "time": SLOT + timedelta(minutes=5), "text": "Пост после конца окна не выгружается.", "views": 50},
        ])

        written = asyncio.run(ghsub_raw_feed.export_raw_feed(SLOT))

        self.assertEqual(written, 1)
        self.assertEqual(self._outbox_files(), ["unagi:raw_feed:unagi-1:2026-W42.json"])
        with open(os.path.join(self.outbox, self._outbox_files()[0]), encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["recipient_email"], "one@example.com")
        self.assertEqual([it["post_id"] for it in data["items"]], [10])
        self.ingest_mock.assert_awaited_once()
        self.assertEqual(self.ingest_mock.await_args.args[0], ["tech_news"])

    def test_paused_channels_are_not_exported(self):
        sub_id = database.add_feed_subscription(1, "paused_chan", "Paused")
        database.set_feed_paused(1, sub_id, True)
        self._set_email(1, "one@example.com", "feed")
        self._store_posts("paused_chan", [{"id": 1, "time": SLOT - timedelta(days=1), "text": "Пост канала на паузе."}])

        asyncio.run(ghsub_raw_feed.export_raw_feed(SLOT))

        with open(os.path.join(self.outbox, "unagi:raw_feed:unagi-1:2026-W42.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["items"], [])

    def test_metrics_are_refreshed_once_per_channel(self):
        database.add_feed_subscription(1, "tech_news", "Tech News")
        database.add_feed_subscription(2, "tech_news", "Tech News")
        self._set_email(1, "one@example.com", "feed")
        self._set_email(2, "two@example.com", "feed")
        self._store_posts("tech_news", [{"id": 5, "time": SLOT - timedelta(days=1), "text": "Пост с метриками для проверки.", "views": 1}])
        source = mock.Mock()
        source.refresh_metrics = mock.AsyncMock(return_value=0)

        written = asyncio.run(ghsub_raw_feed.export_raw_feed(SLOT, source))

        self.assertEqual(written, 2)
        source.refresh_metrics.assert_awaited_once_with("tech_news", [5], period="week")

    def test_atomic_write_leaves_no_tmp(self):
        database.add_feed_subscription(1, "tech_news", "Tech News")
        self._set_email(1, "one@example.com", "feed")
        asyncio.run(ghsub_raw_feed.export_raw_feed(SLOT))
        self.assertEqual([f for f in self._outbox_files() if ".tmp" in f], [])

    def test_schedule_once_per_week_before_ghsub_deadline(self):
        database.add_feed_subscription(1, "tech_news", "Tech News")
        self._set_email(1, "one@example.com", "feed")
        saturday = datetime(2026, 10, 17, 23, 0, tzinfo=MSK)
        sunday_early = datetime(2026, 10, 18, 5, 29, tzinfo=MSK)
        sunday_slot = datetime(2026, 10, 18, 5, 31, tzinfo=MSK)

        self.assertEqual(asyncio.run(run_raw_feed_export_once(now=saturday)), "not_due")
        self.assertEqual(asyncio.run(run_raw_feed_export_once(now=sunday_early)), "not_due")
        self.assertEqual(self._outbox_files(), [])
        self.assertEqual(asyncio.run(run_raw_feed_export_once(now=sunday_slot)), "exported")
        self.assertEqual(self._outbox_files(), ["unagi:raw_feed:unagi-1:2026-W42.json"])
        self.assertEqual(asyncio.run(run_raw_feed_export_once(now=sunday_slot + timedelta(minutes=5))), "done")
        # Следующая неделя — новая выгрузка с другим edition_id.
        self.assertEqual(asyncio.run(run_raw_feed_export_once(now=sunday_slot + timedelta(days=7))), "exported")
        self.assertIn("unagi:raw_feed:unagi-1:2026-W43.json", self._outbox_files())

    def test_export_slot_is_before_ghsub_deadline(self):
        slot = export_slot(datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc))
        self.assertEqual(slot, SLOT)
        deadline = datetime(2026, 10, 18, 6, 15, tzinfo=MSK)
        self.assertLessEqual(slot + timedelta(seconds=ghsub_raw_feed.CHECK_INTERVAL_SECONDS), deadline)

    def test_late_export_is_skipped_after_last_ghsub_retry(self):
        database.add_feed_subscription(1, "tech_news", "Tech News")
        self._set_email(1, "one@example.com", "feed")
        late = datetime(2026, 10, 18, 8, 0, tzinfo=MSK)
        self.assertEqual(asyncio.run(run_raw_feed_export_once(now=late)), "missed")
        self.assertEqual(self._outbox_files(), [])
        self.assertEqual(asyncio.run(run_raw_feed_export_once(now=late + timedelta(hours=1))), "done")

    def test_failed_write_is_retried_on_next_check(self):
        database.add_feed_subscription(1, "tech_news", "Tech News")
        self._set_email(1, "one@example.com", "feed")
        now = datetime(2026, 10, 18, 5, 31, tzinfo=MSK)
        with mock.patch.object(ghsub_raw_feed, "write_raw_feed_edition", side_effect=OSError("Disk full")):
            with self.assertRaises(OSError):
                asyncio.run(run_raw_feed_export_once(now=now))
        self.assertEqual(asyncio.run(run_raw_feed_export_once(now=now + timedelta(minutes=5))), "exported")

    def test_list_feed_email_recipients(self):
        self._set_email(1, "one@example.com", "daily,feed")
        self._set_email(2, "two@example.com", "weekly")
        self._set_email(3, None, "feed")
        self.assertEqual(database.list_feed_email_recipients(), [(1, "one@example.com")])


class GhsubWeeklyDigestWindowTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = os.path.join(self.temp_dir.name, "test_weekly_pin.db")
        database.init_db()
        database.upsert_digest_settings(5, "weekly", send_hour=9, send_minute=30, weekday=2)
        with database.get_connection() as conn:
            conn.execute("INSERT INTO user_emails (user_id, email, email_periods) VALUES (5, 'u@example.com', 'weekly')")
            conn.commit()

    def tearDown(self):
        reset_settings()
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def _with_delivery(self, delivery: str):
        patcher = mock.patch.dict(os.environ, {"EMAIL_DELIVERY": delivery})
        patcher.start()
        self.addCleanup(patcher.stop)
        reset_settings()

    def test_weekly_email_digest_lands_before_ghsub_deadline(self):
        self._with_delivery("ghsub")
        resolved = digest_settings.resolve_digest_settings(5, "weekly")
        now = datetime(2026, 10, 14, 12, 0, tzinfo=MSK)
        send_at = digest_settings.get_next_digest_time("weekly", now, resolved)
        self.assertEqual(send_at, datetime(2026, 10, 18, 5, 0, tzinfo=MSK))
        # Худший случай: цикл дайджестов проснётся через полный интервал проверки.
        worst = send_at + timedelta(seconds=main.DIGEST_CHECK_INTERVAL_SECONDS)
        self.assertLess(worst, datetime(2026, 10, 18, 6, 15, tzinfo=MSK))

    def test_user_schedule_kept_without_ghsub_or_for_other_periods(self):
        self._with_delivery("unagi")
        resolved = digest_settings.resolve_digest_settings(5, "weekly")
        self.assertEqual((resolved["weekday"], resolved["send_hour"], resolved["send_minute"]), (2, 9, 30))
        self._with_delivery("ghsub")
        self.assertEqual(digest_settings.resolve_user_digest_settings(5, "weekly")["send_hour"], 9)
        self.assertEqual(digest_settings.resolve_digest_settings(5, "daily")["send_hour"], 7)

    def test_toggle_reschedules_weekly_subscriptions(self):
        self._with_delivery("ghsub")
        sub_id = database.add_subscription(5, "tech_news", "Tech", "weekly", "2026-10-10 00:00:00", "2026-10-21 06:30:00")
        digest_settings.sync_weekly_email_schedule(5)
        next_send = database.get_subscription_by_id(5, sub_id)
        next_local = database.parse_db_datetime(next_send["next_send_at"]).astimezone(MSK)
        self.assertEqual((next_local.weekday(), next_local.hour, next_local.minute), (6, 5, 0))

    def test_telegram_feed_uses_user_schedule(self):
        self.assertIs(ghsub_raw_feed.feed._resolve_digest_settings_fn, digest_settings.resolve_user_digest_settings)


class IngestFlagsTests(unittest.TestCase):
    def test_mtproto_media_kinds_and_hidden_links(self):
        MessageMediaPoll = type("MessageMediaPoll", (), {})
        MessageMediaPhoto = type("MessageMediaPhoto", (), {})
        MessageMediaWebPage = type("MessageMediaWebPage", (), {})
        webpage = MessageMediaWebPage()
        webpage.webpage = mock.Mock(url="https://preview.example.com/a")
        text_url = mock.Mock(url="https://hidden.example.com/b")
        plain_entity = mock.Mock(spec=[])

        self.assertEqual(_describe_media_and_links(mock.Mock(media=None, entities=None)), ("", []))
        self.assertEqual(_describe_media_and_links(mock.Mock(media=MessageMediaPoll(), entities=[]))[0], "poll")
        self.assertEqual(_describe_media_and_links(mock.Mock(media=MessageMediaPhoto(), entities=[]))[0], "media")
        self.assertEqual(
            _describe_media_and_links(mock.Mock(media=webpage, entities=[text_url, plain_entity])),
            ("", ["https://hidden.example.com/b", "https://preview.example.com/a"]),
        )

    def test_web_scraper_flags(self):
        html_page = """
        <div class="tgme_widget_message">
          <a class="tgme_widget_message_date" href="https://t.me/chan/1"><time class="time" datetime="2026-10-17T10:00:00+00:00"></time></a>
          <a class="tgme_widget_message_photo_wrap"></a>
          <div class="tgme_widget_message_text">Подпись <a href="https://ex.com/x?utm_source=a">тут</a></div>
        </div>
        <div class="tgme_widget_message">
          <div class="tgme_widget_message_forwarded_from">Forwarded from X</div>
          <a class="tgme_widget_message_date" href="https://t.me/chan/2"><time class="time" datetime="2026-10-17T11:00:00+00:00"></time></a>
          <div class="tgme_widget_message_text">Репост</div>
        </div>
        """
        posts, _, _ = _parse_channel_html_page(html_page, "chan", datetime(2026, 10, 1, tzinfo=timezone.utc))
        by_id = {p["id"]: p for p in posts}
        self.assertEqual((by_id[1]["media_kind"], by_id[1]["is_forward"]), ("media", False))
        self.assertEqual(by_id[1]["link_urls"], ["https://ex.com/x?utm_source=a"])
        self.assertEqual((by_id[2]["media_kind"], by_id[2]["is_forward"]), ("", True))


class EmailMenuFeedToggleTests(unittest.TestCase):
    def tearDown(self):
        reset_settings()

    def test_feed_toggle_shown_only_with_ghsub(self):
        for delivery, expected in (("ghsub", True), ("unagi", False)):
            with mock.patch.dict(os.environ, {"EMAIL_DELIVERY": delivery}), \
                 mock.patch.object(main.email_digest, "is_enabled", return_value=True), \
                 mock.patch.object(main, "get_email_delivery", return_value=("u@example.com", {"feed"})):
                reset_settings()
                _, markup = main.build_email_menu(5)
                data = [b.callback_data for row in markup.inline_keyboard for b in row]
                self.assertEqual("email_feed_toggle" in data, expected, delivery)


if __name__ == "__main__":
    unittest.main()
