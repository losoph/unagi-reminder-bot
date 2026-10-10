import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("BOT_TOKEN", "123456:TEST_TOKEN_FOR_UNIT_TESTS")

from data import database  # noqa: E402
import main  # noqa: E402
import reminders_ux  # noqa: E402


class FollowupRescheduleT17Tests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = os.path.join(self.temp_dir.name, "test_t17.db")
        database.init_db()

    def tearDown(self):
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def _create_delivered_reminder(
        self,
        user_id: int = 12345,
        delivered_at: datetime | None = None,
        delivery_status: str = "sent",
        preview: str = "Купить молоко",
        source: str = "Твой текст",
        full_text: str = "Купить молоко и хлеб",
        note: str = "В лавке",
        delivered_msg_id: int = 777,
    ) -> int:
        base_time = datetime(2026, 10, 11, 10, 0, tzinfo=timezone.utc)
        send_at_str = database.serialize_datetime(base_time)
        rem_id = database.add_message(
            user_id=user_id,
            message_id=100,
            send_at=send_at_str,
            text_preview=preview,
            source_name=source,
            full_text=full_text,
            note=note,
        )
        del_at_str = database.serialize_datetime(delivered_at) if delivered_at else None
        with database.get_connection() as conn:
            conn.execute(
                """
                UPDATE scheduled_messages
                SET is_sent = 1, delivered_message_id = ?, delivery_status = ?, delivered_at = ?
                WHERE id = ?
                """,
                (delivered_msg_id, delivery_status, del_at_str, rem_id),
            )
            conn.commit()
        return rem_id

    def test_get_recent_delivered_reminder_in_and_out_of_window(self):
        """Reminders in window (<=10m) are found; outside window (>10m or <0m) are ignored."""
        ref_now = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)

        # 3 minutes ago -> inside window
        rem_id_in = self._create_delivered_reminder(
            user_id=101,
            delivered_at=ref_now - timedelta(minutes=3),
        )
        cand = database.get_recent_delivered_reminder(user_id=101, window_minutes=10, now=ref_now)
        self.assertIsNotNone(cand)
        self.assertEqual(cand["id"], rem_id_in)

        # 15 minutes ago -> outside window
        self._create_delivered_reminder(
            user_id=102,
            delivered_at=ref_now - timedelta(minutes=15),
        )
        cand_old = database.get_recent_delivered_reminder(user_id=102, window_minutes=10, now=ref_now)
        self.assertIsNone(cand_old)

        # Future delivered_at (clock skew, diff < 0) -> ignored
        self._create_delivered_reminder(
            user_id=103,
            delivered_at=ref_now + timedelta(minutes=2),
        )
        cand_future = database.get_recent_delivered_reminder(user_id=103, window_minutes=10, now=ref_now)
        self.assertIsNone(cand_future)

    def test_candidate_selection_latest_delivered_at(self):
        """Candidate must be the one with the latest delivered_at."""
        ref_now = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
        self._create_delivered_reminder(
            user_id=201,
            delivered_at=ref_now - timedelta(minutes=8),
            preview="Старое напоминание",
        )
        latest_id = self._create_delivered_reminder(
            user_id=201,
            delivered_at=ref_now - timedelta(minutes=2),
            preview="Свежее напоминание",
        )

        cand = database.get_recent_delivered_reminder(user_id=201, window_minutes=10, now=ref_now)
        self.assertIsNotNone(cand)
        self.assertEqual(cand["id"], latest_id)
        self.assertEqual(cand["text_preview"], "Свежее напоминание")

    def test_ignores_done_and_null_delivered_at(self):
        """Records with delivery_status='done' or delivered_at IS NULL are not candidates."""
        ref_now = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)

        # delivery_status='done'
        self._create_delivered_reminder(
            user_id=301,
            delivered_at=ref_now - timedelta(minutes=2),
            delivery_status="done",
        )
        self.assertIsNone(database.get_recent_delivered_reminder(user_id=301, window_minutes=10, now=ref_now))

        # delivered_at IS NULL
        self._create_delivered_reminder(
            user_id=302,
            delivered_at=None,
            delivery_status="sent",
        )
        self.assertIsNone(database.get_recent_delivered_reminder(user_id=302, window_minutes=10, now=ref_now))

    async def test_handle_followup_text_reschedule_success(self):
        """Text representing a future time reschedules the latest reminder and offers undo button."""
        now = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
        rem_id = self._create_delivered_reminder(
            user_id=401,
            delivered_at=now - timedelta(minutes=2),
            preview="Отправить отчёт",
        )

        message = MagicMock()
        message.chat.id = 401
        message.message_id = 999
        message.text = "завтра 11"
        message.reply = AsyncMock()

        rescheduled = await reminders_ux.handle_followup_text_reschedule(message, now=now, window_minutes=10)
        self.assertTrue(rescheduled)

        # Verify old delivered reminder was replaced by a pending reminder
        with database.get_connection() as conn:
            old_row = conn.execute("SELECT * FROM scheduled_messages WHERE id = ?", (rem_id,)).fetchone()
            self.assertIsNone(old_row)

            pending_rows = conn.execute(
                "SELECT * FROM scheduled_messages WHERE user_id = 401 AND delivery_status = 'pending'"
            ).fetchall()
            self.assertEqual(len(pending_rows), 1)
            new_row = pending_rows[0]
            new_id = new_row["id"]
            self.assertEqual(new_row["text_preview"], "Отправить отчёт")

        # Verify message.reply was called with confirmation and undo button
        message.reply.assert_awaited_once()
        args, kwargs = message.reply.call_args
        confirmation_text = args[0]
        self.assertIn("Отложил «Отправить отчёт» на завтра в 11:00", confirmation_text)

        markup = kwargs.get("reply_markup")
        self.assertIsNotNone(markup)
        self.assertEqual(markup.inline_keyboard[0][0].text, "↩️ Нет, это новое")
        self.assertEqual(markup.inline_keyboard[0][0].callback_data, f"undo_resched_{new_id}")

    async def test_handle_followup_text_reschedule_unrecognized_text(self):
        """Text not recognized as a future time is not rescheduled."""
        now = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
        rem_id = self._create_delivered_reminder(
            user_id=501,
            delivered_at=now - timedelta(minutes=2),
        )

        message = MagicMock()
        message.chat.id = 501
        message.message_id = 999
        message.text = "Просто заметка без времени"
        message.reply = AsyncMock()

        rescheduled = await reminders_ux.handle_followup_text_reschedule(message, now=now, window_minutes=10)
        self.assertFalse(rescheduled)
        message.reply.assert_not_called()

        # Reminder must remain untouched
        with database.get_connection() as conn:
            row = conn.execute("SELECT * FROM scheduled_messages WHERE id = ?", (rem_id,)).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["delivery_status"], "sent")

    async def test_catch_message_routing_conditions(self):
        """catch_message skips followup reschedule when forward, reply, or active FSM state."""
        now = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
        self._create_delivered_reminder(
            user_id=601,
            delivered_at=now - timedelta(minutes=1),
        )

        # 1. Forward origin present
        msg_fwd = MagicMock()
        msg_fwd.chat.id = 601
        msg_fwd.text = "завтра 11"
        msg_fwd.forward_origin = MagicMock(type="channel")
        msg_fwd.reply_to_message = None
        msg_fwd.reply = AsyncMock()

        state = MagicMock()
        state.get_state = AsyncMock(return_value=None)
        state.clear = AsyncMock()
        state.update_data = AsyncMock()

        with patch("reminders_ux.handle_followup_text_reschedule", new_callable=AsyncMock) as mock_followup:
            await main.catch_message(msg_fwd, state)
            mock_followup.assert_not_called()

        # 2. Reply to another message present
        msg_reply = MagicMock()
        msg_reply.chat.id = 601
        msg_reply.text = "завтра 11"
        msg_reply.forward_origin = None
        msg_reply.reply_to_message = MagicMock()  # not delivered reminder
        msg_reply.reply = AsyncMock()

        with patch("main.get_replied_sent_schedule", return_value=None):
            with patch("reminders_ux.handle_followup_text_reschedule", new_callable=AsyncMock) as mock_followup:
                await main.catch_message(msg_reply, state)
                mock_followup.assert_not_called()

        # 3. Active FSM state present
        msg_fsm = MagicMock()
        msg_fsm.chat.id = 601
        msg_fsm.text = "завтра 11"
        msg_fsm.forward_origin = None
        msg_fsm.reply_to_message = None
        msg_fsm.reply = AsyncMock()

        state_active = MagicMock()
        state_active.get_state = AsyncMock(return_value="SomeState:active")
        state_active.clear = AsyncMock()

        with patch("reminders_ux.handle_followup_text_reschedule", new_callable=AsyncMock) as mock_followup:
            await main.catch_message(msg_fsm, state_active)
            mock_followup.assert_not_called()

    async def test_undo_followup_reschedule_rollback_and_idempotency(self):
        """Undo restores the original delivered reminder, removes pending, and repeated click is idempotent."""
        now = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
        orig_delivered_at = now - timedelta(minutes=2)
        old_id = self._create_delivered_reminder(
            user_id=701,
            delivered_at=orig_delivered_at,
            preview="Купить билет",
            full_text="Купить билет на поезд",
            note="На пятницу",
            delivered_msg_id=888,
        )

        message = MagicMock()
        message.chat.id = 701
        message.message_id = 955
        message.text = "завтра 14:00"
        message.reply = AsyncMock()

        # 1. Reschedule
        rescheduled = await reminders_ux.handle_followup_text_reschedule(message, now=now, window_minutes=10)
        self.assertTrue(rescheduled)

        with database.get_connection() as conn:
            pending_row = conn.execute(
                "SELECT id FROM scheduled_messages WHERE user_id = 701 AND delivery_status = 'pending'"
            ).fetchone()
            new_id = pending_row["id"]

        # 2. Undo
        success, reason, snapshot = database.undo_followup_reschedule(user_id=701, new_pending_id=new_id)
        self.assertTrue(success)
        self.assertEqual(reason, "ok")
        self.assertEqual(snapshot["old_reminder_id"], old_id)
        self.assertEqual(snapshot["original_text"], "завтра 14:00")
        self.assertEqual(snapshot["original_message_id"], 955)

        # Verify old reminder is restored with delivered status and previous delivered_message_id
        with database.get_connection() as conn:
            restored = conn.execute("SELECT * FROM scheduled_messages WHERE id = ?", (old_id,)).fetchone()
            self.assertIsNotNone(restored)
            self.assertEqual(restored["delivery_status"], "sent")
            self.assertEqual(restored["is_sent"], 1)
            self.assertEqual(restored["delivered_message_id"], 888)
            self.assertEqual(restored["full_text"], "Купить билет на поезд")
            self.assertEqual(restored["note"], "На пятницу")

            # Pending row is removed
            pending_check = conn.execute("SELECT * FROM scheduled_messages WHERE id = ?", (new_id,)).fetchone()
            self.assertIsNone(pending_check)

        # 3. Repeated undo -> idempotent
        success2, reason2, _ = database.undo_followup_reschedule(user_id=701, new_pending_id=new_id)
        self.assertFalse(success2)
        self.assertEqual(reason2, "already_undone")

    async def test_undo_fails_if_reminder_modified_by_other_action(self):
        """Undo fails if pending reminder was deleted or changed before undo."""
        now = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
        self._create_delivered_reminder(
            user_id=801,
            delivered_at=now - timedelta(minutes=2),
            preview="Встреча с клиентом",
        )

        message = MagicMock()
        message.chat.id = 801
        message.message_id = 911
        message.text = "завтра 15:00"
        message.reply = AsyncMock()

        await reminders_ux.handle_followup_text_reschedule(message, now=now, window_minutes=10)

        with database.get_connection() as conn:
            pending_row = conn.execute(
                "SELECT id FROM scheduled_messages WHERE user_id = 801 AND delivery_status = 'pending'"
            ).fetchone()
            new_id = pending_row["id"]

            # User deleted the pending reminder via list
            conn.execute("DELETE FROM scheduled_messages WHERE id = ?", (new_id,))
            conn.commit()

        # Attempt undo
        success, reason, _ = database.undo_followup_reschedule(user_id=801, new_pending_id=new_id)
        self.assertFalse(success)
        self.assertEqual(reason, "already_changed")

    async def test_handle_undo_callback_flow(self):
        """Callback handler edits confirmation markup, sends standard menu in reply to original text."""
        now = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
        self._create_delivered_reminder(
            user_id=901,
            delivered_at=now - timedelta(minutes=1),
            preview="Оплатить интернет",
        )

        msg = MagicMock()
        msg.chat.id = 901
        msg.message_id = 333
        msg.text = "завтра 10:00"
        msg.reply = AsyncMock()

        await reminders_ux.handle_followup_text_reschedule(msg, now=now, window_minutes=10)

        with database.get_connection() as conn:
            pending_row = conn.execute(
                "SELECT id FROM scheduled_messages WHERE user_id = 901 AND delivery_status = 'pending'"
            ).fetchone()
            new_id = pending_row["id"]

        callback = MagicMock()
        callback.data = f"undo_resched_{new_id}"
        callback.from_user.id = 901
        callback.message = MagicMock()
        callback.message.chat.id = 901
        callback.message.edit_reply_markup = AsyncMock()
        callback.answer = AsyncMock()
        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock()
        callback.bot = mock_bot

        await reminders_ux.handle_undo_followup_reschedule(callback)

        callback.answer.assert_awaited_once_with("Перенос отменён.")
        callback.message.edit_reply_markup.assert_awaited_once_with(reply_markup=None)

        # Standard menu sent with reply_to_message_id=333
        mock_bot.send_message.assert_awaited_once()
        s_args, s_kwargs = mock_bot.send_message.call_args
        sent_text = s_args[1] if len(s_args) > 1 else s_kwargs.get("text", "")
        self.assertIn("Что мне сделать с этим сообщением?", sent_text)
        self.assertEqual(s_kwargs.get("reply_to_message_id"), 333)
        self.assertIsNotNone(s_kwargs.get("reply_markup"))
