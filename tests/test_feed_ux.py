import os
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("BOT_TOKEN", "123456:TEST_TOKEN_FOR_UNIT_TESTS")

from aiogram.fsm.context import FSMContext  # noqa: E402
from aiogram.fsm.storage.memory import MemoryStorage  # noqa: E402
from aiogram.fsm.storage.base import StorageKey  # noqa: E402

from data import database  # noqa: E402
import feed  # noqa: E402
import main  # noqa: E402


class FeedUXTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = os.path.join(self.temp_dir.name, "test_feed_ux.db")
        database.init_db()
        self.storage = MemoryStorage()
        feed._backfill_tasks.clear()

    async def asyncTearDown(self):
        await feed.cancel_backfill_tasks()
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def _make_fsm_context(self, user_id: int) -> FSMContext:
        key = StorageKey(bot_id=12345, chat_id=user_id, user_id=user_id)
        return FSMContext(storage=self.storage, key=key)

    def _make_callback(self, user_id: int, data: str) -> MagicMock:
        cb = MagicMock()
        cb.from_user.id = user_id
        cb.message = MagicMock()
        cb.message.chat.id = user_id
        cb.message.edit_text = AsyncMock()
        cb.message.answer = AsyncMock()
        cb.answer = AsyncMock()
        cb.data = data
        return cb

    async def test_feed_add_from_fsm(self):
        """feed_add creates subscription from FSM and offers tag, open, and digest buttons."""
        user_id = 1001
        state = self._make_fsm_context(user_id)
        await state.update_data(channel_username="ai_channel", channel_title="AI News")

        cb = self._make_callback(user_id, "feed_add")
        await feed.cb_feed_add_from_fsm(cb, state)

        cb.answer.assert_called_with("✅ Добавлен в Ленту!")
        cb.message.edit_text.assert_called()

        sub = database.get_feed_subscription_by_username(user_id, "ai_channel")
        self.assertIsNotNone(sub)
        self.assertEqual(sub["channel_title"], "AI News")

        # Verify buttons
        markup = cb.message.edit_text.call_args[1]["reply_markup"]
        all_buttons = [b for row in markup.inline_keyboard for b in row]
        button_datas = [b.callback_data for b in all_buttons]

        self.assertIn(f"ftag:{sub['id']}", button_datas)
        self.assertIn("feed_main", button_datas)
        self.assertIn("digest_setup", button_datas)

        for b in all_buttons:
            self.assertLessEqual(len(b.callback_data.encode()), 64)

    async def test_feed_add_duplicate_and_limit(self):
        """Duplicate addition is idempotent; limit displays error alert."""
        user_id = 1002
        state = self._make_fsm_context(user_id)
        await state.update_data(channel_username="channel_x", channel_title="Channel X")

        cb1 = self._make_callback(user_id, "feed_add")
        await feed.cb_feed_add_from_fsm(cb1, state)
        self.assertEqual(database.count_feed_subscriptions(user_id), 1)

        # Duplicate
        cb2 = self._make_callback(user_id, "feed_add")
        await feed.cb_feed_add_from_fsm(cb2, state)
        self.assertEqual(database.count_feed_subscriptions(user_id), 1)

        # Limit
        with patch.object(database, "MAX_FEED_CHANNELS", 1):
            await state.update_data(channel_username="channel_y", channel_title="Channel Y")
            cb3 = self._make_callback(user_id, "feed_add")
            await feed.cb_feed_add_from_fsm(cb3, state)
            cb3.answer.assert_called()
            self.assertTrue(any("лимит" in str(arg).lower() for arg in cb3.answer.call_args[0]))
            self.assertEqual(database.count_feed_subscriptions(user_id), 1)

    async def test_feed_copy_from_digest(self):
        """feed_copy_{sub_id} copies a channel from digest subscription into feed."""
        user_id = 1003
        digest_sub_id = database.add_subscription(
            user_id,
            "digest_ch",
            "Digest Ch",
            "daily",
            database.serialize_datetime(database.utc_now()),
            database.serialize_datetime(database.utc_now()),
            tag="it",
        )

        cb = self._make_callback(user_id, f"feed_copy_{digest_sub_id}")
        await feed.cb_feed_copy_from_digest(cb)

        cb.answer.assert_called_with("✅ Канал добавлен в Ленту!")
        feed_sub = database.get_feed_subscription_by_username(user_id, "digest_ch")
        self.assertIsNotNone(feed_sub)
        self.assertEqual(feed_sub["channel_title"], "Digest Ch")
        self.assertEqual(feed_sub["tag"], "it")

    async def test_feed_channel_list_pagination_and_card(self):
        """Channel list paginates at 8 items and opens channel card."""
        user_id = 1004
        for i in range(10):
            database.add_feed_subscription(user_id, f"ch_{i}", f"Channel {i}")

        cb_page0 = self._make_callback(user_id, "feed_channels_0")
        state = self._make_fsm_context(user_id)
        await feed.cb_feed_channels(cb_page0, state)

        markup0 = cb_page0.message.edit_text.call_args[1]["reply_markup"]
        all_b0 = [b for row in markup0.inline_keyboard for b in row]
        # 8 channels + 1 next button + 1 back to feed button = 10
        self.assertTrue(any(b.callback_data == "feed_channels_1" for b in all_b0))

        # Page 1
        cb_page1 = self._make_callback(user_id, "feed_channels_1")
        await feed.cb_feed_channels(cb_page1, state)
        markup1 = cb_page1.message.edit_text.call_args[1]["reply_markup"]
        all_b1 = [b for row in markup1.inline_keyboard for b in row]
        self.assertTrue(any(b.callback_data == "feed_channels_0" for b in all_b1))

        # Card
        sub = database.get_feed_subscription_by_username(user_id, "ch_0")
        cb_card = self._make_callback(user_id, f"feed_ch:{sub['id']}")
        await feed.cb_feed_channel_card(cb_card, state)
        card_markup = cb_card.message.edit_text.call_args[1]["reply_markup"]
        card_datas = [b.callback_data for row in card_markup.inline_keyboard for b in row]
        self.assertIn(f"ftag:{sub['id']}", card_datas)
        self.assertIn(f"feed_pause:{sub['id']}", card_datas)
        self.assertIn(f"feed_del:{sub['id']}", card_datas)
        self.assertIn("feed_channels_0", card_datas)

    async def test_feed_pause_resume_and_delete(self):
        """Pause toggles status, delete removes subscription."""
        user_id = 1005
        sub_id = database.add_feed_subscription(user_id, "test_channel", "Test")

        # Pause
        cb_pause = self._make_callback(user_id, f"feed_pause:{sub_id}")
        await feed.cb_feed_pause(cb_pause)
        self.assertEqual(database.get_feed_subscription(user_id, sub_id)["is_paused"], 1)

        # Resume
        cb_resume = self._make_callback(user_id, f"feed_resume:{sub_id}")
        await feed.cb_feed_resume(cb_resume)
        self.assertEqual(database.get_feed_subscription(user_id, sub_id)["is_paused"], 0)

        # Delete
        state = self._make_fsm_context(user_id)
        cb_del = self._make_callback(user_id, f"feed_del:{sub_id}")
        await feed.cb_feed_delete(cb_del, state)
        self.assertIsNone(database.get_feed_subscription(user_id, sub_id))

    async def test_feed_tag_ux_pick_new_clear(self):
        """Tag flow: choose existing tag by index, clear tag, or enter new tag via text."""
        user_id = 1006
        sub_id = database.add_feed_subscription(user_id, "chan_tagged", "Tagged", tag="old_tag")
        database.add_feed_subscription(user_id, "chan_other", "Other", tag="ai")
        state = self._make_fsm_context(user_id)

        # Open tag menu
        cb_menu = self._make_callback(user_id, f"ftag:{sub_id}")
        await feed.cb_feed_tag_menu(cb_menu, state)

        # Warning in form
        text = cb_menu.message.edit_text.call_args[0][0]
        self.assertIn("Тег виден на странице Ленты в Telegraph", text)

        # Pick tag by index (0 corresponds to first tag in choices)
        data = await state.get_data()
        choices = data.get("tag_choices")
        self.assertIn("ai", choices)
        ai_idx = choices.index("ai")

        cb_pick = self._make_callback(user_id, f"ftag_pick:{ai_idx}")
        await feed.cb_feed_tag_pick(cb_pick, state)
        self.assertEqual(database.get_feed_subscription(user_id, sub_id)["tag"], "ai")

        # Clear tag
        await state.update_data(tag_sub_id=sub_id)
        cb_clear = self._make_callback(user_id, "ftag_clear")
        await feed.cb_feed_tag_clear(cb_clear, state)
        self.assertIsNone(database.get_feed_subscription(user_id, sub_id)["tag"])

        # Enter new tag via message
        await state.set_state(feed.FeedTagState.waiting_for_tag)
        await state.update_data(tag_sub_id=sub_id)

        msg = MagicMock()
        msg.chat.id = user_id
        msg.text = "#новости "
        msg.answer = AsyncMock()
        await feed.process_feed_tag_text(msg, state)

        self.assertEqual(database.get_feed_subscription(user_id, sub_id)["tag"], "новости")
        self.assertIsNone(await state.get_state())

    def test_keyboards_callback_data_len(self):
        """Verify all callback_data in build_digest_channel_actions and feed main are <= 64 bytes."""
        card_kb = main.build_digest_channel_actions(sub_id=999999, current_period="daily", is_paused=False)
        for row in card_kb.inline_keyboard:
            for b in row:
                self.assertLessEqual(len(b.callback_data.encode()), 64)

        main_kb = feed.build_feed_main_keyboard(total_channels=100)
        for row in main_kb.inline_keyboard:
            for b in row:
                self.assertLessEqual(len(b.callback_data.encode()), 64)
