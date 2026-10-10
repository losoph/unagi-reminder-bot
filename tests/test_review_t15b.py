import os
import tempfile
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("BOT_TOKEN", "123456:TEST_TOKEN_FOR_UNIT_TESTS")

from data import database  # noqa: E402
import main  # noqa: E402
import reminders_ux  # noqa: E402
from time_utils import parse_timezone  # noqa: E402


class ReviewT15bTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = os.path.join(self.temp_dir.name, "test_t15b.db")
        database.init_db()

    def tearDown(self):
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def test_safe_source_url_whitelist(self):
        """Item 1: Whitelist allows only https://t.me/... and tg://user?id=<digits>."""
        self.assertTrue(reminders_ux.is_safe_source_url("https://t.me/durov/123"))
        self.assertTrue(reminders_ux.is_safe_source_url("https://t.me/c/123456/789"))
        self.assertTrue(reminders_ux.is_safe_source_url("tg://user?id=123456789"))

        self.assertFalse(reminders_ux.is_safe_source_url("javascript:alert(1)"))
        self.assertFalse(reminders_ux.is_safe_source_url("http://evil.com/phishing"))
        self.assertFalse(reminders_ux.is_safe_source_url("https://not-telegram.org/post"))
        self.assertFalse(reminders_ux.is_safe_source_url("tg://user?id=not_digits"))
        self.assertFalse(reminders_ux.is_safe_source_url(None))
        self.assertFalse(reminders_ux.is_safe_source_url(""))

    def test_card_open_in_channel_button(self):
        """Item 1: Detail card adds '↗️ Открыть в канале' URL button for https://t.me/..."""
        kb_channel = reminders_ux.build_reminder_detail_keyboard(
            msg_id=101,
            has_message_id=True,
            source_url="https://t.me/my_channel/55",
        )
        first_row = kb_channel.inline_keyboard[0]
        self.assertEqual(len(first_row), 2)
        self.assertEqual(first_row[0].text, "💬 К сообщению")
        self.assertEqual(first_row[1].text, "↗️ Открыть в канале")
        self.assertEqual(first_row[1].url, "https://t.me/my_channel/55")

        # Without channel url
        kb_no_channel = reminders_ux.build_reminder_detail_keyboard(
            msg_id=102,
            has_message_id=True,
            source_url="tg://user?id=999",
        )
        first_row_no_channel = kb_no_channel.inline_keyboard[0]
        self.assertEqual(len(first_row_no_channel), 1)
        self.assertEqual(first_row_no_channel[0].text, "💬 К сообщению")

    def test_calendar_date_day_grouping(self):
        """Item 2: Reminders are grouped by calendar date in user's timezone."""
        tz = ZoneInfo("Asia/Novosibirsk")
        now = datetime(2026, 6, 15, 12, 0, 0, tzinfo=tz)

        items = [
            {"id": 1, "send_at": "2026-06-15 03:00:00"},  # 10:00 local (2026-06-15)
            {"id": 2, "send_at": "2026-06-15 07:00:00"},  # 14:00 local (2026-06-15)
            {"id": 3, "send_at": "2026-06-16 02:00:00"},  # 09:00 local (2026-06-16, tomorrow)
            {"id": 4, "send_at": "2027-06-15 03:00:00"},  # Next year 2027
        ]

        groups = reminders_ux.group_reminders_by_day(items, now)
        self.assertEqual(len(groups), 3)

        header1, items1 = groups[0]
        self.assertEqual(header1, "Сегодня")
        self.assertEqual(len(items1), 2)

        header2, items2 = groups[1]
        self.assertEqual(header2, "Завтра")
        self.assertEqual(len(items2), 1)

        header3, items3 = groups[2]
        self.assertIn("2027", header3)
        self.assertEqual(len(items3), 1)

    async def test_bksched_choice_attaches_note_button(self):
        """Item 3: Moving bookmark to reminders attaches [📝 Примечание] button."""
        user_id = 111333
        saved_id = database.add_saved_message(
            user_id,
            "Важная заметка",
            "Канал",
            "работа",
            "2026-06-15 09:00:00",
        )

        mock_now = datetime(2026, 6, 15, 10, 0, 0, tzinfo=main.TZ)

        cb = MagicMock()
        cb.from_user.id = user_id
        cb.message.chat.id = user_id
        cb.message.message_id = 999
        cb.message.delete = AsyncMock()
        cb.answer = AsyncMock()
        cb.data = f"bksched_choice_{saved_id}_evening"

        state = MagicMock()
        state.clear = AsyncMock()
        state.update_data = AsyncMock()
        state.get_data = AsyncMock(return_value={})
        state.set_data = AsyncMock()

        with patch("main.user_now", return_value=mock_now), \
             patch.object(main.bot, "send_message", new_callable=AsyncMock) as mock_send, \
             patch("main.render_screen", new_callable=AsyncMock):
            await main.handle_bksched_choice(cb, state)
            mock_send.assert_called()
            send_kwargs = mock_send.call_args[1]
            self.assertIsNotNone(send_kwargs.get("reply_markup"))
            kb = send_kwargs["reply_markup"]
            self.assertEqual(kb.inline_keyboard[0][0].text, "📝 Примечание")

        # Bookmark deleted, reminder created
        self.assertIsNone(database.get_saved_message_by_id(user_id, saved_id))
        user_msgs = database.get_user_messages(user_id)
        self.assertEqual(len(user_msgs), 1)

    def test_parse_timezone_offset_boundaries(self):
        """Item 4: Offsets must be strictly within -12:00 to +14:00, minutes 00-59."""
        # Valid maximum and minimum
        tz, name = parse_timezone("UTC+14:00")
        self.assertIsNotNone(tz)
        self.assertEqual(name, "UTC+14:00")

        tz, name = parse_timezone("+14")
        self.assertIsNotNone(tz)
        self.assertEqual(name, "UTC+14:00")

        tz, name = parse_timezone("UTC-12:00")
        self.assertIsNotNone(tz)
        self.assertEqual(name, "UTC-12:00")

        tz, name = parse_timezone("-12")
        self.assertIsNotNone(tz)
        self.assertEqual(name, "UTC-12:00")

        # Whitespace stripping
        tz, name = parse_timezone("   +03:00   ")
        self.assertIsNotNone(tz)
        self.assertEqual(name, "UTC+03:00")

        # Invalid: > +14:00
        self.assertEqual(parse_timezone("UTC+14:01"), (None, None))
        self.assertEqual(parse_timezone("UTC+14:59"), (None, None))
        self.assertEqual(parse_timezone("UTC+15:00"), (None, None))

        # Invalid: < -12:00
        self.assertEqual(parse_timezone("UTC-12:01"), (None, None))
        self.assertEqual(parse_timezone("UTC-13:00"), (None, None))

        # Invalid: minutes >= 60
        self.assertEqual(parse_timezone("UTC+3:60"), (None, None))

    async def test_repeated_snooze_answers_cleanly(self):
        """Clarification T16: Repeated tap on already snoozed reminder answers cleanly."""
        user_id = 999888
        cb = MagicMock()
        cb.from_user.id = user_id
        cb.message.chat.id = user_id
        cb.message.message_id = 777
        cb.data = "snooze_1h"
        cb.answer = AsyncMock()

        # No scheduled message exists for msg_id 777
        await main.handle_sent_snooze(cb)
        cb.answer.assert_called_with("Напоминание уже перенесено.")
