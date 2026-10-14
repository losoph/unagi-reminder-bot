from datetime import datetime, timedelta, timezone
import os
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock

os.environ.setdefault("BOT_TOKEN", "123456:TEST_TOKEN_FOR_UNIT_TESTS")

from aiogram.fsm.context import FSMContext  # noqa: E402
from aiogram.fsm.storage.base import StorageKey  # noqa: E402
from aiogram.fsm.storage.memory import MemoryStorage  # noqa: E402

from channel_source import HybridChannelSource  # noqa: E402
from data import database  # noqa: E402
import feed  # noqa: E402


class FakeDialogClient:
    def __init__(self, dialogs=None, raise_exc=None):
        self.dialogs = dialogs or []
        self.raise_exc = raise_exc
        self.iter_dialogs_called = 0

    async def iter_dialogs(self, limit=300):
        self.iter_dialogs_called += 1
        if self.raise_exc:
            raise self.raise_exc
        for d in self.dialogs[:limit]:
            yield d

    async def iter_messages(self, _username, limit=1000):
        if False:
            yield None


class FeedPersonalChannelsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = os.path.join(self.temp_dir.name, "test_feed_personal.db")
        database.init_db()
        self.storage = MemoryStorage()
        feed._personal_channels_cache.clear()
        feed._backfill_tasks.clear()

    def tearDown(self):
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()
        feed._personal_channels_cache.clear()
        feed._backfill_tasks.clear()

    def _make_fsm_context(self, user_id: int) -> FSMContext:
        key = StorageKey(bot_id=12345, chat_id=user_id, user_id=user_id)
        return FSMContext(storage=self.storage, key=key)

    def _make_callback(self, user_id: int, data: str) -> MagicMock:
        cb = MagicMock()
        cb.from_user.id = user_id
        cb.message = MagicMock()
        cb.message.chat.id = user_id
        cb.message.edit_text = AsyncMock()
        cb.message.edit_reply_markup = AsyncMock()
        cb.message.answer = AsyncMock()
        cb.answer = AsyncMock()
        cb.data = data
        return cb

    def _make_message(self, user_id: int) -> MagicMock:
        msg = MagicMock()
        msg.from_user.id = user_id
        msg.chat.id = user_id
        msg.answer = AsyncMock()
        return msg

    async def test_list_personal_channels_filters_and_sorts(self):
        """list_personal_channels keeps broadcast channels with username, <=30d, unread DESC, max 20."""
        source = HybridChannelSource()
        now = datetime.now(timezone.utc)

        dialogs = [
            # 1. Valid channel, unread=40, active 2 days ago
            SimpleNamespace(
                is_channel=True,
                is_group=False,
                entity=SimpleNamespace(broadcast=True, megagroup=False, username="chan1"),
                title="Channel One",
                unread_count=40,
                date=now - timedelta(days=2),
            ),
            # 2. Valid channel, unread=50, active 5 days ago (should come before chan1 due to unread)
            SimpleNamespace(
                is_channel=True,
                is_group=False,
                entity=SimpleNamespace(broadcast=True, megagroup=False, username="chan2"),
                title="Channel Two",
                unread_count=50,
                date=now - timedelta(days=5),
            ),
            # 3. Group / megagroup (should be filtered out)
            SimpleNamespace(
                is_channel=True,
                is_group=True,
                entity=SimpleNamespace(broadcast=False, megagroup=True, username="group1"),
                title="Group One",
                unread_count=99,
                date=now - timedelta(days=1),
            ),
            # 4. Channel without public username (should be filtered out)
            SimpleNamespace(
                is_channel=True,
                is_group=False,
                entity=SimpleNamespace(broadcast=True, megagroup=False, username=None),
                title="Private Channel",
                unread_count=20,
                date=now - timedelta(days=1),
            ),
            # 5. Old channel (> 30 days, should be filtered out)
            SimpleNamespace(
                is_channel=True,
                is_group=False,
                entity=SimpleNamespace(broadcast=True, megagroup=False, username="old_chan"),
                title="Old Channel",
                unread_count=30,
                date=now - timedelta(days=35),
            ),
            # 6. Already in feed (excluded)
            SimpleNamespace(
                is_channel=True,
                is_group=False,
                entity=SimpleNamespace(broadcast=True, megagroup=False, username="already_in_feed"),
                title="Already In Feed",
                unread_count=15,
                date=now - timedelta(days=1),
            ),
        ]

        # Add 25 more channels to test limit=20
        for i in range(10, 35):
            dialogs.append(
                SimpleNamespace(
                    is_channel=True,
                    is_group=False,
                    entity=SimpleNamespace(broadcast=True, megagroup=False, username=f"extra_{i}"),
                    title=f"Extra {i}",
                    unread_count=i,
                    date=now - timedelta(days=1),
                )
            )

        client = FakeDialogClient(dialogs)
        source._client = client

        result = await source.list_personal_channels(
            limit=20,
            now=now,
            exclude_usernames={"already_in_feed"},
        )

        # Max 20 channels returned
        self.assertEqual(len(result), 20)

        usernames = [r["username"] for r in result]
        self.assertNotIn("group1", usernames)
        self.assertNotIn("old_chan", usernames)
        self.assertNotIn("already_in_feed", usernames)
        self.assertIn("chan2", usernames)
        self.assertIn("chan1", usernames)

        # Check sorting: highest unread_count first
        unread_counts = [r["unread_count"] for r in result]
        self.assertEqual(unread_counts, sorted(unread_counts, reverse=True))
        # chan2 has 50 unread, extra_34 has 34 unread
        self.assertEqual(result[0]["username"], "chan2")

    async def test_stranger_cannot_see_button_or_call_iter_dialogs(self):
        """A user whose user_id != account_id cannot see personal button, nor trigger iter_dialogs."""
        owner_id = 1111
        stranger_id = 9999

        source = HybridChannelSource()
        source.account_id = owner_id
        client = FakeDialogClient([
            SimpleNamespace(
                is_channel=True,
                is_group=False,
                entity=SimpleNamespace(broadcast=True, megagroup=False, username="my_chan"),
                title="My Channel",
                unread_count=5,
                date=datetime.now(timezone.utc) - timedelta(hours=1),
            )
        ])
        source._client = client
        feed.configure(channel_source=source)

        # 1. Stranger sends /feed
        msg = self._make_message(stranger_id)
        state = self._make_fsm_context(stranger_id)
        await feed.cmd_feed(msg, state)

        # Main keyboard was sent to stranger
        msg.answer.assert_called()
        kb = msg.answer.call_args[1]["reply_markup"]
        all_cb = [b.callback_data for row in kb.inline_keyboard for b in row]
        # Must NOT have "feed_personal" button
        self.assertNotIn("feed_personal", all_cb)
        # iter_dialogs MUST NOT have been called!
        self.assertEqual(client.iter_dialogs_called, 0)

        # 2. Stranger tries to call callback "feed_personal" directly
        cb_personal = self._make_callback(stranger_id, "feed_personal")
        await feed.cb_feed_personal(cb_personal, state)
        cb_personal.answer.assert_called_with("Доступно только владельцу сессии.", show_alert=True)
        self.assertEqual(client.iter_dialogs_called, 0)

        # 3. Stranger tries to call "fadd_p:0"
        cb_add = self._make_callback(stranger_id, "fadd_p:0")
        await feed.cb_feed_add_personal(cb_add)
        cb_add.answer.assert_called_with("Доступно только владельцу сессии.", show_alert=True)
        self.assertEqual(client.iter_dialogs_called, 0)
        self.assertEqual(database.count_feed_subscriptions(stranger_id), 0)

    async def test_onboarding_shown_flag_for_owner(self):
        """Owner sees personal onboarding on first open, and normal menu on second open."""
        owner_id = 1111

        source = HybridChannelSource()
        source.account_id = owner_id
        client = FakeDialogClient([
            SimpleNamespace(
                is_channel=True,
                is_group=False,
                entity=SimpleNamespace(broadcast=True, megagroup=False, username="onboard_chan"),
                title="Onboard Channel",
                unread_count=7,
                date=datetime.now(timezone.utc) - timedelta(hours=1),
            )
        ])
        source._client = client
        feed.configure(channel_source=source)

        self.assertFalse(database.get_feed_user_onboarding_shown(owner_id))

        # First /feed open by owner -> triggers onboarding screen
        msg1 = self._make_message(owner_id)
        state1 = self._make_fsm_context(owner_id)
        await feed.cmd_feed(msg1, state1)

        msg1.answer.assert_called()
        text1 = msg1.answer.call_args[0][0]
        self.assertIn("Выбери каналы для Ленты — по одному", text1)
        kb1 = msg1.answer.call_args[1]["reply_markup"]
        all_cb1 = [b.callback_data for row in kb1.inline_keyboard for b in row]
        self.assertIn("fadd_p:0", all_cb1)

        # Flag is now set in DB!
        self.assertTrue(database.get_feed_user_onboarding_shown(owner_id))

        # Second /feed open by owner -> renders standard main screen with "➕ Из моих подписок" button
        msg2 = self._make_message(owner_id)
        state2 = self._make_fsm_context(owner_id)
        await feed.cmd_feed(msg2, state2)

        msg2.answer.assert_called()
        text2 = msg2.answer.call_args[0][0]
        self.assertIn("Лента", text2)
        kb2 = msg2.answer.call_args[1]["reply_markup"]
        all_cb2 = [b.callback_data for row in kb2.inline_keyboard for b in row]
        self.assertIn("feed_personal", all_cb2)
        self.assertIn("feed_period_day", all_cb2)

    async def test_add_personal_channel_by_index(self):
        """Adding channel by index updates DB, schedules backfill, and updates button to ✅."""
        owner_id = 1111

        source = HybridChannelSource()
        source.account_id = owner_id
        client = FakeDialogClient([
            SimpleNamespace(
                is_channel=True,
                is_group=False,
                entity=SimpleNamespace(broadcast=True, megagroup=False, username="target_chan"),
                title="Target Channel",
                unread_count=12,
                date=datetime.now(timezone.utc) - timedelta(hours=1),
            )
        ])
        source._client = client
        feed.configure(channel_source=source)

        # Populate cache
        await feed.render_personal_channels(self._make_message(owner_id), owner_id)

        cb = self._make_callback(owner_id, "fadd_p:0")
        await feed.cb_feed_add_personal(cb)

        cb.answer.assert_called_with("✅ Добавлен в Ленту!")
        cb.message.edit_reply_markup.assert_called()

        # Check DB
        sub = database.get_feed_subscription_by_username(owner_id, "target_chan")
        self.assertIsNotNone(sub)
        self.assertEqual(sub["channel_title"], "Target Channel")

        # Check markup button is now ✅ Target Channel
        markup = cb.message.edit_reply_markup.call_args[1]["reply_markup"]
        first_btn = markup.inline_keyboard[0][0]
        self.assertEqual(first_btn.text, "✅ Target Channel")
        self.assertEqual(first_btn.callback_data, "fadd_p:0")
        self.assertLessEqual(len(first_btn.callback_data.encode()), 64)

    async def test_flood_wait_handling_in_list_personal(self):
        """FloodWaitError during iter_dialogs returns empty list and logs warning."""
        source = HybridChannelSource()
        source._client = FakeDialogClient(raise_exc=RuntimeError("FloodWaitError: simulated"))

        result = await source.list_personal_channels(limit=20)
        self.assertEqual(result, [])

    async def test_caching_personal_channels(self):
        """Channels list is cached for 15 minutes, avoiding repeated iter_dialogs calls."""
        owner_id = 1111

        source = HybridChannelSource()
        source.account_id = owner_id
        client = FakeDialogClient([
            SimpleNamespace(
                is_channel=True,
                is_group=False,
                entity=SimpleNamespace(broadcast=True, megagroup=False, username="cached_chan"),
                title="Cached Channel",
                unread_count=1,
                date=datetime.now(timezone.utc) - timedelta(hours=1),
            )
        ])
        source._client = client
        feed.configure(channel_source=source)

        # Call 1: calls iter_dialogs
        await feed.render_personal_channels(self._make_message(owner_id), owner_id)
        self.assertEqual(client.iter_dialogs_called, 1)

        # Call 2: cache hit, iter_dialogs NOT called again
        await feed.render_personal_channels(self._make_message(owner_id), owner_id)
        self.assertEqual(client.iter_dialogs_called, 1)


if __name__ == "__main__":
    unittest.main()
