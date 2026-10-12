"""One digest edition per day: daily + weekly/monthly on the same day go out together
at the senior period's time, juniors first as a separate group."""
import asyncio
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

os.environ.setdefault("BOT_TOKEN", "123456:TEST_TOKEN_FOR_UNIT_TESTS")

import digest_edition  # noqa: E402
import main  # noqa: E402
from data import database  # noqa: E402
from time_utils import TZ  # noqa: E402

USER = 4242


def next_saturday(hour: int, minute: int = 0) -> datetime:
    """A Saturday in the near future (no hard-coded dates: the code compares with «now»)."""
    today = datetime.now(TZ).replace(hour=hour, minute=minute, second=0, microsecond=0)
    return today + timedelta(days=((5 - today.weekday()) % 7) or 7)


def row(sub_id, period, next_send_at: datetime):
    return (sub_id, USER, f"ch{sub_id}", f"Канал {sub_id}", period, None, 0, None,
            database.serialize_datetime(next_send_at))


class PlanDailyEditionTests(unittest.TestCase):
    def test_senior_later_today_defers_due_juniors(self):
        sat = next_saturday(7, 5)
        subs = [row(1, "daily", sat.replace(minute=0)), row(2, "weekly", sat.replace(hour=9, minute=0))]
        defer_until, due = digest_edition.plan_daily_edition(subs, sat)
        self.assertEqual(defer_until, sat.replace(hour=9, minute=0))
        self.assertEqual([s[0] for s in due], [1])

    def test_senior_due_pulls_later_juniors_in(self):
        sat = next_saturday(7, 5)
        subs = [row(2, "weekly", sat.replace(minute=0)), row(1, "daily", sat.replace(hour=9, minute=0))]
        defer_until, edition = digest_edition.plan_daily_edition(subs, sat)
        self.assertIsNone(defer_until)
        self.assertEqual({s[0] for s in edition}, {1, 2})

    def test_monthly_is_senior_to_weekly(self):
        sat = next_saturday(7, 5)
        subs = [
            row(1, "daily", sat.replace(minute=0)),
            row(2, "weekly", sat.replace(minute=0)),
            row(3, "monthly", sat.replace(hour=10, minute=0)),
        ]
        defer_until, due = digest_edition.plan_daily_edition(subs, sat)
        self.assertEqual(defer_until, sat.replace(hour=10, minute=0))
        self.assertEqual({s[0] for s in due}, {1, 2})

    def test_next_send_base_never_before_own_slot(self):
        sat = next_saturday(7, 5)
        later = database.serialize_datetime(sat.replace(hour=9, minute=0))
        self.assertEqual(digest_edition.next_send_base(sat, later), sat.replace(hour=9, minute=0))
        earlier = database.serialize_datetime(sat.replace(minute=0))
        self.assertEqual(digest_edition.next_send_base(sat, earlier), sat)

    def test_arrange_groups_only_mixed_editions(self):
        weekly = {"period": "weekly", "title": "W", "posts": [{}]}
        daily = {"period": "daily", "title": "D", "posts": [{}]}
        mixed = digest_edition.arrange_edition_sections([weekly, daily])
        self.assertEqual([s["title"] for s in mixed], ["D", "W"])
        self.assertEqual([s["group"] for s in mixed], ["☀️ За сутки", "🗓 За неделю"])
        single = digest_edition.arrange_edition_sections([daily, {**daily, "group": "x"}])
        self.assertTrue(all("group" not in s for s in single))

    def test_dedupe_keeps_post_in_first_group(self):
        post = {"link": "https://t.me/a/1", "text": "x"}
        sections = [
            {"period": "daily", "title": "A", "posts": [post]},
            {"period": "weekly", "title": "A", "posts": [post, {"link": "https://t.me/a/2"}]},
            {"period": "weekly", "title": "B", "posts": [post]},
        ]
        result = digest_edition.dedupe_edition_sections(sections)
        self.assertEqual([(s["title"], len(s["posts"])) for s in result], [("A", 1), ("A", 1)])
        self.assertEqual(result[1]["posts"][0]["link"], "https://t.me/a/2")

    def test_titles(self):
        sat = next_saturday(7, 5)
        self.assertEqual(digest_edition.edition_title([{"period": "daily"}], sat), "Твоя утренняя газета")
        self.assertEqual(digest_edition.edition_title([{"period": "daily"}], sat.replace(hour=20)), "Твоя вечерняя газета")
        self.assertEqual(digest_edition.edition_title([{"period": "daily"}, {"period": "weekly"}], sat), "Твоя газета за неделю")
        self.assertEqual(digest_edition.edition_title([{"period": "monthly"}, {"period": "weekly"}], sat), "Твоя газета за месяц")

    def test_local_day_end(self):
        sat = next_saturday(7, 5)
        end = database.parse_db_datetime(digest_edition.local_day_end(sat)).astimezone(TZ)
        self.assertEqual((end.date(), end.hour, end.minute, end.second), (sat.date(), 23, 59, 59))


