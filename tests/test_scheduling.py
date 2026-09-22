import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

# main.py builds the Bot at import time and refuses to start without a token.
os.environ.setdefault("BOT_TOKEN", "123456:TEST_TOKEN_FOR_UNIT_TESTS")

import main  # noqa: E402
from data import database  # noqa: E402

TZ = main.TZ


def at(year, month, day, hour=0, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=TZ)


def settings(**overrides):
    base = {"send_hour": 7, "send_minute": 0, "weekday": 0, "month_day": 1, "monthly_mode": "date"}
    base.update(overrides)
    return base


class NthWeekdayTests(unittest.TestCase):
    def test_first_and_last_monday(self):
        # September 2026: Mondays are 7, 14, 21, 28.
        self.assertEqual(main._nth_weekday_of_month(2026, 9, 0, 1), 7)
        self.assertEqual(main._nth_weekday_of_month(2026, 9, 0, 4), 28)

    def test_fifth_occurrence_clamps_to_last(self):
        self.assertEqual(main._nth_weekday_of_month(2026, 9, 0, 5), 28)

    def test_occurrence_below_one_clamps_to_first(self):
        self.assertEqual(main._nth_weekday_of_month(2026, 9, 0, 0), 7)


class NextDigestTimeTests(unittest.TestCase):
    def test_daily_later_today(self):
        now = at(2026, 9, 22, 6, 30)
        self.assertEqual(main.get_next_digest_time("daily", now, settings()), at(2026, 9, 22, 7))

    def test_daily_exact_time_rolls_to_tomorrow(self):
        now = at(2026, 9, 22, 7, 0)
        self.assertEqual(main.get_next_digest_time("daily", now, settings()), at(2026, 9, 23, 7))

    def test_daily_rolls_over_year_end(self):
        now = at(2026, 12, 31, 23, 0)
        self.assertEqual(main.get_next_digest_time("daily", now, settings()), at(2027, 1, 1, 7))

    def test_weekly_same_weekday_passed_rolls_a_week(self):
        now = at(2026, 9, 21, 8)  # Monday, after 07:00
        result = main.get_next_digest_time("weekly", now, settings(weekday=0))
        self.assertEqual(result, at(2026, 9, 28, 7))

    def test_weekly_later_this_week(self):
        now = at(2026, 9, 22, 8)  # Tuesday
        result = main.get_next_digest_time("weekly", now, settings(weekday=4, send_hour=18, send_minute=30))
        self.assertEqual(result, at(2026, 9, 25, 18, 30))

    def test_monthly_date_clamped_to_short_month(self):
        now = at(2026, 2, 10)
        result = main.get_next_digest_time("monthly", now, settings(month_day=31))
        self.assertEqual(result, at(2026, 2, 28, 7))

    def test_monthly_date_passed_moves_to_next_month_full_day(self):
        now = at(2026, 1, 31, 8)
        result = main.get_next_digest_time("monthly", now, settings(month_day=31))
        self.assertEqual(result, at(2026, 2, 28, 7))
        now = at(2026, 2, 28, 8)
        result = main.get_next_digest_time("monthly", now, settings(month_day=31))
        self.assertEqual(result, at(2026, 3, 31, 7))

    def test_monthly_date_december_rolls_to_january(self):
        now = at(2026, 12, 15)
        result = main.get_next_digest_time("monthly", now, settings(month_day=1))
        self.assertEqual(result, at(2027, 1, 1, 7))

    def test_monthly_nth_weekday(self):
        # month_day 8..14 means "2nd <weekday>"; 2nd Friday of Oct 2026 is the 9th.
        now = at(2026, 9, 30)
        result = main.get_next_digest_time(
            "monthly", now, settings(monthly_mode="weekday", weekday=4, month_day=10)
        )
        self.assertEqual(result, at(2026, 10, 9, 7))

    def test_monthly_fifth_weekday_uses_last_when_missing(self):
        # "5th Monday" in October 2026 does not exist; the last Monday is the 26th.
        now = at(2026, 10, 1)
        result = main.get_next_digest_time(
            "monthly", now, settings(monthly_mode="weekday", weekday=0, month_day=29)
        )
        self.assertEqual(result, at(2026, 10, 26, 7))

    def test_result_is_always_in_the_future(self):
        start = at(2026, 1, 1, 0, 0)
        cases = [
            ("daily", settings()),
            ("weekly", settings(weekday=3)),
            ("monthly", settings(month_day=31)),
            ("monthly", settings(monthly_mode="weekday", weekday=6, month_day=29)),
        ]
        for hours in range(0, 24 * 400, 7):
            now = start + timedelta(hours=hours)
            for period, cfg in cases:
                result = main.get_next_digest_time(period, now, cfg)
                self.assertGreater(result, now, (period, cfg, now))
                self.assertLessEqual(result - now, timedelta(days=62), (period, cfg, now))

    def test_unknown_period_raises(self):
        with self.assertRaises(ValueError):
            main.get_next_digest_time("yearly", at(2026, 9, 22), settings())


class ChannelUsernameTests(unittest.TestCase):
    def test_strips_at_and_lowercases(self):
        self.assertEqual(database.normalize_channel_username("@Durov"), "durov")
        self.assertEqual(database.normalize_channel_username("@@News"), "news")

    def test_non_string_returns_empty(self):
        self.assertEqual(database.normalize_channel_username(None), "")


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = os.path.join(self.temp_dir.name, "test.db")
        database.init_db()

    def tearDown(self):
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def test_removes_only_expired_rows(self):
        now = datetime(2026, 9, 22, 12, tzinfo=timezone.utc)
        old = now - timedelta(days=database.MESSAGE_RETENTION_DAYS + 1)
        recent = now - timedelta(days=1)
        with database.get_connection() as conn:
            for msg_id, created, is_sent in ((1, old, 1), (2, recent, 1), (3, old, 0)):
                conn.execute(
                    """
                    INSERT INTO scheduled_messages (user_id, message_id, send_at, created_at, is_sent, delivery_status)
                    VALUES (1, ?, ?, ?, ?, ?)
                    """,
                    (
                        msg_id,
                        database.serialize_datetime(created),
                        database.serialize_datetime(created),
                        is_sent,
                        "sent" if is_sent else "pending",
                    ),
                )
            conn.commit()
        database.upsert_channel_posts(
            "@Example",
            [
                {"id": 1, "time": now - timedelta(days=database.DIGEST_POST_RETENTION_DAYS + 1), "text": "old"},
                {"id": 2, "time": recent, "text": "new"},
            ],
            "web",
        )

        stats = database.cleanup_old_records(database.serialize_datetime(now))

        self.assertEqual(stats["scheduled_sent_deleted"], 1)
        self.assertEqual(stats["channel_posts_deleted"], 1)
        with database.get_connection() as conn:
            remaining = [row[0] for row in conn.execute("SELECT message_id FROM scheduled_messages ORDER BY message_id")]
            posts = [row[0] for row in conn.execute("SELECT post_id FROM channel_posts")]
        # The pending reminder must survive no matter how old it is.
        self.assertEqual(remaining, [2, 3])
        self.assertEqual(posts, [2])


if __name__ == "__main__":
    unittest.main()
