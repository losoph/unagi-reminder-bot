import os
import sqlite3
import tempfile
import unittest
from datetime import timedelta

os.environ.setdefault("BOT_TOKEN", "123456:TEST_TOKEN_FOR_UNIT_TESTS")

from data import database  # noqa: E402
import main  # noqa: E402


class RemindersT14Tests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = os.path.join(self.temp_dir.name, "test_t14.db")
        database.init_db()

    def tearDown(self):
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def test_migration_from_legacy_schema(self):
        legacy_db_path = os.path.join(self.temp_dir.name, "legacy.db")
        with sqlite3.connect(legacy_db_path) as conn:
            conn.execute(
                """
                CREATE TABLE scheduled_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    message_id INTEGER,
                    delivered_message_id INTEGER,
                    send_at DATETIME,
                    created_at DATETIME,
                    is_sent INTEGER DEFAULT 0,
                    text_preview TEXT,
                    source_name TEXT,
                    delivery_status TEXT DEFAULT 'pending',
                    retry_count INTEGER DEFAULT 0,
                    last_error TEXT,
                    last_attempt_at DATETIME,
                    tag TEXT
                )
                """
            )
            conn.execute(
                """
                INSERT INTO scheduled_messages (user_id, text_preview, send_at, delivery_status)
                VALUES (42, 'Старое превью', '2026-10-10 10:00:00', 'pending')
                """
            )
            conn.commit()

        # Run init_db against legacy DB
        database.DB_PATH = legacy_db_path
        database.init_db()

        with database.get_connection() as conn:
            columns = [row[1] for row in conn.execute("PRAGMA table_info(scheduled_messages)").fetchall()]
            self.assertIn("full_text", columns)
            self.assertIn("note", columns)
            self.assertIn("delivered_at", columns)

            row = conn.execute("SELECT user_id, text_preview, full_text, note, delivered_at FROM scheduled_messages WHERE user_id = 42").fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["text_preview"], "Старое превью")
            self.assertIsNone(row["full_text"])
            self.assertIsNone(row["note"])
            self.assertIsNone(row["delivered_at"])

    def test_add_message_with_full_text_and_note(self):
        now = database.utc_now()
        send_at = database.serialize_datetime(now + timedelta(hours=2))
        full_text = "Очень длинный оригинальный текст напоминания, содержащий множество подробностей."
        note = "Позвонить перед встречей"
        row_id = database.add_message(
            user_id=101,
            message_id=None,
            send_at=send_at,
            text_preview="Очень длинный...",
            source_name="Твой текст",
            tag="Планы",
            full_text=full_text,
            note=note,
        )

        with database.get_connection() as conn:
            row = conn.execute("SELECT * FROM scheduled_messages WHERE id = ?", (row_id,)).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["text_preview"], "Очень длинный...")
            self.assertEqual(row["full_text"], full_text)
            self.assertEqual(row["note"], note)

    def test_add_message_fallback_to_text_preview(self):
        now = database.utc_now()
        send_at = database.serialize_datetime(now + timedelta(hours=1))
        row_id = database.add_message(
            user_id=102,
            message_id=None,
            send_at=send_at,
            text_preview="Обычный текст без full_text",
        )
        with database.get_connection() as conn:
            row = conn.execute("SELECT text_preview, full_text FROM scheduled_messages WHERE id = ?", (row_id,)).fetchone()
            self.assertEqual(row["text_preview"], "Обычный текст без full_text")
            self.assertEqual(row["full_text"], "Обычный текст без full_text")

    def test_mark_as_sent_records_delivered_at(self):
        now = database.utc_now()
        send_at = database.serialize_datetime(now + timedelta(minutes=5))
        row_id = database.add_message(
            user_id=103,
            message_id=55,
            send_at=send_at,
            text_preview="Сообщение для доставки",
        )
        database.mark_as_sent(row_id, delivered_message_id=555)

        with database.get_connection() as conn:
            row = conn.execute("SELECT is_sent, delivered_message_id, delivered_at, delivery_status FROM scheduled_messages WHERE id = ?", (row_id,)).fetchone()
            self.assertEqual(row["is_sent"], 1)
            self.assertEqual(row["delivered_message_id"], 555)
            self.assertEqual(row["delivery_status"], "sent")
            self.assertIsNotNone(row["delivered_at"])

    def test_replace_sent_reminder_transfers_full_text_and_note(self):
        now = database.utc_now()
        send_at = database.serialize_datetime(now + timedelta(hours=1))
        full_text = "Подробное описание задачи с важными деталями."
        note = "Учесть контекст задачи"

        old_row_id = database.add_message(
            user_id=104,
            message_id=None,
            send_at=send_at,
            text_preview="Подробное...",
            source_name="Канал",
            tag="Работа",
            full_text=full_text,
            note=note,
        )
        database.mark_as_sent(old_row_id, delivered_message_id=777)

        new_send_at = database.serialize_datetime(now + timedelta(days=1))
        new_row_id = database.replace_sent_reminder_with_pending(
            user_id=104,
            old_db_id=old_row_id,
            message_id=None,
            send_at=new_send_at,
            text_preview="Подробное...",
            source_name="Канал",
        )

        with database.get_connection() as conn:
            old_row = conn.execute("SELECT * FROM scheduled_messages WHERE id = ?", (old_row_id,)).fetchone()
            self.assertIsNone(old_row)

            new_row = conn.execute("SELECT * FROM scheduled_messages WHERE id = ?", (new_row_id,)).fetchone()
            self.assertIsNotNone(new_row)
            self.assertEqual(new_row["user_id"], 104)
            self.assertEqual(new_row["is_sent"], 0)
            self.assertEqual(new_row["delivery_status"], "pending")
            self.assertEqual(new_row["tag"], "Работа")
            self.assertEqual(new_row["full_text"], full_text)
            self.assertEqual(new_row["note"], note)

    def test_export_import_roundtrip_lossless(self):
        now = database.utc_now()
        future_send_at = database.serialize_datetime(now + timedelta(days=2))
        full_text = "Полный текст напоминания для тестирования экспорта/импорта."
        note = "Заметка к напоминанию"

        database.add_message(
            user_id=200,
            message_id=None,
            send_at=future_send_at,
            text_preview="Короткое превью...",
            source_name="Источник",
            full_text=full_text,
            note=note,
        )

        exported = database.export_user_data(user_id=200)
        self.assertEqual(len(exported["reminders"]), 1)
        rem_dict = exported["reminders"][0]
        self.assertEqual(rem_dict["send_at"], future_send_at)
        self.assertEqual(rem_dict["full_text"], full_text)
        self.assertEqual(rem_dict["note"], note)

        # Import into user 201
        report = main._import_user_payload(user_id=201, payload=exported)
        self.assertIn("Напоминания: +1", report)

        with database.get_connection() as conn:
            imported_row = conn.execute("SELECT * FROM scheduled_messages WHERE user_id = 201").fetchone()
            self.assertIsNotNone(imported_row)
            self.assertEqual(imported_row["full_text"], full_text)
            self.assertEqual(imported_row["note"], note)
            self.assertEqual(imported_row["text_preview"], "Короткое превью...")

    def test_import_fallback_for_legacy_payload(self):
        now = database.utc_now()
        future_send_at = database.serialize_datetime(now + timedelta(days=3))
        legacy_payload = {
            "reminders": [
                {
                    "send_at": future_send_at,
                    "text_preview": "Старый формат только с превью",
                    "source_name": "Архив",
                }
            ]
        }

        report = main._import_user_payload(user_id=300, payload=legacy_payload)
        self.assertIn("Напоминания: +1", report)

        with database.get_connection() as conn:
            row = conn.execute("SELECT * FROM scheduled_messages WHERE user_id = 300").fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["text_preview"], "Старый формат только с превью")
            self.assertEqual(row["full_text"], "Старый формат только с превью")
            self.assertIsNone(row["note"])

    def test_update_reminder_note(self):
        now = database.utc_now()
        send_at = database.serialize_datetime(now + timedelta(hours=1))
        row_id = database.add_message(
            user_id=400,
            message_id=None,
            send_at=send_at,
            text_preview="Тест примечания",
        )
        ok = database.update_reminder_note(user_id=400, reminder_id=row_id, note="Купить хлеб")
        self.assertTrue(ok)

        with database.get_connection() as conn:
            row = conn.execute("SELECT note FROM scheduled_messages WHERE id = ?", (row_id,)).fetchone()
            self.assertEqual(row["note"], "Купить хлеб")

        # Non-existent reminder returns False
        self.assertFalse(database.update_reminder_note(user_id=400, reminder_id=999999, note="Нет такого"))
