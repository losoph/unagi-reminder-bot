import os

os.environ.setdefault("BOT_TOKEN", "123456:TEST_TOKEN_FOR_UNIT_TESTS")

import asyncio  # noqa: E402
import tempfile  # noqa: E402
import unittest  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402
from unittest.mock import AsyncMock, MagicMock  # noqa: E402

import data.database as db  # noqa: E402
from digest_alerts import build_digest_alert_keyboard  # noqa: E402
from main import (  # noqa: E402
    build_digest_settings_keyboard,
    handle_alert_disable,
    handle_alert_pause_24h,
    handle_alert_pause_7d,
    handle_alert_retry,
    handle_alert_toggle_enable,
    render_digest_settings_text,
)


class TestAlertControls(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp_dir.name, "test_controls.db")
        self.orig_db_path = db.DB_PATH
        db.DB_PATH = self.db_path
        db.init_db()

    def tearDown(self):
        db.DB_PATH = self.orig_db_path
        self.tmp_dir.cleanup()

    def test_build_digest_alert_keyboard_buttons(self):
        kb = build_digest_alert_keyboard(execution_id=42)
        callbacks = [btn.callback_data for row in kb.inline_keyboard for btn in row]
        self.assertIn("alert_retry_42", callbacks)
        self.assertIn("alert_pause_24h", callbacks)
        self.assertIn("alert_pause_7d", callbacks)
        self.assertIn("alert_disable", callbacks)
        self.assertIn("ds", callbacks)

        # Labels check
        labels = [btn.text for row in kb.inline_keyboard for btn in row]
        self.assertTrue(any("Повторить" in label for label in labels))
        self.assertTrue(any("24 ч" in label for label in labels))
        self.assertTrue(any("7 дней" in label for label in labels))
        self.assertTrue(any("Отключить" in label for label in labels))
        self.assertTrue(any("Настройки" in label for label in labels))

    def test_retry_digest_execution(self):
        user_id = 50
        period = "daily"
        now = datetime(2026, 9, 29, 9, 0, 0, tzinfo=timezone.utc)
        sched_str = db.serialize_datetime(now)

        # 1. Add subscription and failed execution
        db.add_subscription(user_id, "channel1", "Channel 1", period, sched_str, sched_str)
        exec_id = db.record_digest_execution_start(user_id, period, sched_str)
        db.record_digest_execution_finish(
            exec_id,
            status="failed",
            channels_count=1,
            channels_failed=1,
            error_message="Fail",
        )

        # Mark subscription with errors
        subs = db.get_user_subscriptions(user_id)
        sub_id = subs[0][0]
        db.mark_subscription_delivery_error(sub_id, "Fail", sched_str, failure_count=5, is_permanent=True)

        # 2. Trigger retry
        success = db.retry_digest_execution(exec_id)
        self.assertTrue(success)

        # 3. Check execution updated to retrying with retry_count=0
        updated_exec = db.get_digest_execution_by_id(exec_id)
        self.assertEqual(updated_exec["status"], "retrying")
        self.assertEqual(updated_exec["retry_count"], 0)
        self.assertIsNone(updated_exec["error_message"])

        # 4. Check subscription reset to pending and failure_count=0
        updated_subs = db.get_user_subscriptions(user_id)
        self.assertEqual(updated_subs[0][8], 0)  # failure_count
        self.assertEqual(updated_subs[0][7], "pending")  # digest_status

    def test_alert_pause_and_disable_do_not_stop_digests(self):
        user_id = 60
        period = "daily"
        now = datetime(2026, 9, 29, 9, 0, 0, tzinfo=timezone.utc)
        sched_str = db.serialize_datetime(now)

        db.add_subscription(user_id, "chan_test", "Channel Test", period, sched_str, sched_str)

        # Pause alerts for 24h
        future = now + timedelta(hours=24)
        db.set_user_alerts_paused_until(user_id, db.serialize_datetime(future))
        self.assertFalse(db.is_user_alerting_active(user_id, now=now))

        # Check subscriptions are NOT paused or disabled!
        subs = db.get_user_subscriptions(user_id)
        self.assertEqual(subs[0][5], 0)  # is_paused = 0
        due = db.get_due_subscriptions(sched_str, 10)
        self.assertEqual(len(due), 1)

        # Disable alerts completely
        db.set_user_alerts_enabled(user_id, False)
        self.assertFalse(db.is_user_alerting_active(user_id, now=now))

        # Check subscriptions are STILL active and due!
        subs2 = db.get_user_subscriptions(user_id)
        self.assertEqual(subs2[0][5], 0)
        due2 = db.get_due_subscriptions(sched_str, 10)
        self.assertEqual(len(due2), 1)

    def test_settings_screen_alert_status_and_toggle_buttons(self):
        user_id = 70
        now = datetime(2026, 9, 29, 9, 0, 0, tzinfo=timezone.utc)
        sched_str = db.serialize_datetime(now)
        db.add_subscription(user_id, "chan70", "Channel 70", "daily", sched_str, sched_str)
        user_subs = db.get_user_subscriptions(user_id)

        # 1. Default (enabled)
        text_enabled = render_digest_settings_text(user_id)
        self.assertIn("Технические алерты:</b> включены", text_enabled)
        kb_enabled = build_digest_settings_keyboard(user_subs, user_id=user_id)
        callbacks_enabled = [btn.callback_data for row in kb_enabled.inline_keyboard for btn in row]
        self.assertIn("alert_disable", callbacks_enabled)

        # 2. Disabled
        db.set_user_alerts_enabled(user_id, False)
        text_disabled = render_digest_settings_text(user_id)
        self.assertIn("Технические алерты:</b> отключены", text_disabled)
        kb_disabled = build_digest_settings_keyboard(user_subs, user_id=user_id)
        callbacks_disabled = [btn.callback_data for row in kb_disabled.inline_keyboard for btn in row]
        self.assertIn("alert_toggle_enable", callbacks_disabled)

        # 3. Paused
        future = datetime(2026, 9, 29, 20, 0, 0, tzinfo=timezone.utc)
        db.set_user_alerts_paused_until(user_id, db.serialize_datetime(future))
        text_paused = render_digest_settings_text(user_id)
        self.assertIn("Технические алерты:</b> на паузе", text_paused)
        kb_paused = build_digest_settings_keyboard(user_subs, user_id=user_id)
        labels_paused = [btn.text for row in kb_paused.inline_keyboard for btn in row]
        self.assertTrue(any("Снять паузу" in label for label in labels_paused))

    def test_callback_handlers(self):
        user_id = 80
        period = "daily"
        now = datetime(2026, 9, 29, 9, 0, 0, tzinfo=timezone.utc)
        sched_str = db.serialize_datetime(now)

        db.add_subscription(user_id, "c80", "C80", period, sched_str, sched_str)
        exec_id = db.record_digest_execution_start(user_id, period, sched_str)

        # Setup mock CallbackQuery
        cb = MagicMock()
        cb.from_user.id = user_id
        cb.message.chat.id = user_id
        cb.message.text = "⚠️ Алерт о пропуске"
        cb.message.caption = None
        cb.message.edit_text = AsyncMock()
        cb.answer = AsyncMock()

        # 1. Test alert_retry
        cb.data = f"alert_retry_{exec_id}"
        asyncio.run(handle_alert_retry(cb))
        cb.answer.assert_called()
        self.assertTrue(any("Повтор запущен" in str(arg) for arg in cb.answer.call_args[0]))
        cb.message.edit_text.assert_called()

        # 2. Test alert_pause_24h
        cb.data = "alert_pause_24h"
        asyncio.run(handle_alert_pause_24h(cb))
        settings = db.get_user_alert_settings(user_id)
        self.assertIsNotNone(settings["alerts_paused_until"])

        # 3. Test alert_pause_7d
        cb.data = "alert_pause_7d"
        asyncio.run(handle_alert_pause_7d(cb))
        settings7 = db.get_user_alert_settings(user_id)
        self.assertIsNotNone(settings7["alerts_paused_until"])

        # 4. Test alert_disable
        cb.data = "alert_disable"
        asyncio.run(handle_alert_disable(cb))
        settings_off = db.get_user_alert_settings(user_id)
        self.assertFalse(settings_off["alerts_enabled"])

        # 5. Test alert_toggle_enable
        cb.data = "alert_toggle_enable"
        asyncio.run(handle_alert_toggle_enable(cb))
        settings_on = db.get_user_alert_settings(user_id)
        self.assertTrue(settings_on["alerts_enabled"])
        self.assertIsNone(settings_on["alerts_paused_until"])


if __name__ == "__main__":
    unittest.main()