class GroupRenderingTests(unittest.TestCase):
    SECTIONS = digest_edition.arrange_edition_sections([
        {"sub_id": 2, "period": "weekly", "title": "W", "posts": [{"text": "w", "link": "https://t.me/w/1"}]},
        {"sub_id": 1, "period": "daily", "title": "D", "posts": [{"text": "d", "link": "https://t.me/d/1"}]},
    ])

    def test_telegram_lines_have_group_headers_in_order(self):
        text = "\n".join(main.render_digest_lines("Газета", self.SECTIONS))
        self.assertLess(text.index("☀️ За сутки"), text.index("<b>D</b>"))
        self.assertLess(text.index("<b>D</b>"), text.index("🗓 За неделю"))
        self.assertLess(text.index("🗓 За неделю"), text.index("<b>W</b>"))

    def test_telegraph_and_email_have_group_headers(self):
        import email_digest
        from telegraph_publisher import build_content

        nodes = build_content(self.SECTIONS)
        headers = [n["children"][0] for n in nodes if isinstance(n, dict) and n.get("tag") == "h3"]
        self.assertEqual(headers, ["☀️ За сутки", "🗓 За неделю"])
        html_body, text_body = email_digest.build_digest_email("Газета", self.SECTIONS)
        self.assertEqual(html_body.count("<h3"), 2)
        self.assertIn("##### ☀️ За сутки #####", text_body)


