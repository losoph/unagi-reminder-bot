import os

os.environ.setdefault("BOT_TOKEN", "123456:TEST_TOKEN_FOR_UNIT_TESTS")

import asyncio  # noqa: E402
import tempfile  # noqa: E402
import unittest  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from unittest.mock import AsyncMock, MagicMock  # noqa: E402

from aiogram.exceptions import TelegramForbiddenError  # noqa: E402

import data.database as db  # noqa: E402
from digest_alerts import (  # noqa: E402
    format_recovery_alert,
    process_pending_digest_alerts,
    process_pending_recovery_alerts,
    send_digest_recovery_alert,
)


class TestAlertRecovery(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp_dir.name, "test_recovery.db")
        self.orig_db_path = db.DB_PATH
        db.DB_PATH = self.db_path
        db.init_db()

    def tearDown(self):
        db.DB_PATH = self.orig_db_path
        self.tmp_dir.cleanup()

    def test_format_recovery_alert(self):
        sched = "2026-09-29 08:00:00"
        text = format_recovery_alert("daily", sched, posts_count=12, digest_url="https://telegra.ph/digest-123")
        self.assertIn("Утренний дайджест восстановлен", text)
        self.assertIn("29.09.2026 08:00 UTC", text)
        self.assertIn("12", text)
        self.assertIn("https://telegra.ph/digest-123", text)

        # Without telegraph
        text_no_url = format_recovery_alert("weekly", sched, posts_count=5, digest_url=None)
        self.assertIn("Еженедельный дайджест восстановлен", text_no_url)
        self.assertIn("5", text_no_url)
        self.assertNotIn("Telegraph", text_no_url)

    def test_recovery_sent_after_failure_alert(self):
        user_id = 555
        period = "daily"
        sched = "2026-09-29 08:00:00"

        # 1. Record execution and failure alert
        exec_id = db.record_digest_execution_start(user_id, period, sched)
        db.record_digest_execution_finish(exec_id, status="failed", error_message="Network error")
        db.record_digest_alert_sent(user_id, period, sched, alert_type="failure", message_id=101)

        # 2. Execution later finishes with delivered status
        db.record_digest_execution_finish(exec_id, status="delivered", posts_count=7)

        # 3. Record telegraph digest
        db.add_telegraph_digest(user_id, "https://telegra.ph/rec-555", "Дайджест", 2, 7)

        # 4. Send recovery alert
        bot = MagicMock()
        mock_msg = MagicMock()
        mock_msg.message_id = 202
        bot.send_message = AsyncMock(return_value=mock_msg)

        execution = db.get_digest_execution_by_id(exec_id)
        result = asyncio.run(send_digest_recovery_alert(bot, execution))

        self.assertTrue(result)
        bot.send_message.assert_called_once()
        call_kwargs = bot.send_message.call_args.kwargs
        self.assertEqual(call_kwargs["chat_id"], user_id)
        self.assertIn("восстановлен", call_kwargs["text"])
        self.assertIn("7", call_kwargs["text"])
        self.assertIn("https://telegra.ph/rec-555", call_kwargs["text"])

        # Verify DB recorded recovery alert
        self.assertTrue(db.has_digest_alert_been_sent(user_id, period, sched, "recovery"))

    def test_no_recovery_if_no_failure_alert(self):
        user_id = 556
        period = "daily"
        sched = "2026-09-29 08:00:00"

        # Execution succeeded on first try, no failure alert was ever sent
        exec_id = db.record_digest_execution_start(user_id, period, sched)
        db.record_digest_execution_finish(exec_id, status="delivered", posts_count=5)

        bot = MagicMock()
        bot.send_message = AsyncMock()

        execution = db.get_digest_execution_by_id(exec_id)
        result = asyncio.run(send_digest_recovery_alert(bot, execution))

        self.assertFalse(result)
        bot.send_message.assert_not_called()
        self.assertFalse(db.has_digest_alert_been_sent(user_id, period, sched, "recovery"))

    def test_no_recovery_if_alerts_disabled(self):
        user_id = 557
        period = "daily"
        sched = "2026-09-29 08:00:00"

        exec_id = db.record_digest_execution_start(user_id, period, sched)
        db.record_digest_execution_finish(exec_id, status="failed", error_message="Network error")
        db.record_digest_alert_sent(user_id, period, sched, alert_type="failure", message_id=102)

        # User disables alerts
        db.set_user_alerts_enabled(user_id, False)

        # Later delivered
        db.record_digest_execution_finish(exec_id, status="delivered", posts_count=3)

        bot = MagicMock()
        bot.send_message = AsyncMock()

        execution = db.get_digest_execution_by_id(exec_id)
        result = asyncio.run(send_digest_recovery_alert(bot, execution))

        self.assertFalse(result)
        bot.send_message.assert_not_called()

    def test_no_recovery_if_alerts_paused(self):
        user_id = 558
        period = "daily"
        sched = "2026-09-29 08:00:00"

        exec_id = db.record_digest_execution_start(user_id, period, sched)
        db.record_digest_execution_finish(exec_id, status="failed", error_message="Network error")
        db.record_digest_alert_sent(user_id, period, sched, alert_type="failure", message_id=103)

        # User pauses alerts until 2026-09-30
        db.set_user_alerts_paused_until(user_id, "2026-09-30 08:00:00")

        # Later delivered at 09:00 on 2026-09-29 (during pause)
        db.record_digest_execution_finish(exec_id, status="delivered", posts_count=4)

        bot = MagicMock()
        bot.send_message = AsyncMock()

        now = datetime(2026, 9, 29, 9, 0, 0, tzinfo=timezone.utc)
        execution = db.get_digest_execution_by_id(exec_id)
        result = asyncio.run(send_digest_recovery_alert(bot, execution, now=now))

        self.assertFalse(result)
        bot.send_message.assert_not_called()

    def test_recovery_deduplication(self):
        user_id = 559
        period = "daily"
        sched = "2026-09-29 08:00:00"

        exec_id = db.record_digest_execution_start(user_id, period, sched)
        db.record_digest_execution_finish(exec_id, status="failed", error_message="Error")
        db.record_digest_alert_sent(user_id, period, sched, alert_type="failure", message_id=104)

        db.record_digest_execution_finish(exec_id, status="delivered", posts_count=10)

        bot = MagicMock()
        mock_msg = MagicMock()
        mock_msg.message_id = 303
        bot.send_message = AsyncMock(return_value=mock_msg)

        execution = db.get_digest_execution_by_id(exec_id)
        # First send -> succeeds
        res1 = asyncio.run(send_digest_recovery_alert(bot, execution))
        self.assertTrue(res1)
        self.assertEqual(bot.send_message.call_count, 1)

        # Second send -> deduplicated, does not send again
        res2 = asyncio.run(send_digest_recovery_alert(bot, execution))
        self.assertFalse(res2)
        self.assertEqual(bot.send_message.call_count, 1)

    def test_recovery_telegram_forbidden_error(self):
        user_id = 560
        period = "daily"
        sched = "2026-09-29 08:00:00"

        exec_id = db.record_digest_execution_start(user_id, period, sched)
        db.record_digest_alert_sent(user_id, period, sched, alert_type="failure", message_id=105)
        db.record_digest_execution_finish(exec_id, status="delivered", posts_count=2)

        bot = MagicMock()
        bot.send_message = AsyncMock(side_effect=TelegramForbiddenError(MagicMock(), "Bot was blocked by user"))

        execution = db.get_digest_execution_by_id(exec_id)
        result = asyncio.run(send_digest_recovery_alert(bot, execution))

        self.assertFalse(result)
        # User alerts must be disabled after TelegramForbiddenError
        self.assertFalse(db.is_user_alerting_active(user_id))

    def test_process_pending_recovery_alerts_batch(self):
        # Setup 2 executions:
        # User 1: failed + alert sent -> then delivered (should get recovery alert)
        u1 = 601
        sched1 = "2026-09-29 08:00:00"
        e1 = db.record_digest_execution_start(u1, "daily", sched1)
        db.record_digest_alert_sent(u1, "daily", sched1, alert_type="failure", message_id=10)
        db.record_digest_execution_finish(e1, status="delivered", posts_count=6)

        # User 2: delivered directly without failure alert (should NOT get recovery alert)
        u2 = 602
        sched2 = "2026-09-29 08:00:00"
        e2 = db.record_digest_execution_start(u2, "daily", sched2)
        db.record_digest_execution_finish(e2, status="delivered", posts_count=4)

        bot = MagicMock()
        mock_msg = MagicMock()
        mock_msg.message_id = 404
        bot.send_message = AsyncMock(return_value=mock_msg)

        sent_count = asyncio.run(process_pending_recovery_alerts(bot))
        self.assertEqual(sent_count, 1)
        bot.send_message.assert_called_once()
        self.assertEqual(bot.send_message.call_args.kwargs["chat_id"], u1)

    def test_process_pending_digest_alerts_includes_recovery(self):
        u1 = 701
        sched1 = "2026-09-29 08:00:00"
        e1 = db.record_digest_execution_start(u1, "daily", sched1)
        db.record_digest_alert_sent(u1, "daily", sched1, alert_type="failure", message_id=20)
        db.record_digest_execution_finish(e1, status="delivered", posts_count=8)

        bot = MagicMock()
        mock_msg = MagicMock()
        mock_msg.message_id = 505
        bot.send_message = AsyncMock(return_value=mock_msg)

        total_sent = asyncio.run(process_pending_digest_alerts(bot))
        self.assertEqual(total_sent, 1)
        bot.send_message.assert_called_once()

    def test_retry_digest_execution_preserves_scheduled_at_for_recovery(self):
        user_id = 801
        period = "daily"
        sched = "2026-09-29 08:00:00"

        db.add_subscription(user_id, "@testch", "Test Ch", period, "2026-09-28 08:00:00", sched)
        exec_id = db.record_digest_execution_start(user_id, period, sched)
        db.record_digest_execution_finish(exec_id, status="failed", error_message="timeout")
        db.record_digest_alert_sent(user_id, period, sched, alert_type="failure", message_id=30)

        # Retry triggered by user
        self.assertTrue(db.retry_digest_execution(exec_id))

        # Check that subscription next_send_at preserved scheduled_at
        due = db.get_due_subscriptions("2026-09-29 10:00:00")
        sub = next((s for s in due if s[1] == user_id), None)
        self.assertIsNotNone(sub)
        self.assertEqual(sub[8], sched)


if __name__ == "__main__":
    unittest.main()
