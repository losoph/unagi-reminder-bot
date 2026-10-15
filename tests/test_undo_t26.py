import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

os.environ.setdefault("BOT_TOKEN", "123456:TEST_TOKEN_FOR_UNIT_TESTS")

from data import database  # noqa: E402
import reminders_ux  # noqa: E402


class UndoT26Tests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = os.path.join(self.temp_dir.name, "test_t26.db")
        database.init_db()

    def tearDown(self):
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def _create_delivered_reminder(
        self,
        user_id: int = 1001,
        delivered_at: datetime | None = None,
        preview: str = "Купить хлеб",
        full_text: str = "Купить свежий хлеб в пекарне",
        note: str = "Только ржаной",
        tag: str = "покупки",
        source_name: str = "Твой текст",
        source_url: str = "https://t.me/c/123/45",
        delivered_msg_id: int = 555,
    ) -> int:
        base_time = datetime(2026, 10, 11, 10, 0, tzinfo=timezone.utc)
        send_at_str = database.serialize_datetime(base_time)
        rem_id = database.add_message(
            user_id=user_id,
            message_id=101,
            send_at=send_at_str,
            text_preview=preview,
            source_name=source_name,
            full_text=full_text,
            note=note,
        )
        del_at_str = database.serialize_datetime(delivered_at) if delivered_at else None
        with database.get_connection() as conn:
            conn.execute(
                """
                UPDATE scheduled_messages
                SET is_sent = 1,
                    delivered_message_id = ?,
                    delivery_status = 'sent',
                    delivered_at = ?,
                    tag = ?,
                    source_url = ?
                WHERE id = ?
                """,
                (delivered_msg_id, del_at_str, tag, source_url, rem_id),
            )
            conn.commit()
        return rem_id

    # -------------------------------------------------------------------------
    # 1. Cleanup reschedule_undo_history
    # -------------------------------------------------------------------------
    def test_cleanup_reschedule_undo_history_deletes_old_and_preserves_fresh(self):
        ref_now = datetime(2026, 10, 16, 12, 0, 0, tzinfo=timezone.utc)
        now_str = database.serialize_datetime(ref_now)
        base_ts = ref_now.timestamp()

        # Insert 3 entries in reschedule_undo_history:
        # 1. Old (25 hours ago) -> should be deleted
        # 2. Fresh (1 hour ago) -> should stay
        # 3. Missing created_timestamp (None) -> should not be deleted silently
        with database.get_connection() as conn:
            conn.execute(
                """
                INSERT INTO reschedule_undo_history (
                    new_pending_id, user_id, old_reminder_id, text_preview, created_timestamp, is_undone
                ) VALUES (?, ?, ?, ?, ?, 0)
                """,
                (2001, 101, 11, "Старый перенос", base_ts - 25 * 3600),
            )
            conn.execute(
                """
                INSERT INTO reschedule_undo_history (
                    new_pending_id, user_id, old_reminder_id, text_preview, created_timestamp, is_undone
                ) VALUES (?, ?, ?, ?, ?, 0)
                """,
                (2002, 101, 12, "Свежий перенос", base_ts - 1 * 3600),
            )
            conn.execute(
                """
                INSERT INTO reschedule_undo_history (
                    new_pending_id, user_id, old_reminder_id, text_preview, created_timestamp, is_undone
                ) VALUES (?, ?, ?, ?, NULL, 0)
                """,
                (2003, 101, 13, "Без метки времени",),
            )
            conn.commit()

        stats = database.cleanup_old_records(now_str)
        self.assertIn("reschedule_undo_deleted", stats)
        self.assertEqual(stats["reschedule_undo_deleted"], 1)

        with database.get_connection() as conn:
            remaining = conn.execute(
                "SELECT new_pending_id, text_preview, created_timestamp FROM reschedule_undo_history ORDER BY new_pending_id"
            ).fetchall()
            remaining_ids = [r["new_pending_id"] for r in remaining]

        self.assertEqual(remaining_ids, [2002, 2003])
        self.assertEqual(remaining[0]["text_preview"], "Свежий перенос")
        self.assertEqual(remaining[1]["text_preview"], "Без метки времени")

    # -------------------------------------------------------------------------
    # 2. Atomic undo: save snapshot with replace_sent_reminder_with_pending
    # -------------------------------------------------------------------------
    def test_replace_sent_reminder_with_pending_creates_snapshot_and_undo_works(self):
        user_id = 2001
        old_id = self._create_delivered_reminder(
            user_id=user_id,
            delivered_at=datetime(2026, 10, 16, 10, 0, tzinfo=timezone.utc),
            preview="Оплатить интернет",
            full_text="Оплатить интернет 1000 руб",
            note="Картой через приложение",
            tag="счета",
            source_url="https://t.me/c/123/99",
            delivered_msg_id=777,
        )

        new_send_at = database.serialize_datetime(datetime(2026, 10, 17, 10, 0, tzinfo=timezone.utc))
        new_id = database.replace_sent_reminder_with_pending(
            user_id=user_id,
            old_db_id=old_id,
            message_id=888,
            send_at=new_send_at,
            text_preview="Оплатить интернет",
            source_name="Твой текст",
            undo={
                "original_text": "завтра 10:00",
                "original_message_id": 999,
            },
        )
        self.assertGreater(new_id, 0)

        # Verify old row deleted and new pending row created
        with database.get_connection() as conn:
            old_row = conn.execute("SELECT * FROM scheduled_messages WHERE id = ?", (old_id,)).fetchone()
            self.assertIsNone(old_row)

            pending_row = conn.execute("SELECT * FROM scheduled_messages WHERE id = ?", (new_id,)).fetchone()
            self.assertIsNotNone(pending_row)
            self.assertEqual(pending_row["delivery_status"], "pending")
            self.assertEqual(pending_row["is_sent"], 0)
            self.assertEqual(pending_row["tag"], "счета")
            self.assertEqual(pending_row["full_text"], "Оплатить интернет 1000 руб")
            self.assertEqual(pending_row["note"], "Картой через приложение")
            self.assertEqual(pending_row["source_url"], "https://t.me/c/123/99")

            # Verify snapshot was written to reschedule_undo_history
            undo_row = conn.execute(
                "SELECT * FROM reschedule_undo_history WHERE new_pending_id = ? AND user_id = ?",
                (new_id, user_id),
            ).fetchone()
            self.assertIsNotNone(undo_row)
            self.assertEqual(undo_row["old_reminder_id"], old_id)
            self.assertEqual(undo_row["original_text"], "завтра 10:00")
            self.assertEqual(undo_row["original_message_id"], 999)
            self.assertEqual(undo_row["delivered_message_id"], 777)
            self.assertEqual(undo_row["delivery_status"], "sent")
            self.assertEqual(undo_row["tag"], "счета")
            self.assertEqual(undo_row["full_text"], "Оплатить интернет 1000 руб")
            self.assertEqual(undo_row["note"], "Картой через приложение")
            self.assertEqual(undo_row["source_url"], "https://t.me/c/123/99")
            self.assertIsNotNone(undo_row["created_timestamp"])
            self.assertEqual(undo_row["is_undone"], 0)

        # Rollback via undo_followup_reschedule
        success, reason, snapshot = database.undo_followup_reschedule(user_id=user_id, new_pending_id=new_id)
        self.assertTrue(success)
        self.assertEqual(reason, "ok")
        self.assertEqual(snapshot["old_reminder_id"], old_id)

        # Verify old delivered reminder is restored and new pending row is deleted
        with database.get_connection() as conn:
            pending_check = conn.execute("SELECT * FROM scheduled_messages WHERE id = ?", (new_id,)).fetchone()
            self.assertIsNone(pending_check)

            restored_row = conn.execute("SELECT * FROM scheduled_messages WHERE id = ?", (old_id,)).fetchone()
            self.assertIsNotNone(restored_row)
            self.assertEqual(restored_row["is_sent"], 1)
            self.assertEqual(restored_row["delivery_status"], "sent")
            self.assertEqual(restored_row["delivered_message_id"], 777)
            self.assertEqual(restored_row["tag"], "счета")
            self.assertEqual(restored_row["full_text"], "Оплатить интернет 1000 руб")
            self.assertEqual(restored_row["note"], "Картой через приложение")
            self.assertEqual(restored_row["source_url"], "https://t.me/c/123/99")

    # -------------------------------------------------------------------------
    # 3. Snapshot failure rolls back whole transaction
    # -------------------------------------------------------------------------
    def test_replace_sent_reminder_with_pending_rolls_back_on_snapshot_failure(self):
        user_id = 3001
        old_id = self._create_delivered_reminder(
            user_id=user_id,
            delivered_at=datetime(2026, 10, 16, 11, 0, tzinfo=timezone.utc),
            preview="Важная задача",
            full_text="Полный текст важной задачи",
        )

        # Create a trigger on reschedule_undo_history that fails on insert to simulate error
        with database.get_connection() as conn:
            conn.execute(
                """
                CREATE TRIGGER fail_snapshot_insert
                BEFORE INSERT ON reschedule_undo_history
                BEGIN
                    SELECT RAISE(ABORT, 'forced snapshot failure');
                END;
                """
            )
            conn.commit()

        new_send_at = database.serialize_datetime(datetime(2026, 10, 17, 11, 0, tzinfo=timezone.utc))

        with self.assertRaises(Exception):
            database.replace_sent_reminder_with_pending(
                user_id=user_id,
                old_db_id=old_id,
                message_id=888,
                send_at=new_send_at,
                text_preview="Важная задача",
                undo={"original_text": "завтра 11", "original_message_id": 111},
            )

        # Verify atomic rollback: old row is intact, no new pending row created
        with database.get_connection() as conn:
            old_row = conn.execute("SELECT * FROM scheduled_messages WHERE id = ?", (old_id,)).fetchone()
            self.assertIsNotNone(old_row)
            self.assertEqual(old_row["is_sent"], 1)
            self.assertEqual(old_row["delivery_status"], "sent")
            self.assertEqual(old_row["text_preview"], "Важная задача")

            pending_rows = conn.execute(
                "SELECT * FROM scheduled_messages WHERE user_id = ? AND delivery_status = 'pending'",
                (user_id,),
            ).fetchall()
            self.assertEqual(len(pending_rows), 0)

            undo_rows = conn.execute(
                "SELECT * FROM reschedule_undo_history WHERE user_id = ?",
                (user_id,),
            ).fetchall()
            self.assertEqual(len(undo_rows), 0)

    # -------------------------------------------------------------------------
    # 4. replace_sent_reminder_with_pending without undo preserves existing behavior
    # -------------------------------------------------------------------------
    def test_replace_sent_reminder_with_pending_without_undo_leaves_history_untouched(self):
        user_id = 4001
        old_id = self._create_delivered_reminder(
            user_id=user_id,
            delivered_at=datetime(2026, 10, 16, 9, 0, tzinfo=timezone.utc),
            preview="Обычный перенос",
        )

        new_send_at = database.serialize_datetime(datetime(2026, 10, 17, 9, 0, tzinfo=timezone.utc))
        new_id = database.replace_sent_reminder_with_pending(
            user_id=user_id,
            old_db_id=old_id,
            message_id=222,
            send_at=new_send_at,
            text_preview="Обычный перенос",
        )
        self.assertGreater(new_id, 0)

        with database.get_connection() as conn:
            old_row = conn.execute("SELECT * FROM scheduled_messages WHERE id = ?", (old_id,)).fetchone()
            self.assertIsNone(old_row)

            pending_row = conn.execute("SELECT * FROM scheduled_messages WHERE id = ?", (new_id,)).fetchone()
            self.assertIsNotNone(pending_row)
            self.assertEqual(pending_row["delivery_status"], "pending")

            undo_rows = conn.execute(
                "SELECT * FROM reschedule_undo_history WHERE user_id = ?",
                (user_id,),
            ).fetchall()
            self.assertEqual(len(undo_rows), 0)

    # -------------------------------------------------------------------------
    # 5. UX flow in reminders_ux uses atomic undo
    # -------------------------------------------------------------------------
    async def test_reminders_ux_followup_reschedule_integrates_atomic_undo(self):
        user_id = 5001
        ref_now = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)
        old_id = self._create_delivered_reminder(
            user_id=user_id,
            delivered_at=ref_now - timedelta(minutes=3),
            preview="Презентация",
        )

        message = MagicMock()
        message.chat.id = user_id
        message.message_id = 444
        message.text = "завтра 14:00"
        message.reply = AsyncMock()

        rescheduled = await reminders_ux.handle_followup_text_reschedule(message, now=ref_now, window_minutes=10)
        self.assertTrue(rescheduled)

        # Check pending reminder was created and old removed
        with database.get_connection() as conn:
            old_row = conn.execute("SELECT * FROM scheduled_messages WHERE id = ?", (old_id,)).fetchone()
            self.assertIsNone(old_row)

            pending = conn.execute(
                "SELECT * FROM scheduled_messages WHERE user_id = ? AND delivery_status = 'pending'",
                (user_id,),
            ).fetchone()
            self.assertIsNotNone(pending)
            new_id = pending["id"]

            undo_entry = conn.execute(
                "SELECT * FROM reschedule_undo_history WHERE new_pending_id = ? AND user_id = ?",
                (new_id, user_id),
            ).fetchone()
            self.assertIsNotNone(undo_entry)
            self.assertEqual(undo_entry["original_text"], "завтра 14:00")
            self.assertEqual(undo_entry["original_message_id"], 444)

        # Undo via callback/function works
        success, reason, snapshot = database.undo_followup_reschedule(user_id=user_id, new_pending_id=new_id)
        self.assertTrue(success)
        self.assertEqual(reason, "ok")
        self.assertEqual(snapshot["old_reminder_id"], old_id)


if __name__ == "__main__":
    unittest.main()
