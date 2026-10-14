import asyncio
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("BOT_TOKEN", "123456:TEST_TOKEN_FOR_UNIT_TESTS")

from aiogram import types  # noqa: E402
from data import database  # noqa: E402
import main  # noqa: E402
import reminders_ux  # noqa: E402


class RemindersT14bReviewFixTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = os.path.join(self.temp_dir.name, "test_t14b.db")
        database.init_db()

    def tearDown(self):
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def test_guard_no_main_imports_in_reminders_ux(self):
        """Guard test: reminders_ux must never import main."""
        current_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        reminders_ux_path = os.path.join(current_dir, "reminders_ux.py")
        with open(reminders_ux_path, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertNotIn("from main import", content)
        self.assertNotIn("import main", content)

    async def test_dp_routing_note_state_invokes_note_handler_not_catch_message(self):
        """A text message in ReminderNoteState.waiting_for_note must be routed to process_reminder_note,
        not intercepted by catch_message."""
        chat_id = 998877
        user_id = 998877
        rem_id = database.add_message(
            user_id=chat_id,
            message_id=123,
            send_at=(datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S"),
            text_preview="Задача для теста",
            full_text="Полный текст задачи для теста",
        )

        mock_bot = AsyncMock()
        mock_bot.id = 12345
        mock_bot.send_message = AsyncMock()

        fsm_ctx = main.dp.fsm.get_context(bot=mock_bot, chat_id=chat_id, user_id=user_id)
        await fsm_ctx.set_state(reminders_ux.ReminderNoteState.waiting_for_note)
        await fsm_ctx.update_data(note_rem_id=rem_id, note_from_detail=False)

        user = types.User(id=user_id, is_bot=False, first_name="Tester")
        chat = types.Chat(id=chat_id, type="private")
        message = types.Message(
            message_id=555,
            date=datetime.now(timezone.utc),
            chat=chat,
            from_user=user,
            text="Важное примечание к задаче",
        )
        update = types.Update(update_id=1, message=message)

        with patch.object(main, "catch_message", new_callable=AsyncMock) as mock_catch:
            await main.dp.feed_update(mock_bot, update)
            # catch_message must NOT be called
            mock_catch.assert_not_called()

        # FSM state should now be cleared
        current_state = await fsm_ctx.get_state()
        self.assertIsNone(current_state)

        # Note in database should be updated
        row = database.get_reminder_by_id(chat_id, rem_id)
        self.assertIsNotNone(row)
        self.assertEqual(row["note"], "Важное примечание к задаче")

    async def test_check_messages_escapes_html(self):
        """Text without message_id containing HTML special chars must be escaped when sent with parse_mode=HTML."""
        user_id = 777666
        raw_text = "<b>Текст с тегами & <спецсимволами></b>"
        source_name = "Канал & Автор"
        rem_id = database.add_message(
            user_id=user_id,
            message_id=None,
            send_at=(datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S"),
            text_preview=raw_text[:37],
            source_name=source_name,
            full_text=raw_text,
        )

        mock_sent = MagicMock()
        mock_sent.message_id = 888

        with patch.object(main.bot, "send_message", new_callable=AsyncMock) as mock_send, \
             patch("asyncio.sleep", side_effect=asyncio.CancelledError):
            mock_send.return_value = mock_sent
            try:
                await main.check_messages()
            except asyncio.CancelledError:
                pass

            mock_send.assert_called_once()
            _, kwargs = mock_send.call_args
            self.assertEqual(kwargs.get("chat_id"), user_id)
            self.assertEqual(kwargs.get("parse_mode"), "HTML")
            text_sent = kwargs.get("text", "")
            # Must contain escaped html entities
            self.assertIn("&lt;b&gt;Текст с тегами &amp; &lt;спецсимволами&gt;&lt;/b&gt;", text_sent)
            self.assertIn("Канал &amp; Автор", text_sent)
            # Must NOT contain raw unescaped <спецсимволами>
            self.assertNotIn("<спецсимволами>", text_sent)

        with database.get_connection() as conn:
            row = conn.execute("SELECT delivery_status FROM scheduled_messages WHERE id = ?", (rem_id,)).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["delivery_status"], "sent")

    async def test_handle_time_selection_attaches_note_button_and_does_not_delete(self):
        """Quick schedule confirmation must attach [📝 Примечание] button and not auto-delete after 3s."""
        chat_id = 111222
        user_id = 111222
        user = types.User(id=user_id, is_bot=False, first_name="Tester")
        chat = types.Chat(id=chat_id, type="private")

        orig_msg = types.Message(
            message_id=10,
            date=datetime.now(timezone.utc),
            chat=chat,
            from_user=user,
            text="Текст сообщения для напоминания",
        )
        bot_msg = AsyncMock()
        bot_msg.message_id = 20
        bot_msg.chat.id = chat_id
        bot_msg.reply_to_message = orig_msg
        bot_msg.edit_text = AsyncMock()
        bot_msg.delete = AsyncMock()

        callback = AsyncMock()
        callback.data = "time_hour"
        callback.message = bot_msg

        mock_state = AsyncMock()
        mock_state.get_data.return_value = {
            "orig_msg": orig_msg,
            "preview": "Текст сообщения для напоминания",
            "source": "Твой текст",
            "full_text": "Текст сообщения для напоминания",
            "source_url": None,
        }

        await main.handle_time_selection(callback, mock_state)

        bot_msg.edit_text.assert_called_once()
        _, kwargs = bot_msg.edit_text.call_args
        reply_markup = kwargs.get("reply_markup")
        self.assertIsNotNone(reply_markup)
        # Button must be 📝 Примечание
        self.assertTrue(any("📝 Примечание" in btn.text for row in reply_markup.inline_keyboard for btn in row))
        # Must not be deleted
        bot_msg.delete.assert_not_called()

    async def test_complete_manual_schedule_attaches_note_button_and_does_not_delete(self):
        """Manual schedule confirmation must attach [📝 Примечание] button and not auto-delete."""
        chat_id = 333444
        target_msg = AsyncMock()

        mock_state = AsyncMock()
        mock_state.get_data.return_value = {
            "message_id": 15,
            "preview": "Ручное напоминание",
            "source": "Твой текст",
            "full_text": "Ручное напоминание",
            "source_url": None,
        }

        future_dt = datetime.now(timezone.utc) + timedelta(days=1)

        with patch.object(main.bot, "send_message", new_callable=AsyncMock) as mock_send, \
             patch.object(main.bot, "delete_message", new_callable=AsyncMock):
            mock_confirm_msg = AsyncMock()
            mock_send.return_value = mock_confirm_msg

            await main.complete_manual_schedule(
                chat_id=chat_id,
                state=mock_state,
                scheduled_time=future_dt,
                reply_target=target_msg,
            )

            mock_send.assert_called_once()
            _, kwargs = mock_send.call_args
            reply_markup = kwargs.get("reply_markup")
            self.assertIsNotNone(reply_markup)
            self.assertTrue(any("📝 Примечание" in btn.text for row in reply_markup.inline_keyboard for btn in row))
            mock_confirm_msg.delete.assert_not_called()
