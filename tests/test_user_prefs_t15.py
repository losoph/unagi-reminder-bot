import os
import tempfile
import unittest
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from unittest.mock import AsyncMock, patch

os.environ.setdefault("BOT_TOKEN", "123456:TEST_TOKEN_FOR_UNIT_TESTS")

from aiogram import types  # noqa: E402
from data import database  # noqa: E402
import main  # noqa: E402
from time_parser import parse_reminder_time  # noqa: E402
from time_utils import display_db_datetime, parse_timezone  # noqa: E402
import user_prefs  # noqa: E402


class UserPrefsT15Tests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = os.path.join(self.temp_dir.name, "test_t15.db")
        database.init_db()

    def tearDown(self):
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def test_guard_no_main_imports_in_user_prefs(self):
        """Guard test: user_prefs.py must never import main."""
        current_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        module_path = os.path.join(current_dir, "user_prefs.py")
        with open(module_path, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertNotIn("from main import", content)
        self.assertNotIn("import main", content)

    def test_default_preferences(self):
        """When user has no entry in user_prefs, fallback to defaults."""
        user_id = 112233
        self.assertIsNone(database.get_user_prefs(user_id))
        self.assertEqual(user_prefs.user_tz(user_id), main.TZ)
        hours = user_prefs.day_part_hours(user_id)
        self.assertEqual(hours, {"morning": 9, "day": 14, "evening": 20})

    def test_upsert_and_export_user_prefs(self):
        """upsert_user_prefs stores settings and export_user_data includes them."""
        user_id = 445566
        database.upsert_user_prefs(
            user_id,
            timezone="Asia/Novosibirsk",
            morning_hour=7,
            day_hour=13,
            evening_hour=21,
        )
        prefs = database.get_user_prefs(user_id)
        self.assertIsNotNone(prefs)
        self.assertEqual(prefs["timezone"], "Asia/Novosibirsk")
        self.assertEqual(prefs["morning_hour"], 7)
        self.assertEqual(prefs["day_hour"], 13)
        self.assertEqual(prefs["evening_hour"], 21)

        exported = database.export_user_data(user_id)
        self.assertIn("prefs", exported)
        self.assertEqual(exported["prefs"]["timezone"], "Asia/Novosibirsk")
        self.assertEqual(exported["prefs"]["morning_hour"], 7)

    def test_custom_timezone_and_morning_hour_utc_calculation(self):
        """Novosibirsk (UTC+7) user with morning 7:00 gets scheduled in exact UTC."""
        user_id = 556677
        database.upsert_user_prefs(
            user_id,
            timezone="Asia/Novosibirsk",
            morning_hour=7,
            day_hour=13,
            evening_hour=21,
        )
        tz = user_prefs.user_tz(user_id)
        self.assertEqual(str(tz), "Asia/Novosibirsk")
        hours = user_prefs.day_part_hours(user_id)
        self.assertEqual(hours["morning"], 7)

        # Simulated fixed user local time: 2026-06-15 15:00:00 (after 07:00 morning)
        user_fixed_now = datetime(2026, 6, 15, 15, 0, 0, tzinfo=tz)

        # Quick action 'morning' -> should be tomorrow at 07:00:00 local time
        scheduled_morning, _ = main.get_quick_scheduled_time("morning", user_fixed_now, hours=hours)
        self.assertEqual(scheduled_morning.year, 2026)
        self.assertEqual(scheduled_morning.month, 6)
        self.assertEqual(scheduled_morning.day, 16)
        self.assertEqual(scheduled_morning.hour, 7)
        self.assertEqual(scheduled_morning.minute, 0)

        # Serialized to UTC for database storage: 2026-06-16 07:00:00 UTC+7 is 2026-06-16 00:00:00 UTC
        utc_str = database.serialize_datetime(scheduled_morning)
        self.assertEqual(utc_str, "2026-06-16 00:00:00")

        # Quick action 'evening' (21:00) -> today at 21:00:00 local time
        scheduled_evening, _ = main.get_quick_scheduled_time("evening", user_fixed_now, hours=hours)
        self.assertEqual(scheduled_evening.day, 15)
        self.assertEqual(scheduled_evening.hour, 21)
        utc_evening_str = database.serialize_datetime(scheduled_evening)
        self.assertEqual(utc_evening_str, "2026-06-15 14:00:00")

    def test_parse_reminder_time_with_user_hours_and_tz(self):
        """parse_reminder_time respects user custom hours and timezone."""
        tz = ZoneInfo("Asia/Novosibirsk")
        user_fixed_now = datetime(2026, 6, 15, 15, 0, 0, tzinfo=tz)
        custom_hours = {"morning": 7, "day": 13, "evening": 21}

        # 'завтра' defaults to user's morning_hour (7:00)
        res_tomorrow = parse_reminder_time("завтра", user_fixed_now, hours=custom_hours)
        self.assertIsNotNone(res_tomorrow)
        self.assertEqual(res_tomorrow.day, 16)
        self.assertEqual(res_tomorrow.hour, 7)
        self.assertEqual(res_tomorrow.minute, 0)
        self.assertEqual(res_tomorrow.tzinfo, tz)

        # 'вечером' uses user's evening_hour (21:00)
        res_evening = parse_reminder_time("вечером", user_fixed_now, hours=custom_hours)
        self.assertIsNotNone(res_evening)
        self.assertEqual(res_evening.day, 15)
        self.assertEqual(res_evening.hour, 21)

        # 'завтра днем' uses user's day_hour (13:00)
        res_day = parse_reminder_time("завтра днем", user_fixed_now, hours=custom_hours)
        self.assertIsNotNone(res_day)
        self.assertEqual(res_day.day, 16)
        self.assertEqual(res_day.hour, 13)

    def test_midnight_boundary_relative_calculations(self):
        """Relative calculations around midnight correctly respect user's local day."""
        tz = ZoneInfo("Europe/Kaliningrad")  # UTC+2
        # Local time: 23:50 on 2026-06-15 (in UTC it's already 21:50)
        user_late_now = datetime(2026, 6, 15, 23, 50, 0, tzinfo=tz)
        custom_hours = {"morning": 8, "day": 14, "evening": 20}

        # Morning should be next day at 08:00
        sched, _ = main.get_quick_scheduled_time("morning", user_late_now, hours=custom_hours)
        self.assertEqual(sched.day, 16)
        self.assertEqual(sched.hour, 8)
        # UTC serialization: 16th at 06:00:00
        self.assertEqual(database.serialize_datetime(sched), "2026-06-16 06:00:00")

    def test_validate_hours(self):
        """Hour validation enforces morning < day < evening within valid bands."""
        ok, msg = user_prefs.validate_hours(7, 13, 20)
        self.assertTrue(ok)
        self.assertEqual(msg, "")

        ok, msg = user_prefs.validate_hours(12, 14, 20)
        self.assertFalse(ok)

        ok, msg = user_prefs.validate_hours(9, 8, 20)
        self.assertFalse(ok)

        ok, msg = user_prefs.validate_hours(9, 14, 13)
        self.assertFalse(ok)

    def test_parse_timezone_helper(self):
        """parse_timezone correctly parses IANA and offset formats."""
        tz, name = parse_timezone("Europe/Moscow")
        self.assertIsNotNone(tz)
        self.assertEqual(name, "Europe/Moscow")

        tz, name = parse_timezone("UTC+7")
        self.assertIsNotNone(tz)
        self.assertEqual(name, "UTC+07:00")

        tz, name = parse_timezone("+5")
        self.assertIsNotNone(tz)
        self.assertEqual(name, "UTC+05:00")

        tz, name = parse_timezone("GMT-3")
        self.assertIsNotNone(tz)
        self.assertEqual(name, "UTC-03:00")

        tz, name = parse_timezone("Invalid/City_Name")
        self.assertIsNone(tz)
        self.assertIsNone(name)

    def test_display_db_datetime_with_user_tz(self):
        """display_db_datetime translates UTC DB string to user's timezone."""
        utc_db_val = "2026-06-16 00:00:00"
        novosibirsk_tz = ZoneInfo("Asia/Novosibirsk")  # UTC+7
        displayed = display_db_datetime(utc_db_val, tz=novosibirsk_tz)
        self.assertEqual(displayed.strftime("%d.%m.%Y в %H:%M"), "16.06.2026 в 07:00")
        self.assertEqual(displayed.hour, 7)
        self.assertEqual(displayed.tzinfo, novosibirsk_tz)

    async def test_user_prefs_fsm_tz_text_routing(self):
        """FSM state waiting_for_tz_text is routed to process_tz_text_input, not catch_message."""
        chat_id = 991122
        user_id = 991122

        mock_bot = AsyncMock()
        mock_bot.id = 12345
        mock_bot.send_message = AsyncMock()

        fsm_ctx = main.dp.fsm.get_context(bot=mock_bot, chat_id=chat_id, user_id=user_id)
        await fsm_ctx.set_state(user_prefs.UserPrefsState.waiting_for_tz_text)

        user = types.User(id=user_id, is_bot=False, first_name="Tester")
        chat = types.Chat(id=chat_id, type="private")
        message = types.Message(
            message_id=777,
            date=datetime.now(timezone.utc),
            chat=chat,
            from_user=user,
            text="Europe/Samara",
        )
        update = types.Update(update_id=2, message=message)

        with patch.object(main, "catch_message", new_callable=AsyncMock) as mock_catch:
            await main.dp.feed_update(mock_bot, update)
            mock_catch.assert_not_called()

        # Prefs should now have Europe/Samara
        prefs = database.get_user_prefs(user_id)
        self.assertIsNotNone(prefs)
        self.assertEqual(prefs["timezone"], "Europe/Samara")
        self.assertIsNone(await fsm_ctx.get_state())
