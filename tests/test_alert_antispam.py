import os

os.environ.setdefault("BOT_TOKEN", "123456:TEST_TOKEN_FOR_UNIT_TESTS")

import asyncio  # noqa: E402
import tempfile  # noqa: E402
import unittest  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402
from unittest.mock import AsyncMock, MagicMock  # noqa: E402

from aiogram.exceptions import TelegramForbiddenError  # noqa: E402

import data.database as db  # noqa: E402
from digest_alerts import (  # noqa: E402
    calculate_alert_cooldown,
    is_alert_allowed_by_cooldown,
    send_digest_failure_alert,
)


class TestAlertAntiSpam(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp_dir.name, "test_antispam.db")
        self.orig_db_path = db.DB_PATH
        db.DB_PATH = self.db_path
        db.init_db()

    def tearDown(self):
        db.DB_PATH = self.orig_db_path
        self.tmp_dir.cleanup()

    def test_calculate_alert_cooldown_exponential(self):
        base = 3600  # 1 hour
        max_cd = 14400  # 4 hours

        # 0 failures -> 0
        self.assertEqual(calculate_alert_cooldown(0, base_cooldown=base, max_cooldown=max_cd), 0)
        # 1 failure -> base (3600)
        self.assertEqual(calculate_alert_cooldown(1, base_cooldown=base, max_cooldown=max_cd), 3600)
        # 2 failures -> base * 2 (7200)
        self.assertEqual(calculate_alert_cooldown(2, base_cooldown=base, max_cooldown=max_cd), 7200)
        # 3 failures -> base * 4 (14400, cap reached)
        self.assertEqual(calculate_alert_cooldown(3, base_cooldown=base, max_cooldown=max_cd), 14400)
        # 4 failures -> capped at 14400
        self.assertEqual(calculate_alert_cooldown(4, base_cooldown=base, max_cooldown=max_cd), 14400)

    def test_is_alert_allowed_by_cooldown(self):
        user_id = 111
        base = 1800  # 30 mins
        max_cd = 7200

        # No alerts yet -> allowed
        now = datetime(2026, 9, 29, 10, 0, 0, tzinfo=timezone.utc)
        self.assertTrue(is_alert_allowed_by_cooldown(user_id, now=now, base_cooldown=base, max_cooldown=max_cd))

        # Alert sent at 10:00 (consecutive=1)
        db.record_user_alert_dispatched(user_id, db.serialize_datetime(now))

        # Check at 10:15 (15 mins later, < 30 mins cooldown) -> Denied
        now_15m = now + timedelta(minutes=15)
        self.assertFalse(is_alert_allowed_by_cooldown(user_id, now=now_15m, base_cooldown=base, max_cooldown=max_cd))

        # Check at 10:31 (31 mins later, > 30 mins cooldown) -> Allowed
        now_31m = now + timedelta(minutes=31)
        self.assertTrue(is_alert_allowed_by_cooldown(user_id, now=now_31m, base_cooldown=base, max_cooldown=max_cd))

    def test_send_digest_failure_alert_suppresses_within_cooldown(self):
        user_id = 222
        period = "daily"
        t0 = datetime(2026, 9, 29, 8, 0, 0, tzinfo=timezone.utc)
        sched_a = db.serialize_datetime(t0)

        bot_mock = AsyncMock()
        msg_mock = MagicMock()
        msg_mock.message_id = 555
        bot_mock.send_message.return_value = msg_mock

        # First failure alert (Edition A)
        exec_a = {
            "id": 1,
            "user_id": user_id,
            "period": period,
            "scheduled_at": sched_a,
            "channels_count": 2,
            "channels_failed": 2,
            "status": "failed",
            "retry_count": 5,
        }
        sent_a = asyncio.run(
            send_digest_failure_alert(bot_mock, exec_a, now=t0, base_cooldown=3600, max_cooldown=14400)
        )
        self.assertTrue(sent_a)
        self.assertEqual(bot_mock.send_message.call_count, 1)

        # Second failure alert (Edition B) 30 minutes later (within 1h cooldown)
        t_30m = t0 + timedelta(minutes=30)
        sched_b = db.serialize_datetime(t_30m)
        exec_b = {
            "id": 2,
            "user_id": user_id,
            "period": period,
            "scheduled_at": sched_b,
            "channels_count": 2,
            "channels_failed": 2,
            "status": "failed",
            "retry_count": 5,
        }
        sent_b = asyncio.run(
            send_digest_failure_alert(bot_mock, exec_b, now=t_30m, base_cooldown=3600, max_cooldown=14400)
        )
        # Suppressed by cooldown!
        self.assertFalse(sent_b)
        self.assertEqual(bot_mock.send_message.call_count, 1)

        # Check at 70 mins (cooldown elapsed) -> Allowed
        t_70m = t0 + timedelta(minutes=70)
        sent_b_later = asyncio.run(
            send_digest_failure_alert(bot_mock, exec_b, now=t_70m, base_cooldown=3600, max_cooldown=14400)
        )
        self.assertTrue(sent_b_later)
        self.assertEqual(bot_mock.send_message.call_count, 2)

    def test_send_digest_failure_alert_updates_existing_message(self):
        user_id = 333
        period = "daily"
        t0 = datetime(2026, 9, 29, 8, 0, 0, tzinfo=timezone.utc)
        sched_str = db.serialize_datetime(t0)

        bot_mock = AsyncMock()
        msg_mock = MagicMock()
        msg_mock.message_id = 777
        bot_mock.send_message.return_value = msg_mock
        bot_mock.edit_message_text = AsyncMock()

        exec_dict = {
            "id": 10,
            "user_id": user_id,
            "period": period,
            "scheduled_at": sched_str,
            "channels_count": 3,
            "channels_failed": 2,
            "status": "failed",
            "retry_count": 5,
            "error_message": "Network issue 1",
        }

        # 1. Send first alert
        sent1 = asyncio.run(send_digest_failure_alert(bot_mock, exec_dict, now=t0))
        self.assertTrue(sent1)
        self.assertEqual(bot_mock.send_message.call_count, 1)
        self.assertEqual(bot_mock.edit_message_text.call_count, 0)

        # 2. Update with new error message for the SAME edition
        exec_dict["channels_failed"] = 3
        exec_dict["error_message"] = "All channels failed"
        t_later = t0 + timedelta(minutes=10)
        sent2 = asyncio.run(send_digest_failure_alert(bot_mock, exec_dict, now=t_later))
        self.assertTrue(sent2)
        # Did not send a second message, edited the existing one!
        self.assertEqual(bot_mock.send_message.call_count, 1)
        self.assertEqual(bot_mock.edit_message_text.call_count, 1)

    def test_edit_message_handles_forbidden_error(self):
        user_id = 444
        period = "daily"
        t0 = datetime(2026, 9, 29, 8, 0, 0, tzinfo=timezone.utc)
        sched_str = db.serialize_datetime(t0)

        bot_mock = AsyncMock()
        msg_mock = MagicMock()
        msg_mock.message_id = 888
        bot_mock.send_message.return_value = msg_mock
        bot_mock.edit_message_text.side_effect = TelegramForbiddenError(
            method=MagicMock(),
            message="Forbidden: bot was blocked by the user",
        )

        exec_dict = {
            "id": 15,
            "user_id": user_id,
            "period": period,
            "scheduled_at": sched_str,
            "channels_count": 2,
            "channels_failed": 2,
            "status": "failed",
            "retry_count": 5,
        }

        # First alert sends message
        sent1 = asyncio.run(send_digest_failure_alert(bot_mock, exec_dict, now=t0))
        self.assertTrue(sent1)

        # Change error so edit is attempted, triggering TelegramForbiddenError
        exec_dict["error_message"] = "Critical failure"
        sent2 = asyncio.run(send_digest_failure_alert(bot_mock, exec_dict, now=t0 + timedelta(minutes=5)))
        self.assertFalse(sent2)
        # User alerts must be disabled!
        settings = db.get_user_alert_settings(user_id)
        self.assertFalse(settings["alerts_enabled"])

    def test_reset_consecutive_failures_on_delivery(self):
        user_id = 555
        now = datetime(2026, 9, 29, 12, 0, 0, tzinfo=timezone.utc)
        now_str = db.serialize_datetime(now)

        # Dispatch 3 alerts
        db.record_user_alert_dispatched(user_id, now_str)
        db.record_user_alert_dispatched(user_id, now_str)
        db.record_user_alert_dispatched(user_id, now_str)

        settings = db.get_user_alert_settings(user_id)
        self.assertEqual(settings["consecutive_failures"], 3)

        # Reset
        db.reset_user_alert_consecutive_failures(user_id)
        settings_after = db.get_user_alert_settings(user_id)
        self.assertEqual(settings_after["consecutive_failures"], 0)


if __name__ == "__main__":
    unittest.main()
