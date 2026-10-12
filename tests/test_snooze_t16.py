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


class SnoozeT16Tests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = os.path.join(self.temp_dir.name, "test_t16.db")
        database.init_db()

    def tearDown(self):
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def test_delivered_keyboard_layout_before_evening(self):
        """When evening is still ahead, row 1 has 3 buttons (+3 ч, Вечером, Завтра утром)."""
        tz = ZoneInfo("Europe/Moscow")
        now = datetime(2026, 6, 15, 14, 0, 0, tzinfo=tz)
        kb = reminders_ux.build_sent_reminder_actions_keyboard(now=now, evening_hour=20)

        # Row 1
        self.assertEqual(len(kb.inline_keyboard[0]), 3)
        self.assertEqual(kb.inline_keyboard[0][0].text, "+3 ч")
        self.assertEqual(kb.inline_keyboard[0][0].callback_data, "snooze_3h")
        self.assertEqual(kb.inline_keyboard[0][1].text, "Вечером")
        self.assertEqual(kb.inline_keyboard[0][1].callback_data, "snooze_evening")
        self.assertEqual(kb.inline_keyboard[0][2].text, "Завтра утром")
        self.assertEqual(kb.inline_keyboard[0][2].callback_data, "snooze_tomorrow_morning")

        # Row 2
        self.assertEqual(len(kb.inline_keyboard[1]), 2)
        self.assertEqual(kb.inline_keyboard[1][0].text, "⏰ Другое время")
        self.assertEqual(kb.inline_keyboard[1][0].callback_data, "sent_later")
        self.assertEqual(kb.inline_keyboard[1][1].text, "✅ Готово")
        self.assertEqual(kb.inline_keyboard[1][1].callback_data, "sent_done")

        # Row 3
        self.assertEqual(len(kb.inline_keyboard[2]), 2)
        self.assertEqual(kb.inline_keyboard[2][0].text, "📁 Сохранить")
        self.assertEqual(kb.inline_keyboard[2][0].callback_data, "sent_save")
        self.assertEqual(kb.inline_keyboard[2][1].text, "🗑 Удалить")
        self.assertEqual(kb.inline_keyboard[2][1].callback_data, "sent_delete")

    def test_delivered_keyboard_layout_after_evening(self):
        """When evening has passed, 'Вечером' is omitted from row 1."""
        tz = ZoneInfo("Europe/Moscow")
        now = datetime(2026, 6, 15, 21, 30, 0, tzinfo=tz)
        kb = reminders_ux.build_sent_reminder_actions_keyboard(now=now, evening_hour=20)

        # Row 1 has only 2 buttons: +3 ч and Завтра утром
        self.assertEqual(len(kb.inline_keyboard[0]), 2)
        self.assertEqual(kb.inline_keyboard[0][0].text, "+3 ч")
        self.assertEqual(kb.inline_keyboard[0][0].callback_data, "snooze_3h")
        self.assertEqual(kb.inline_keyboard[0][1].text, "Завтра утром")
        self.assertEqual(kb.inline_keyboard[0][1].callback_data, "snooze_tomorrow_morning")

    async def test_snooze_1h(self):
        """Legacy «+1 ч» button on old messages still reschedules by 1 hour."""
        chat_id = 111222
        user_id = chat_id
        db_id = database.add_message(
            user_id=chat_id,
            message_id=10,
            send_at="2026-06-15 10:00:00",
            text_preview="Купить молоко",
            full_text="Купить молоко и хлеб",
        )
        delivered_msg_id = 901
        database.mark_as_sent(db_id, delivered_msg_id)

        tz = ZoneInfo("Europe/Moscow")
        mock_now = datetime(2026, 6, 15, 14, 15, 0, tzinfo=tz)

        cb = MagicMock()
        cb.from_user.id = user_id
        cb.message.chat.id = chat_id
        cb.message.message_id = delivered_msg_id
        cb.message.delete = AsyncMock()
        cb.answer = AsyncMock()
        cb.data = "snooze_1h"

        with patch("main.user_now", return_value=mock_now), \
             patch.object(main.bot, "delete_message", new_callable=AsyncMock) as mock_del, \
             patch.object(main.bot, "send_message", new_callable=AsyncMock) as mock_send:
            await main.handle_sent_snooze(cb)
            mock_del.assert_called()
            mock_send.assert_called()
            send_args = mock_send.call_args[0]
            self.assertIn("15:15", send_args[1])
            cb.answer.assert_called()

        # Original row is deleted, new pending exists
        with database.get_connection() as conn:
            old_row = conn.execute("SELECT * FROM scheduled_messages WHERE id = ?", (db_id,)).fetchone()
            self.assertIsNone(old_row)
            new_rows = conn.execute(
                "SELECT send_at, delivery_status, full_text FROM scheduled_messages WHERE user_id = ? AND is_sent = 0",
                (chat_id,),
            ).fetchall()
            self.assertEqual(len(new_rows), 1)
            # 15:15 Moscow (UTC+3) is 12:15 UTC
            self.assertEqual(new_rows[0][0], "2026-06-15 12:15:00")
            self.assertEqual(new_rows[0][1], "pending")
            self.assertEqual(new_rows[0][2], "Купить молоко и хлеб")

    async def test_snooze_3h(self):
        """Snooze +3 ч reschedules by 3 hours."""
        chat_id = 111333
        db_id = database.add_message(
            user_id=chat_id,
            message_id=11,
            send_at="2026-06-15 10:00:00",
            text_preview="Позвонить",
            full_text="Позвонить в банк",
        )
        database.mark_as_sent(db_id, 902)
        mock_now = datetime(2026, 6, 15, 14, 15, 0, tzinfo=ZoneInfo("Europe/Moscow"))

        cb = MagicMock()
        cb.from_user.id = chat_id
        cb.message.chat.id = chat_id
        cb.message.message_id = 902
        cb.answer = AsyncMock()
        cb.data = "snooze_3h"

        with patch("main.user_now", return_value=mock_now), \
             patch.object(main.bot, "delete_message", new_callable=AsyncMock), \
             patch.object(main.bot, "send_message", new_callable=AsyncMock) as mock_send:
            await main.handle_sent_snooze(cb)
            self.assertIn("17:15", mock_send.call_args[0][1])

        with database.get_connection() as conn:
            rows = conn.execute(
                "SELECT send_at FROM scheduled_messages WHERE user_id = ? AND is_sent = 0", (chat_id,)
            ).fetchall()
        # 17:15 Moscow (UTC+3) is 14:15 UTC
        self.assertEqual([r[0] for r in rows], ["2026-06-15 14:15:00"])

    async def test_snooze_evening_with_user_prefs(self):
        """Snooze evening uses user's custom evening hour and timezone."""
        chat_id = 333444
        database.upsert_user_prefs(
            chat_id,
            timezone="Asia/Novosibirsk",
            morning_hour=7,
            day_hour=13,
            evening_hour=21,
        )
        db_id = database.add_message(
            user_id=chat_id,
            message_id=20,
            send_at="2026-06-15 05:00:00",
            text_preview="Встреча",
        )
        delivered_msg_id = 902
        database.mark_as_sent(db_id, delivered_msg_id)

        novosibirsk_tz = ZoneInfo("Asia/Novosibirsk")  # UTC+7
        mock_now = datetime(2026, 6, 15, 16, 0, 0, tzinfo=novosibirsk_tz)

        cb = MagicMock()
        cb.from_user.id = chat_id
        cb.message.chat.id = chat_id
        cb.message.message_id = delivered_msg_id
        cb.message.delete = AsyncMock()
        cb.answer = AsyncMock()
        cb.data = "snooze_evening"

        with patch("main.user_now", return_value=mock_now), \
             patch.object(main.bot, "delete_message", new_callable=AsyncMock), \
             patch.object(main.bot, "send_message", new_callable=AsyncMock) as mock_send:
            await main.handle_sent_snooze(cb)
            mock_send.assert_called()
            self.assertIn("21:00", mock_send.call_args[0][1])

        # 21:00 UTC+7 is 14:00 UTC
        with database.get_connection() as conn:
            new_rows = conn.execute(
                "SELECT send_at FROM scheduled_messages WHERE user_id = ? AND is_sent = 0",
                (chat_id,),
            ).fetchall()
            self.assertEqual(len(new_rows), 1)
            self.assertEqual(new_rows[0][0], "2026-06-15 14:00:00")

    async def test_snooze_tomorrow_morning_with_user_prefs(self):
        """Snooze tomorrow morning uses user's custom morning hour."""
        chat_id = 555666
        database.upsert_user_prefs(
            chat_id,
            timezone="Europe/Kaliningrad",  # UTC+2
            morning_hour=8,
            day_hour=14,
            evening_hour=20,
        )
        db_id = database.add_message(
            user_id=chat_id,
            message_id=30,
            send_at="2026-06-15 10:00:00",
            text_preview="Проверить отчёт",
        )
        delivered_msg_id = 903
        database.mark_as_sent(db_id, delivered_msg_id)

        kaliningrad_tz = ZoneInfo("Europe/Kaliningrad")
        mock_now = datetime(2026, 6, 15, 22, 0, 0, tzinfo=kaliningrad_tz)

        cb = MagicMock()
        cb.from_user.id = chat_id
        cb.message.chat.id = chat_id
        cb.message.message_id = delivered_msg_id
        cb.message.delete = AsyncMock()
        cb.answer = AsyncMock()
        cb.data = "snooze_tomorrow_morning"

        with patch("main.user_now", return_value=mock_now), \
             patch.object(main.bot, "delete_message", new_callable=AsyncMock), \
             patch.object(main.bot, "send_message", new_callable=AsyncMock) as mock_send:
            await main.handle_sent_snooze(cb)
            mock_send.assert_called()
            self.assertIn("08:00", mock_send.call_args[0][1])

        # 16th 08:00 UTC+2 is 16th 06:00 UTC
        with database.get_connection() as conn:
            new_rows = conn.execute(
                "SELECT send_at FROM scheduled_messages WHERE user_id = ? AND is_sent = 0",
                (chat_id,),
            ).fetchall()
            self.assertEqual(len(new_rows), 1)
            self.assertEqual(new_rows[0][0], "2026-06-16 06:00:00")

    async def test_sent_done_marks_status_removes_keyboard_preserves_record(self):
        """'✅ Готово' removes markup, sets delivery_status='done', does not delete row."""
        chat_id = 777888
        db_id = database.add_message(
            user_id=chat_id,
            message_id=40,
            send_at="2026-06-15 10:00:00",
            text_preview="Сделать зарядку",
        )
        delivered_msg_id = 904
        database.mark_as_sent(db_id, delivered_msg_id)

        cb = MagicMock()
        cb.from_user.id = chat_id
        cb.message.chat.id = chat_id
        cb.message.message_id = delivered_msg_id
        cb.message.edit_reply_markup = AsyncMock()
        cb.answer = AsyncMock()
        cb.data = "sent_done"

        await main.handle_sent_reminder_done(cb)
        cb.message.edit_reply_markup.assert_called_with(reply_markup=None)
        cb.answer.assert_called_with("Готово")

        # Row remains in database, with delivery_status='done'
        with database.get_connection() as conn:
            row = conn.execute(
                "SELECT delivery_status, is_sent FROM scheduled_messages WHERE id = ?",
                (db_id,),
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], "done")
            self.assertEqual(row[1], 1)
