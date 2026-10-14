import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

os.environ.setdefault("BOT_TOKEN", "123456:TEST_TOKEN_FOR_UNIT_TESTS")

from data import database  # noqa: E402
import main  # noqa: E402
import reminders_ux  # noqa: E402


class RemindersT18Tests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = os.path.join(self.temp_dir.name, "test_t18.db")
        database.init_db()
        self.now = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)

    def tearDown(self):
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def test_parse_copied_log_prefix(self):
        text = "[09.10.2026 17:57] Карина Кравченко: текст напоминания"
        name, body = reminders_ux.parse_copied_log_prefix(text)
        self.assertEqual(name, "Карина Кравченко")
        self.assertEqual(body, "текст напоминания")

        multiline = "[10.10.2026 09:15] Иван: Первая строка\nВторая строка"
        name, body = reminders_ux.parse_copied_log_prefix(multiline)
        self.assertEqual(name, "Иван")
        self.assertEqual(body, "Первая строка\nВторая строка")

        # Name exceeding 60 chars is not treated as a valid copied log prefix
        long_name_text = f"[09.10.2026 17:57] {'A' * 65}: какой-то текст"
        name, body = reminders_ux.parse_copied_log_prefix(long_name_text)
        self.assertIsNone(name)
        self.assertEqual(body, long_name_text)

        # Non-log text
        plain = "Обычный текст без префикса"
        name, body = reminders_ux.parse_copied_log_prefix(plain)
        self.assertIsNone(name)
        self.assertEqual(body, plain)

    def test_extract_source_url(self):
        # Channel origin with username
        msg_chan = MagicMock()
        msg_chan.forward_origin = MagicMock()
        msg_chan.forward_origin.type = "channel"
        msg_chan.forward_origin.chat = MagicMock()
        msg_chan.forward_origin.chat.username = "unagi_channel"
        msg_chan.forward_origin.message_id = 789
        self.assertEqual(
            reminders_ux.extract_source_url(msg_chan),
            "https://t.me/unagi_channel/789",
        )

        # User origin with id
        msg_user = MagicMock()
        msg_user.forward_origin = MagicMock()
        msg_user.forward_origin.type = "user"
        msg_user.forward_origin.sender_user = MagicMock()
        msg_user.forward_origin.sender_user.id = 555123
        self.assertEqual(
            reminders_ux.extract_source_url(msg_user),
            "tg://user?id=555123",
        )

        # Channel without username
        msg_no_user = MagicMock()
        msg_no_user.forward_origin = MagicMock()
        msg_no_user.forward_origin.type = "channel"
        msg_no_user.forward_origin.chat = MagicMock()
        msg_no_user.forward_origin.chat.username = None
        self.assertIsNone(reminders_ux.extract_source_url(msg_no_user))

        # No origin
        msg_none = MagicMock()
        msg_none.forward_origin = None
        self.assertIsNone(reminders_ux.extract_source_url(msg_none))

    def test_resolve_reminder_display_data(self):
        # When source is 'Твой текст', log prefix overrides source and strips prefix from text
        eff_src, disp_txt, url = reminders_ux.resolve_reminder_display_data(
            source="Твой текст",
            full_text="[09.10.2026 17:57] Карина: Купить продукты",
            text_preview="[09.10.2026 17:57]...",
            source_url="https://t.me/x",
        )
        self.assertEqual(eff_src, "Карина")
        self.assertEqual(disp_txt, "Купить продукты")
        self.assertEqual(url, "https://t.me/x")

        # When source is specific, it is preserved
        eff_src, disp_txt, _ = reminders_ux.resolve_reminder_display_data(
            source="Рабочий чат",
            full_text="[09.10.2026 17:57] Карина: Купить продукты",
            text_preview="...",
        )
        self.assertEqual(eff_src, "Рабочий чат")

    def test_extract_first_line(self):
        text = "\n\n  Первая строка сообщения  \nВторая строка\nТретья"
        self.assertEqual(reminders_ux.extract_first_line(text, 100), "Первая строка сообщения")

        long_line = "A" * 120
        res = reminders_ux.extract_first_line(long_line, 100)
        self.assertTrue(len(res) <= 100)
        self.assertTrue(res.endswith("..."))

    def test_format_button_label(self):
        dt = datetime(2026, 10, 11, 14, 30, tzinfo=timezone.utc)
        label = reminders_ux.format_button_label(dt, "Карина Кравченко", max_len=40)
        self.assertEqual(label, "14:30 · Карина Кравченко")
        self.assertTrue(len(label) <= 40)

        very_long = reminders_ux.format_button_label(dt, "Очень длинный текст сообщения, который не поместится", max_len=40)
        self.assertTrue(len(very_long) <= 40)
        self.assertTrue(very_long.startswith("14:30 · "))

    def test_group_reminders_by_day(self):
        today_dt = self.now + timedelta(hours=2)
        tomorrow_dt = self.now + timedelta(days=1, hours=1)
        future_dt = self.now + timedelta(days=3, hours=4)

        items = [
            {"send_at": database.serialize_datetime(today_dt), "title": "1"},
            {"send_at": database.serialize_datetime(tomorrow_dt), "title": "2"},
            {"send_at": database.serialize_datetime(future_dt), "title": "3"},
        ]

        def dt_getter(it):
            return database.parse_db_datetime(it["send_at"]).replace(tzinfo=timezone.utc)

        groups = reminders_ux.group_reminders_by_day(items, self.now, dt_getter=dt_getter)
        headers = [g[0] for g in groups]
        self.assertEqual(headers[0], "Сегодня")
        self.assertEqual(headers[1], "Завтра")
        self.assertTrue(len(headers[2]) > 0 and headers[2] not in ("Сегодня", "Завтра"))

    def test_render_detail_card_new_and_old_record(self):
        # New record with full_text, note, source_url
        send_at = database.serialize_datetime(self.now + timedelta(hours=3))
        new_row = {
            "id": 1,
            "send_at": send_at,
            "text_preview": "Превью...",
            "source_name": "Канал",
            "tag": "Работа",
            "message_id": 100,
            "full_text": "Полный текст подробного напоминания",
            "note": "Срочно",
            "source_url": "https://t.me/news/100",
        }
        card_html = reminders_ux.render_reminder_detail_text(new_row, self.now)
        self.assertIn("https://t.me/news/100", card_html)
        self.assertIn("Канал", card_html)
        self.assertIn("Полный текст подробного напоминания", card_html)
        self.assertIn("Срочно", card_html)
        self.assertIn("#Работа", card_html)

        # Old record without full_text
        old_row = {
            "id": 2,
            "send_at": send_at,
            "text_preview": "Старый превью",
            "source_name": "Твой текст",
            "tag": None,
            "message_id": 200,
            "full_text": None,
            "note": None,
            "source_url": None,
        }
        old_card_html = reminders_ux.render_reminder_detail_text(old_row, self.now)
        self.assertIn("Старый превью", old_card_html)
        self.assertIn("Полный текст — по кнопке «💬 К сообщению»", old_card_html)

    def test_build_reminder_detail_keyboard(self):
        # With message_id
        kb_with_msg = reminders_ux.build_reminder_detail_keyboard(msg_id=10, has_message_id=True)
        btn_texts_with = [b.text for row in kb_with_msg.inline_keyboard for b in row]
        self.assertIn("💬 К сообщению", btn_texts_with)
        self.assertIn("⏰ Перенести", btn_texts_with)
        self.assertIn("📌 Примечание", btn_texts_with)
        self.assertIn("🏷 Тег", btn_texts_with)
        self.assertIn("📁 В закладки", btn_texts_with)
        self.assertIn("🗑 Удалить", btn_texts_with)
        self.assertIn("🔙 К списку", btn_texts_with)

        # Without message_id
        kb_no_msg = reminders_ux.build_reminder_detail_keyboard(msg_id=10, has_message_id=False)
        btn_texts_no = [b.text for row in kb_no_msg.inline_keyboard for b in row]
        self.assertNotIn("💬 К сообщению", btn_texts_no)
        self.assertIn("⏰ Перенести", btn_texts_no)

    def test_source_url_database_roundtrip(self):
        future = database.serialize_datetime(database.utc_now() + timedelta(days=2))
        url = "https://t.me/mychannel/42"
        row_id = database.add_message(
            user_id=888,
            message_id=42,
            send_at=future,
            text_preview="превью",
            source_name="Канал",
            full_text="полный текст",
            source_url=url,
        )

        row = database.get_reminder_by_id(888, row_id)
        self.assertEqual(row["source_url"], url)

        # Reschedule retains source_url
        database.mark_as_sent(row_id, delivered_message_id=999)
        new_row_id = database.replace_sent_reminder_with_pending(
            888, row_id, message_id=42, send_at=database.serialize_datetime(database.utc_now() + timedelta(days=3))
        )
        rescheduled_row = database.get_reminder_by_id(888, new_row_id)
        self.assertEqual(rescheduled_row["source_url"], url)

        # Export and import preserves source_url
        exported = database.export_user_data(888)
        self.assertEqual(exported["reminders"][0]["source_url"], url)

        main._import_user_payload(889, exported)
        imported_messages = database.get_user_messages(889)
        self.assertEqual(len(imported_messages), 1)
        self.assertEqual(imported_messages[0]["source_url"], url)
