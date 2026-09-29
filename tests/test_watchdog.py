import os

os.environ.setdefault("BOT_TOKEN", "123456:TEST_TOKEN_FOR_UNIT_TESTS")

import asyncio  # noqa: E402
import tempfile  # noqa: E402
import unittest  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402
from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

from aiogram.exceptions import TelegramAPIError  # noqa: E402

import data.database as db  # noqa: E402
from watchdog import (  # noqa: E402
    check_watchdog,
    format_backlog_growth_alert,
    format_stall_alert,
    get_admin_chat_ids,
    is_backlog_strictly_growing,
)


class TestWatchdog(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp_dir.name, "test_watchdog.db")
        self.orig_db_path = db.DB_PATH
        db.DB_PATH = self.db_path
        db.init_db()

    def tearDown(self):
        db.DB_PATH = self.orig_db_path
        self.tmp_dir.cleanup()

    def test_record_digest_cycle_heartbeat(self):
        t1 = datetime(2026, 9, 29, 12, 0, 0, tzinfo=timezone.utc)
        db.record_digest_cycle_heartbeat(backlog=3, now=t1)
        self.assertEqual(db.get_last_digest_cycle_at(), db.serialize_datetime(t1))
        self.assertEqual(db.get_digest_cycle_backlog_history(), [3])

        t2 = datetime(2026, 9, 29, 12, 5, 0, tzinfo=timezone.utc)
        db.record_digest_cycle_heartbeat(backlog=7, now=t2)
        self.assertEqual(db.get_last_digest_cycle_at(), db.serialize_datetime(t2))
        self.assertEqual(db.get_digest_cycle_backlog_history(), [3, 7])

        # Test sliding window (max 10 entries)
        for i in range(15):
            db.record_digest_cycle_heartbeat(backlog=i)
        hist = db.get_digest_cycle_backlog_history()
        self.assertEqual(len(hist), 10)
        self.assertEqual(hist[-1], 14)

    def test_get_admin_chat_ids(self):
        with patch.dict(os.environ, {"ADMIN_USER_ID": "12345, 67890", "ADMIN_CHAT_ID": ""}):
            self.assertEqual(get_admin_chat_ids(), [12345, 67890])

        with patch.dict(os.environ, {"ADMIN_USER_ID": "", "ADMIN_CHAT_ID": "999"}):
            self.assertEqual(get_admin_chat_ids(), [999])

        with patch.dict(os.environ, {"ADMIN_USER_ID": "invalid, 100", "ADMIN_CHAT_ID": ""}):
            self.assertEqual(get_admin_chat_ids(), [100])

        with patch.dict(os.environ, {"ADMIN_USER_ID": "", "ADMIN_CHAT_ID": ""}):
            self.assertEqual(get_admin_chat_ids(), [])

    def test_is_backlog_strictly_growing(self):
        # Needs at least required_cycles
        self.assertFalse(is_backlog_strictly_growing([1, 2], required_cycles=3))

        # Flat / zero
        self.assertFalse(is_backlog_strictly_growing([0, 0, 0], required_cycles=3))
        self.assertFalse(is_backlog_strictly_growing([5, 5, 5], required_cycles=3))

        # Decreasing / fluctuating
        self.assertFalse(is_backlog_strictly_growing([5, 3, 7], required_cycles=3))
        self.assertFalse(is_backlog_strictly_growing([5, 8, 4], required_cycles=3))

        # Strictly growing
        self.assertTrue(is_backlog_strictly_growing([2, 5, 8], required_cycles=3))
        self.assertTrue(is_backlog_strictly_growing([1, 2, 5, 8], required_cycles=3))

    def test_format_alerts(self):
        stall_text = format_stall_alert("2026-09-29 10:00:00", 7500, stall_threshold_seconds=7200)
        self.assertIn("простой цикла дайджестов", stall_text)
        self.assertIn("125 мин.", stall_text)
        self.assertIn("29.09.2026 10:00 UTC", stall_text)

        growth_text = format_backlog_growth_alert([3, 8, 14], 3)
        self.assertIn("рост очереди backlog", growth_text)
        self.assertIn("3 → 8 → 14", growth_text)
        self.assertIn("3 циклов подряд", growth_text)

    def test_watchdog_detects_stall_and_deduplicates(self):
        admin_id = 9999
        with patch.dict(os.environ, {"ADMIN_USER_ID": str(admin_id)}):
            # Record cycle 3 hours ago
            start_time = datetime(2026, 9, 29, 10, 0, 0, tzinfo=timezone.utc)
            db.record_digest_cycle_heartbeat(backlog=0, now=start_time)

            now = start_time + timedelta(hours=3)

            bot = MagicMock()
            bot.send_message = AsyncMock()

            # First run: stall detected, alert sent to admin
            alerts = asyncio.run(check_watchdog(bot, now=now, stall_seconds=7200))
            self.assertIn("stall", alerts)
            bot.send_message.assert_called_once()
            call_kwargs = bot.send_message.call_args.kwargs
            self.assertEqual(call_kwargs["chat_id"], admin_id)
            self.assertIn("простой цикла", call_kwargs["text"])

            # Second run: stall persists, but alert is NOT duplicated
            bot.send_message.reset_mock()
            alerts2 = asyncio.run(check_watchdog(bot, now=now + timedelta(minutes=5), stall_seconds=7200))
            self.assertEqual(alerts2, [])
            bot.send_message.assert_not_called()

            # Cycle recovers: records new heartbeat
            recovery_time = now + timedelta(minutes=10)
            db.record_digest_cycle_heartbeat(backlog=0, now=recovery_time)

            # Check again right after recovery: no stall alert
            alerts3 = asyncio.run(check_watchdog(bot, now=recovery_time + timedelta(minutes=1), stall_seconds=7200))
            self.assertEqual(alerts3, [])

    def test_watchdog_detects_backlog_growth_and_clears_on_decrease(self):
        admin_id = 9999
        with patch.dict(os.environ, {"ADMIN_USER_ID": str(admin_id)}):
            t0 = datetime(2026, 9, 29, 10, 0, 0, tzinfo=timezone.utc)
            # Cycles 1, 2, 3 with growing backlog: 5 -> 10 -> 15
            db.record_digest_cycle_heartbeat(backlog=5, now=t0)
            db.record_digest_cycle_heartbeat(backlog=10, now=t0 + timedelta(minutes=1))
            db.record_digest_cycle_heartbeat(backlog=15, now=t0 + timedelta(minutes=2))

            bot = MagicMock()
            bot.send_message = AsyncMock()

            # Growth detected
            alerts = asyncio.run(check_watchdog(bot, now=t0 + timedelta(minutes=3), growth_cycles=3, stall_seconds=100000))
            self.assertIn("backlog_growth", alerts)
            bot.send_message.assert_called_once()
            self.assertIn("5 → 10 → 15", bot.send_message.call_args.kwargs["text"])

            # Second check: deduplicated, no repeated message
            bot.send_message.reset_mock()
            alerts2 = asyncio.run(check_watchdog(bot, now=t0 + timedelta(minutes=4), growth_cycles=3, stall_seconds=100000))
            self.assertEqual(alerts2, [])
            bot.send_message.assert_not_called()

            # Next cycle decreases backlog: 15 -> 8
            db.record_digest_cycle_heartbeat(backlog=8, now=t0 + timedelta(minutes=5))

            # Not strictly growing now: no alert
            alerts3 = asyncio.run(check_watchdog(bot, now=t0 + timedelta(minutes=6), growth_cycles=3, stall_seconds=100000))
            self.assertEqual(alerts3, [])

    def test_watchdog_does_not_alert_regular_users(self):
        admin_id = 9999
        user_id = 1111
        # Regular user subscription exists
        db.add_subscription(user_id, "@channel", "Title", "daily", "2026-09-28 08:00:00", "2026-09-29 08:00:00")

        with patch.dict(os.environ, {"ADMIN_USER_ID": str(admin_id)}):
            t0 = datetime(2026, 9, 29, 8, 0, 0, tzinfo=timezone.utc)
            db.record_digest_cycle_heartbeat(backlog=0, now=t0)

            bot = MagicMock()
            bot.send_message = AsyncMock()

            # Run check 3 hours later
            asyncio.run(check_watchdog(bot, now=t0 + timedelta(hours=3), stall_seconds=7200))

            # Bot only sent to admin_id, never to user_id
            for call in bot.send_message.call_args_list:
                self.assertEqual(call.kwargs["chat_id"], admin_id)
                self.assertNotEqual(call.kwargs["chat_id"], user_id)

    def test_watchdog_handles_telegram_errors_gracefully(self):
        admin_id = 9999
        with patch.dict(os.environ, {"ADMIN_USER_ID": str(admin_id)}):
            t0 = datetime(2026, 9, 29, 8, 0, 0, tzinfo=timezone.utc)
            db.record_digest_cycle_heartbeat(backlog=0, now=t0)

            bot = MagicMock()
            bot.send_message = AsyncMock(side_effect=TelegramAPIError(MagicMock(), "Network issue"))

            # Must not crash
            alerts = asyncio.run(check_watchdog(bot, now=t0 + timedelta(hours=3), stall_seconds=7200))
            self.assertIn("stall", alerts)


if __name__ == "__main__":
    unittest.main()
