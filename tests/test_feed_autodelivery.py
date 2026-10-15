"""Автодоставка Ленты (T24c): срок, один выпуск на срок, повтор и пропуск, блокировка бота, приватность кнопок.

Даты строятся от «сегодня в полдень по поясу приложения» — абсолютных дат нет, результат не зависит от реального времени суток.
"""

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("BOT_TOKEN", "123456:TEST_TOKEN_FOR_UNIT_TESTS")

from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError  # noqa: E402

import feed  # noqa: E402
from data import database  # noqa: E402

TZ = feed.TZ


def noon() -> datetime:
    """Сегодня 12:00 по поясу приложения (aware)."""
    return datetime.now(TZ).replace(hour=12, minute=0, second=0, microsecond=0)


def utc(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc)


def result(status="ok", **kwargs):
    defaults = {"telegraph_url": "https://telegra.ph/test", "post_count": 3, "channel_count": 2}
    return feed.FeedBuildResult(status=status, **(defaults | kwargs))


class AutodeliveryBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        original_path = database.DB_PATH
        database.DB_PATH = os.path.join(self.temp_dir.name, "autodelivery.db")
        self.addCleanup(setattr, database, "DB_PATH", original_path)
        database.init_db()
        feed._autodelivery_failures.clear()
        self.addCleanup(feed._autodelivery_failures.clear)
        self.settings = {"daily": {"send_hour": 7, "send_minute": 0}, "weekly": {"send_hour": 9, "send_minute": 0, "weekday": 0}}
        for target, value in (
            ("_resolve_digest_settings_fn", lambda user_id, period, now=None: self.settings[period]),
            ("_get_next_digest_time_fn", None),  # встроенный расчёт срока
        ):
            patcher = patch.object(feed, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def enable(self, user_id, period, created: datetime, last_run: datetime | None = None):
        database.enable_feed_autodelivery(user_id, period, utc(created))
        if last_run:
            database.update_feed_autodelivery_last_run(user_id, period, utc(last_run))

    def row(self, user_id, period):
        return database.get_feed_autodelivery(user_id, period)

    async def run_cycle(self, now: datetime, build, bot=None):
        bot = bot or SimpleNamespace(send_message=AsyncMock())
        with patch("feed.build_feed_edition", new=build), patch.object(feed, "_bot", bot):
            sent = await feed.process_feed_autodeliveries(now=utc(now))
        return sent, bot


class DueTimeTests(AutodeliveryBase):
    def test_first_slot_is_after_enabling_not_the_already_passed_one(self):
        created = noon()
        self.enable(1, "day", created)
        due = feed.get_autodelivery_due_time(1, "day", self.row(1, "day"), now=utc(created))
        self.assertEqual(due, (created + timedelta(days=1)).replace(hour=7))

    def test_next_slot_counts_from_last_run(self):
        created = noon() - timedelta(days=5)
        last_run = noon() - timedelta(hours=1)
        self.enable(1, "day", created, last_run)
        due = feed.get_autodelivery_due_time(1, "day", self.row(1, "day"), now=utc(noon()))
        self.assertEqual(due, (last_run + timedelta(days=1)).replace(hour=7))

    def test_digest_time_change_is_picked_up_without_stored_next_run(self):
        created = noon()
        self.enable(1, "day", created)
        self.settings["daily"] = {"send_hour": 23, "send_minute": 50}
        due = feed.get_autodelivery_due_time(1, "day", self.row(1, "day"), now=utc(created))
        self.assertEqual((due.date(), due.hour, due.minute), (created.date(), 23, 50))
        self.settings["daily"] = {"send_hour": 8, "send_minute": 30}
        due = feed.get_autodelivery_due_time(1, "day", self.row(1, "day"), now=utc(created))
        self.assertEqual((due.date(), due.hour, due.minute), ((created + timedelta(days=1)).date(), 8, 30))

    def test_db_utc_is_converted_to_local_schedule_at_midnight_boundary(self):
        created = noon().replace(hour=23, minute=55)
        self.enable(1, "day", created)
        self.settings["daily"] = {"send_hour": 0, "send_minute": 5}
        due = feed.get_autodelivery_due_time(1, "day", self.row(1, "day"), now=utc(created))
        self.assertEqual((due.date(), due.hour, due.minute), ((created + timedelta(days=1)).date(), 0, 5))

    def test_weekly_slot_lands_on_configured_weekday(self):
        created = noon()
        self.enable(1, "week", created)
        self.settings["weekly"]["weekday"] = (created.weekday() + 3) % 7
        due = feed.get_autodelivery_due_time(1, "week", self.row(1, "week"), now=utc(created))
        self.assertEqual((due - created).days, 2)  # +3 суток по календарю, 09:00 — меньше 3 полных суток от полудня
        self.assertEqual((due.weekday(), due.hour), ((created.weekday() + 3) % 7, 9))

    def test_unknown_period_or_empty_row_has_no_due_time(self):
        self.assertIsNone(feed.get_autodelivery_due_time(1, "year", {"created_at": "2026-01-01 00:00:00"}))
        self.assertIsNone(feed.get_autodelivery_due_time(1, "day", {"created_at": None, "last_run_at": None}))


class StorageTests(AutodeliveryBase):
    def test_enable_is_idempotent_and_rejects_unknown_period(self):
        created = noon() - timedelta(days=1)
        self.enable(1, "day", created, last_run=created + timedelta(hours=3))
        database.enable_feed_autodelivery(1, "day", utc(noon()))
        row = self.row(1, "day")
        self.assertEqual(row["created_at"], database.serialize_datetime(utc(created)))
        self.assertEqual(row["last_run_at"], database.serialize_datetime(utc(created + timedelta(hours=3))))
        with self.assertRaises(ValueError):
            database.enable_feed_autodelivery(1, "year")

    def test_init_db_is_idempotent_and_autodelivery_is_not_exported(self):
        self.enable(1, "day", noon())
        database.init_db()
        database.init_db()
        self.assertNotIn("feed_autodelivery", database.export_user_data(1))
        with database.get_connection() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM feed_autodelivery").fetchone()[0], 1)

    def test_users_are_independent_and_disable_is_per_user(self):
        self.enable(1, "day", noon())
        self.enable(2, "day", noon())
        self.assertTrue(database.disable_feed_autodelivery(1, "day"))
        self.assertIsNone(self.row(1, "day"))
        self.assertIsNotNone(self.row(2, "day"))
        self.assertFalse(database.disable_feed_autodelivery(1, "day"))


class ProcessTests(AutodeliveryBase):
    async def test_due_slot_sends_once_and_next_cycle_in_same_slot_is_silent(self):
        now = noon() + timedelta(days=1, hours=1)  # завтра 13:00: срок 07:00 наступил
        self.enable(1, "day", noon())
        build = AsyncMock(return_value=result())
        sent, bot = await self.run_cycle(now, build)
        self.assertEqual(sent, 1)
        text = bot.send_message.await_args.args[1]
        self.assertIn("Автодоставка", text)
        self.assertIn("https://telegra.ph/test", text)
        self.assertEqual(build.await_args.kwargs["ignore_rate_limit"], True)
        self.assertEqual(self.row(1, "day")["last_run_at"], database.serialize_datetime(utc(now)))
        sent, bot = await self.run_cycle(now + timedelta(minutes=2), build, bot)
        self.assertEqual((sent, build.await_count, bot.send_message.await_count), (0, 1, 1))

    async def test_not_due_yet_does_nothing(self):
        self.enable(1, "day", noon())
        build = AsyncMock(return_value=result())
        sent, bot = await self.run_cycle(noon() + timedelta(hours=5), build)
        self.assertEqual((sent, build.await_count), (0, 0))
        bot.send_message.assert_not_awaited()

    async def test_only_due_users_get_a_message(self):
        now = noon() + timedelta(days=1, hours=1)
        self.enable(1, "day", noon())
        self.enable(2, "day", now - timedelta(minutes=30))  # включил недавно: ближайший срок завтра
        build = AsyncMock(return_value=result())
        sent, bot = await self.run_cycle(now, build)
        self.assertEqual(sent, 1)
        self.assertEqual(bot.send_message.await_args.args[0], 1)

    async def test_long_downtime_yields_one_catch_up_edition(self):
        now = noon() + timedelta(days=10)
        self.enable(1, "day", noon())
        build = AsyncMock(return_value=result())
        sent, _ = await self.run_cycle(now, build)
        self.assertEqual(sent, 1)
        sent, _ = await self.run_cycle(now + timedelta(minutes=2), build)
        self.assertEqual((sent, build.await_count), (0, 1))

    async def test_failures_retry_then_skip_the_slot_without_user_message(self):
        now = noon() + timedelta(days=1, hours=1)
        self.enable(1, "day", noon())
        build = AsyncMock(return_value=result("error", error_message="сборка не удалась"))
        bot = SimpleNamespace(send_message=AsyncMock())
        for attempt in (1, 2):
            await self.run_cycle(now, build, bot)
            self.assertEqual(feed._autodelivery_failures, {(1, "day"): attempt})
            self.assertIsNone(self.row(1, "day")["last_run_at"])
        await self.run_cycle(now, build, bot)
        self.assertEqual(build.await_count, 3)
        self.assertEqual(feed._autodelivery_failures, {})
        self.assertEqual(self.row(1, "day")["last_run_at"], database.serialize_datetime(utc(now)))
        await self.run_cycle(now, build, bot)  # слот пропущен: четвёртой попытки нет
        self.assertEqual(build.await_count, 3)
        bot.send_message.assert_not_awaited()

    async def test_success_after_failure_resets_the_counter(self):
        now = noon() + timedelta(days=1, hours=1)
        self.enable(1, "day", noon())
        build = AsyncMock(side_effect=[result("timeout"), result()])
        await self.run_cycle(now, build)
        self.assertEqual(feed._autodelivery_failures, {(1, "day"): 1})
        sent, _ = await self.run_cycle(now, build)
        self.assertEqual((sent, feed._autodelivery_failures), (1, {}))

    async def test_exception_in_build_counts_as_failure_and_loop_continues(self):
        now = noon() + timedelta(days=1, hours=1)
        self.enable(1, "day", noon())
        self.enable(2, "day", noon())
        build = AsyncMock(side_effect=[RuntimeError("boom"), result()])
        sent, bot = await self.run_cycle(now, build)
        self.assertEqual(sent, 1)
        self.assertEqual(bot.send_message.await_args.args[0], 2)
        self.assertEqual(feed._autodelivery_failures, {(1, "day"): 1})

    async def test_send_error_is_a_retryable_failure_and_keeps_the_slot_open(self):
        now = noon() + timedelta(days=1, hours=1)
        self.enable(1, "day", noon())
        bot = SimpleNamespace(send_message=AsyncMock(side_effect=TelegramAPIError(method=object(), message="flood")))
        sent, _ = await self.run_cycle(now, AsyncMock(return_value=result()), bot)
        self.assertEqual(sent, 0)
        self.assertIsNone(self.row(1, "day")["last_run_at"])
        self.assertEqual(feed._autodelivery_failures, {(1, "day"): 1})

    async def test_blocked_bot_disables_autodelivery_instead_of_retrying_forever(self):
        now = noon() + timedelta(days=1, hours=1)
        self.enable(1, "day", noon())
        bot = SimpleNamespace(send_message=AsyncMock(side_effect=TelegramForbiddenError(method=object(), message="blocked")))
        build = AsyncMock(return_value=result())
        await self.run_cycle(now, build, bot)
        self.assertIsNone(self.row(1, "day"))
        self.assertEqual(feed._autodelivery_failures, {})
        await self.run_cycle(now, build, bot)
        self.assertEqual(build.await_count, 1)

    async def test_empty_results_advance_the_slot_without_a_message(self):
        for status in ("empty_channels", "all_paused", "empty_posts"):
            with self.subTest(status=status):
                now = noon() + timedelta(days=1, hours=1)
                self.enable(5, "day", noon())
                database.update_feed_autodelivery_last_run(5, "day", utc(noon()))
                sent, bot = await self.run_cycle(now, AsyncMock(return_value=result(status)))
                self.assertEqual(sent, 0)
                bot.send_message.assert_not_awaited()
                self.assertEqual(self.row(5, "day")["last_run_at"], database.serialize_datetime(utc(now)))
                database.disable_feed_autodelivery(5, "day")

    async def test_failure_counters_of_disabled_autodeliveries_are_dropped(self):
        feed._autodelivery_failures[(77, "week")] = 2
        await self.run_cycle(noon(), AsyncMock(return_value=result()))
        self.assertEqual(feed._autodelivery_failures, {})


class CallbackTests(AutodeliveryBase):
    @staticmethod
    def callback(user_id, data):
        message = SimpleNamespace(text="", edit_text=AsyncMock(), answer=AsyncMock(), edit_reply_markup=AsyncMock())
        return SimpleNamespace(from_user=SimpleNamespace(id=user_id), data=data, message=message, answer=AsyncMock())

    async def test_confirm_and_off_touch_only_the_callers_own_row(self):
        self.enable(999, "week", noon())
        await feed.cb_feed_autodel_confirm(self.callback(92, "feed_autodel_confirm_week"))
        self.assertIsNotNone(self.row(92, "week"))
        await feed.cb_feed_autodel_off(self.callback(92, "feed_autodel_off_week"))
        self.assertIsNone(self.row(92, "week"))
        self.assertIsNotNone(self.row(999, "week"))

    async def test_confirm_twice_does_not_reset_the_schedule_base(self):
        await feed.cb_feed_autodel_confirm(self.callback(93, "feed_autodel_confirm_day"))
        first = self.row(93, "day")["created_at"]
        database.update_feed_autodelivery_last_run(93, "day", utc(noon()))
        await feed.cb_feed_autodel_confirm(self.callback(93, "feed_autodel_confirm_day"))
        row = self.row(93, "day")
        self.assertEqual((row["created_at"], row["last_run_at"]), (first, database.serialize_datetime(utc(noon()))))

    async def test_stale_or_invalid_buttons_are_explained(self):
        for handler, data in (
            (feed.cb_feed_autodel_confirm, "feed_autodel_confirm_year"),
            (feed.cb_feed_autodel_prompt, "feed_autodel_prompt_"),
            (feed.cb_feed_autodel_off, "feed_autodel_off_xx"),
            (feed.cb_feed_autodel_cancel, "feed_autodel_cancel_????"),
        ):
            with self.subTest(data=data):
                stale = self.callback(94, data)
                await handler(stale)
                self.assertIn("устарела", stale.answer.await_args.args[0])

    def test_callback_data_fits_telegram_limit(self):
        for period in ("day", "week", "month"):
            markup = feed.build_feed_edition_keyboard(1, period)
            for row in markup.inline_keyboard:
                for button in row:
                    if button.callback_data:
                        self.assertLessEqual(len(button.callback_data.encode("utf-8")), 64, button.callback_data)
        for action in ("prompt", "confirm", "cancel", "off"):
            self.assertLessEqual(len(f"feed_autodel_{action}_month".encode("utf-8")), 64)


class ScreenTests(AutodeliveryBase):
    def test_edition_keyboard_offers_enable_then_disable(self):
        def autodel_buttons():
            rows = feed.build_feed_edition_keyboard(1, "week").inline_keyboard
            return [b for row in rows for b in row if (b.callback_data or "").startswith("feed_autodel_")]

        self.assertEqual([(b.text, b.callback_data) for b in autodel_buttons()], [("🔔 Присылать автоматически", "feed_autodel_prompt_week")])
        self.enable(1, "week", noon())
        self.assertEqual([(b.text, b.callback_data) for b in autodel_buttons()], [("🔕 Не присылать", "feed_autodel_off_week")])

    async def test_main_screen_lists_enabled_autodeliveries_with_schedule_and_off_button(self):
        self.enable(1, "week", noon())
        target = SimpleNamespace(answer=AsyncMock())
        with patch.object(feed, "is_session_owner", return_value=False):
            await feed.render_feed_main(target, 1)
        text = target.answer.await_args.args[0]
        self.assertIn("Автодоставка", text)
        self.assertIn("неделя", text)
        markup = target.answer.await_args.kwargs["reply_markup"]
        callbacks = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertIn("feed_autodel_off_week", callbacks)
        self.assertNotIn("feed_autodel_off_day", callbacks)

    async def test_prompt_text_names_the_digest_schedule_and_asks_confirmation(self):
        callback = CallbackTests.callback(1, "feed_autodel_prompt_day")
        await feed.cb_feed_autodel_prompt(callback)
        text = callback.message.edit_text.await_args.args[0]
        self.assertIn("Лента за день будет приходить", text)
        buttons = [b.callback_data for row in callback.message.edit_text.await_args.kwargs["reply_markup"].inline_keyboard for b in row]
        self.assertEqual(buttons, ["feed_autodel_confirm_day", "feed_autodel_cancel_day"])
        self.assertIsNone(self.row(1, "day"))  # включение — только после подтверждения


if __name__ == "__main__":
    unittest.main()