class DigestCycleEditionTests(unittest.TestCase):
    """run_digest_cycle end to end: real SQLite, mocked channel source and Telegram delivery."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = os.path.join(self.temp_dir.name, "test.db")
        database.init_db()
        self.channel_posts: dict[str, list[dict]] = {}

    def tearDown(self):
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def subscribe(self, channel, period, next_send_at: datetime) -> int:
        last_scraped = database.serialize_datetime(next_send_at - timedelta(days=8))
        sub_id = database.add_subscription(
            USER, channel, f"Канал {channel}", period, last_scraped, database.serialize_datetime(next_send_at)
        )
        post_time = next_send_at - timedelta(hours=3)
        self.channel_posts.setdefault(channel, [
            {"id": 100 + len(self.channel_posts), "time": post_time, "text": f"Пост из {channel}. Подробности.",
             "link": f"https://t.me/{channel}/{100 + len(self.channel_posts)}"}
        ])
        return sub_id

    def run_cycle(self, now: datetime):
        async def fetch(username, marker, web_session=None):
            return list(self.channel_posts.get(username, [])), "web"

        telegram = mock.AsyncMock()
        with mock.patch.object(main, "utc_now", return_value=now.astimezone(database.UTC)), \
                mock.patch.object(main, "local_now", return_value=now), \
                mock.patch.object(main.channel_source, "fetch", side_effect=fetch), \
                mock.patch.object(main, "get_email_delivery_periods", return_value=(None, set())), \
                mock.patch.object(main, "send_telegram_digest", telegram):
            asyncio.run(main.run_digest_cycle(mock.MagicMock(), asyncio.Semaphore(5)))
        return telegram

    def next_send(self, sub_id) -> datetime:
        return database.parse_db_datetime(database.get_subscription_by_id(USER, sub_id)["next_send_at"]).astimezone(TZ)

    def test_same_time_gives_one_edition_daily_first(self):
        sat = next_saturday(7)
        database.upsert_digest_settings(USER, "daily", send_hour=7, send_minute=0)
        database.upsert_digest_settings(USER, "weekly", send_hour=7, send_minute=0, weekday=5)
        daily = self.subscribe("dailych", "daily", sat)
        weekly = self.subscribe("weeklych", "weekly", sat)

        telegram = self.run_cycle(sat + timedelta(minutes=5))

        telegram.assert_awaited_once()
        _, title, sections, _ = telegram.call_args.args
        self.assertEqual(title, "Твоя газета за неделю")
        self.assertEqual([(s["title"], s.get("group")) for s in sections],
                         [("Канал dailych", "☀️ За сутки"), ("Канал weeklych", "🗓 За неделю")])
        self.assertEqual(self.next_send(daily), sat + timedelta(days=1))
        self.assertEqual(self.next_send(weekly), sat + timedelta(days=7))
        for period in ("daily", "weekly"):
            execution = database.get_digest_execution(USER, period, database.serialize_datetime(sat))
            self.assertEqual(execution["status"], "delivered")

    def test_daily_waits_for_later_weekly(self):
        sat = next_saturday(7)
        database.upsert_digest_settings(USER, "daily", send_hour=7, send_minute=0)
        database.upsert_digest_settings(USER, "weekly", send_hour=9, send_minute=0, weekday=5)
        daily = self.subscribe("dailych", "daily", sat)
        weekly = self.subscribe("weeklych", "weekly", sat.replace(hour=9))

        early = self.run_cycle(sat + timedelta(minutes=5))
        early.assert_not_awaited()
        self.assertEqual(self.next_send(daily), sat.replace(hour=9))

        late = self.run_cycle(sat.replace(hour=9, minute=5))
        late.assert_awaited_once()
        self.assertEqual([s["period"] for s in late.call_args.args[2]], ["daily", "weekly"])
        self.assertEqual(self.next_send(daily), sat + timedelta(days=1))
        self.assertEqual(self.next_send(weekly), sat.replace(hour=9) + timedelta(days=7))

    def test_earlier_weekly_pulls_in_later_daily_once(self):
        sat = next_saturday(7)
        database.upsert_digest_settings(USER, "daily", send_hour=9, send_minute=0)
        database.upsert_digest_settings(USER, "weekly", send_hour=7, send_minute=0, weekday=5)
        daily = self.subscribe("dailych", "daily", sat.replace(hour=9))
        self.subscribe("weeklych", "weekly", sat)

        telegram = self.run_cycle(sat + timedelta(minutes=5))
        telegram.assert_awaited_once()
        self.assertEqual([s["period"] for s in telegram.call_args.args[2]], ["daily", "weekly"])
        # Not again at 09:00 today: the next daily run is tomorrow.
        self.assertEqual(self.next_send(daily), sat.replace(hour=9) + timedelta(days=1))
        again = self.run_cycle(sat.replace(hour=9, minute=5))
        again.assert_not_awaited()

    def test_ordinary_day_daily_only(self):
        sat = next_saturday(7)
        database.upsert_digest_settings(USER, "daily", send_hour=7, send_minute=0)
        database.upsert_digest_settings(USER, "weekly", send_hour=7, send_minute=0, weekday=0)
        self.subscribe("dailych", "daily", sat)
        self.subscribe("weeklych", "weekly", sat + timedelta(days=2))

        telegram = self.run_cycle(sat + timedelta(minutes=5))
        telegram.assert_awaited_once()
        _, title, sections, _ = telegram.call_args.args
        self.assertEqual(title, "Твоя утренняя газета")
        self.assertEqual([s["period"] for s in sections], ["daily"])
        self.assertNotIn("group", sections[0])

    def test_channel_in_both_periods_is_not_repeated(self):
        sat = next_saturday(7)
        database.upsert_digest_settings(USER, "daily", send_hour=7, send_minute=0)
        database.upsert_digest_settings(USER, "weekly", send_hour=7, send_minute=0, weekday=5)
        self.subscribe("samech", "daily", sat)
        self.subscribe("samech", "weekly", sat)

        telegram = self.run_cycle(sat + timedelta(minutes=5))
        sections = telegram.call_args.args[2]
        links = [post["link"] for s in sections for post in s["posts"]]
        self.assertEqual(len(links), len(set(links)))
        self.assertEqual([s["period"] for s in sections], ["daily"])


if __name__ == "__main__":
    unittest.main()
