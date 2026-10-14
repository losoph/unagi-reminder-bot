import os
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("BOT_TOKEN", "123456:TEST_TOKEN_FOR_UNIT_TESTS")

from channel_source import HybridChannelSource  # noqa: E402
from data import database  # noqa: E402
import feed  # noqa: E402


class FixesT24bTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = os.path.join(self.temp_dir.name, "test_fixes_t24b.db")
        database.init_db()
        feed._personal_channels_cache.clear()
        feed._backfill_tasks.clear()

    def tearDown(self):
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()
        feed._personal_channels_cache.clear()
        feed._backfill_tasks.clear()

    async def test_item_14_list_personal_channels_logs_only_error_type_and_seconds(self):
        """Item 14: list_personal_channels must log only exception type and seconds, without str(exc)."""
        source = HybridChannelSource()

        # Case 1: Generic exception with sensitive text
        class SecretError(Exception):
            pass

        sensitive_client = MagicMock()
        sensitive_client.iter_dialogs = MagicMock(side_effect=SecretError("SECRET_PRIVATE_DATA"))
        source._client = sensitive_client

        with patch("channel_source.logger.warning") as mock_warn:
            res = await source.list_personal_channels(limit=10)
            self.assertEqual(res, [])
            mock_warn.assert_called_once()
            log_args = mock_warn.call_args[0]
            rendered_log = log_args[0] % log_args[1:]
            self.assertIn("SecretError", rendered_log)
            self.assertNotIn("SECRET_PRIVATE_DATA", rendered_log)

        # Case 2: FloodWait-like error with .seconds attribute
        class MockFloodWait(Exception):
            def __init__(self, seconds: int):
                super().__init__(f"Flood wait of {seconds} seconds required for private chat 12345")
                self.seconds = seconds

        flood_client = MagicMock()
        flood_client.iter_dialogs = MagicMock(side_effect=MockFloodWait(35))
        source._client = flood_client

        with patch("channel_source.logger.warning") as mock_warn:
            res = await source.list_personal_channels(limit=10)
            self.assertEqual(res, [])
            mock_warn.assert_called_once()
            log_args = mock_warn.call_args[0]
            rendered_log = log_args[0] % log_args[1:]
            self.assertIn("MockFloodWait", rendered_log)
            self.assertIn("35", rendered_log)
            self.assertNotIn("private chat 12345", rendered_log)

    async def test_item_15_repeat_personal_channel_add_answers_already_in_feed_and_no_backfill(self):
        """Item 15: Repeat click on personal channel already in feed replies 'уже в Ленте' without backfill."""
        owner_id = 12345
        source = HybridChannelSource()
        source.account_id = owner_id
        feed.configure(channel_source=source)

        # Pre-seed personal channels cache
        feed.set_cached_personal_channels(
            owner_id,
            [{"username": "stale_chan", "title": "Stale Channel", "unread_count": 10}],
        )

        # Add subscription beforehand so it's already in feed
        database.add_feed_subscription(owner_id, "stale_chan", channel_title="Stale Channel")

        cb = MagicMock()
        cb.from_user.id = owner_id
        cb.data = "fadd_p:0"
        cb.message = MagicMock()
        cb.message.chat.id = owner_id
        cb.message.edit_reply_markup = AsyncMock()
        cb.answer = AsyncMock()

        with patch("feed.schedule_channel_backfill") as mock_backfill:
            await feed.cb_feed_add_personal(cb)
            # schedule_channel_backfill MUST NOT be called!
            mock_backfill.assert_not_called()

        # Toast answer must indicate already in feed
        cb.answer.assert_called_once()
        answer_text = cb.answer.call_args[0][0]
        self.assertIn("уже в ленте", answer_text.lower())
        self.assertEqual(answer_text, "уже в Ленте")

        # Markup should be updated and show the checkmark
        cb.message.edit_reply_markup.assert_called_once()
        kb = cb.message.edit_reply_markup.call_args[1]["reply_markup"]
        btn = kb.inline_keyboard[0][0]
        self.assertTrue(btn.text.startswith("✅"))


if __name__ == "__main__":
    unittest.main()
