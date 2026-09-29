import asyncio
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock
from aiogram.exceptions import TelegramForbiddenError

import data.database as db
from digest_alerts import (
    format_missed_digest_alert,
    process_pending_digest_alerts,
    send_digest_failure_alert,
    should_send_digest_failure_alert,
)


class TestDigestAlerts(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp_dir.name, "test_alerts.db")
        self.orig_db_path = db.DB_PATH
        db.DB_PATH = self.db_path
        db.init_db()

    def tearDown(self):
        db.DB_PATH = self.orig_db_path
        self.tmp_dir.cleanup()

    def test_format_missed_digest_alert(self):
        text = format_missed_digest_alert(
            period="daily",
            scheduled_at="2026-09-29 09:00:00",
            channels_count=5,
            channels_failed=2,
            will_retry=False,
            error_message="Connection timeout",
        )
        self.assertIn("утренний дайджест", text)
        self.assertIn("29.09.2026 09:00 UTC", text)
        self.assertIn("2 из 5", text)
        self.assertIn("автоматические попытки исчерпаны", text)
        self.assertIn("Connection timeout", text)

        # Test will_retry=True
        text_retry = format_missed_digest_alert(
            period="weekly",
            scheduled_at="2026-09-29 09:00:00",
            channels_count=3,
            channels_failed=1,
            will_retry=True,
        )
        self.assertIn("еженедельный дайджест", text_retry)
        self.assertIn("бот предпримет ещё одну попытку", text_retry)

    def test_should_send_digest_failure_alert_empty_and_delivered(self):
        now = datetime(2026, 9, 29, 12, 0, 0, tzinfo=timezone.utc)
        exec_empty = {
            "user_id": 1,
            "period": "daily",
            "scheduled_at": "2026-09-29 09:00:00",
            "status": "empty",
            "retry_count": 0,
        }
        self.assertFalse(should_send_digest_failure_alert(exec_empty, now=now))

        exec_delivered = {
            "user_id": 1,
            "period": "daily",
            "scheduled_at": "2026-09-29 09:00:00",
            "status": "delivered",
            "retry_count": 0,
        }
        self.assertFalse(should_send_digest_failure_alert(exec_delivered, now=now))

        exec_partial = {
            "user_id": 1,
            "period": "daily",
            "scheduled_at": "2026-09-29 09:00:00",
            "status": "partial",
            "retry_count": 0,
        }
        self.assertFalse(should_send_digest_failure_alert(exec_partial, now=now))

    def test_should_send_digest_failure_alert_retries_and_grace_period(self):
        sched_time = datetime(2026, 9, 29, 9, 0, 0, tzinfo=timezone.utc)
        sched_str = db.serialize_datetime(sched_time)

        exec_retrying_not_exhausted = {
            "user_id": 10,
            "period": "daily",
            "scheduled_at": sched_str,
            "status": "retrying",
            "retry_count": 2,  # < MAX_DIGEST_RETRIES (5)
        }
        now_after_grace = sched_time + timedelta(seconds=3700)
        # Retries not exhausted yet -> False
        self.assertFalse(
            should_send_digest_failure_alert(exec_retrying_not_exhausted, now=now_after_grace)
        )

        # Retries exhausted (retry_count=5)
        exec_retrying_exhausted = {
            "user_id": 10,
            "period": "daily",
            "scheduled_at": sched_str,
            "status": "retrying",
            "retry_count": 5,
        }
        # Before grace period -> False
        now_before_grace = sched_time + timedelta(seconds=1800)
        self.assertFalse(
            should_send_digest_failure_alert(exec_retrying_exhausted, now=now_before_grace)
        )
        # After grace period -> True
        self.assertTrue(
            should_send_digest_failure_alert(exec_retrying_exhausted, now=now_after_grace)
        )

        # Status failed (exhausted by definition)
        exec_failed = {
            "user_id": 10,
            "period": "daily",
            "scheduled_at": sched_str,
            "status": "failed",
            "retry_count": 1,
        }
        self.assertTrue(
            should_send_digest_failure_alert(exec_failed, now=now_after_grace)
        )

    def test_user_alert_settings_disable_and_pause(self):
        user_id = 42
        # Default is enabled
        self.assertTrue(db.is_user_alerting_active(user_id))

        # Disable
        db.set_user_alerts_enabled(user_id, False)
        settings = db.get_user_alert_settings(user_id)
        self.assertFalse(settings["alerts_enabled"])
        self.assertFalse(db.is_user_alerting_active(user_id))

        # Re-enable
        db.set_user_alerts_enabled(user_id, True)
        self.assertTrue(db.is_user_alerting_active(user_id))

        # Pause until future
        future = datetime(2026, 9, 29, 15, 0, 0, tzinfo=timezone.utc)
        db.set_user_alerts_paused_until(user_id, db.serialize_datetime(future))
        now_during_pause = datetime(2026, 9, 29, 12, 0, 0, tzinfo=timezone.utc)
        now_after_pause = datetime(2026, 9, 29, 16, 0, 0, tzinfo=timezone.utc)
        self.assertFalse(db.is_user_alerting_active(user_id, now=now_during_pause))
        self.assertTrue(db.is_user_alerting_active(user_id, now=now_after_pause))

    def test_send_digest_failure_alert_success(self):
        user_id = 100
        period = "daily"
        scheduled_at = "2026-09-29 08:00:00"

        bot_mock = AsyncMock()
        msg_mock = MagicMock()
        msg_mock.message_id = 999
        bot_mock.send_message.return_value = msg_mock

        execution = {
            "user_id": user_id,
            "period": period,
            "scheduled_at": scheduled_at,
            "channels_count": 4,
            "channels_failed": 4,
            "status": "failed",
            "retry_count": 5,
            "error_message": "Network down",
        }

        sent = asyncio.run(send_digest_failure_alert(bot_mock, execution))
        self.assertTrue(sent)
        bot_mock.send_message.assert_called_once()
        self.assertTrue(db.has_digest_alert_been_sent(user_id, period, scheduled_at, "failure"))

        # Subsequent call must be skipped (deduplication)
        sent_again = asyncio.run(send_digest_failure_alert(bot_mock, execution))
        self.assertFalse(sent_again)
        self.assertEqual(bot_mock.send_message.call_count, 1)

    def test_send_digest_failure_alert_forbidden(self):
        user_id = 200
        period = "daily"
        scheduled_at = "2026-09-29 08:00:00"

        bot_mock = AsyncMock()
        bot_mock.send_message.side_effect = TelegramForbiddenError(
            method=MagicMock(),
            message="Forbidden: bot was blocked by the user",
        )

        execution = {
            "user_id": user_id,
            "period": period,
            "scheduled_at": scheduled_at,
            "channels_count": 2,
            "channels_failed": 2,
            "status": "failed",
            "retry_count": 5,
        }

        sent = asyncio.run(send_digest_failure_alert(bot_mock, execution))
        self.assertFalse(sent)
        # Alerts should now be disabled for this user
        settings = db.get_user_alert_settings(user_id)
        self.assertFalse(settings["alerts_enabled"])
        # Recorded so we don't try again
        self.assertTrue(db.has_digest_alert_been_sent(user_id, period, scheduled_at, "failure"))

    def test_process_pending_digest_alerts_end_to_end(self):
        user_id = 300
        period = "daily"
        base_time = datetime(2026, 9, 29, 8, 0, 0, tzinfo=timezone.utc)
        sched_str = db.serialize_datetime(base_time)

        # 1. Create a failed execution
        exec_id = db.record_digest_execution_start(user_id, period, sched_str)
        db.record_digest_execution_finish(
            exec_id,
            status="failed",
            channels_count=3,
            channels_failed=3,
            error_message="Timeout",
        )

        bot_mock = AsyncMock()
        msg_mock = MagicMock()
        msg_mock.message_id = 1234
        bot_mock.send_message.return_value = msg_mock

        # Within grace period (30 mins after scheduled) -> 0 sent
        now_30m = base_time + timedelta(minutes=30)
        count_early = asyncio.run(
            process_pending_digest_alerts(bot_mock, now=now_30m, grace_period_seconds=3600)
        )
        self.assertEqual(count_early, 0)
        self.assertEqual(bot_mock.send_message.call_count, 0)

        # After grace period (70 mins after scheduled) -> 1 sent
        now_70m = base_time + timedelta(minutes=70)
        count_due = asyncio.run(
            process_pending_digest_alerts(bot_mock, now=now_70m, grace_period_seconds=3600)
        )
        self.assertEqual(count_due, 1)
        self.assertEqual(bot_mock.send_message.call_count, 1)

        # Run again immediately -> 0 sent (deduplicated)
        count_repeat = asyncio.run(
            process_pending_digest_alerts(bot_mock, now=now_70m, grace_period_seconds=3600)
        )
        self.assertEqual(count_repeat, 0)
        self.assertEqual(bot_mock.send_message.call_count, 1)

    def test_process_pending_digest_alerts_ignores_empty(self):
        user_id = 400
        period = "daily"
        base_time = datetime(2026, 9, 29, 8, 0, 0, tzinfo=timezone.utc)
        sched_str = db.serialize_datetime(base_time)

        # Create an empty execution
        exec_id = db.record_digest_execution_start(user_id, period, sched_str)
        db.record_digest_execution_finish(
            exec_id,
            status="empty",
            channels_count=5,
            channels_failed=0,
        )

        bot_mock = AsyncMock()
        now_70m = base_time + timedelta(minutes=70)
        count = asyncio.run(
            process_pending_digest_alerts(bot_mock, now=now_70m, grace_period_seconds=3600)
        )
        self.assertEqual(count, 0)
        self.assertEqual(bot_mock.send_message.call_count, 0)


if __name__ == "__main__":
    unittest.main()
