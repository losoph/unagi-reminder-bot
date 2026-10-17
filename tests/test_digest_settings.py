"""Тесты модуля digest_settings.py и его интеграции с main.py (MS2)."""

import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from aiogram.types import CallbackQuery, Chat, Message, Update, User

os.environ.setdefault("BOT_TOKEN", "123456:TEST_TOKEN_FOR_UNIT_TESTS")

import data.database as database  # noqa: E402
import digest_settings  # noqa: E402
import main  # noqa: E402


def _now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class TestDigestSettingsArchitecture(unittest.TestCase):
    """Архитектурные тесты изоляции, отсутствия импортов main и порядка роутеров."""

    def test_guard_no_main_imports_in_digest_settings(self):
        """digest_settings.py НЕ должен импортировать main."""
        current_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        ds_path = os.path.join(current_dir, "digest_settings.py")
        with open(ds_path, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertNotIn("from main import", content)
        self.assertNotIn("import main", content)

    def test_reexports_on_main(self):
        """Все перемещённые символы должны быть доступны через main.<имя>."""
        exported_symbols = [
            "SubTagState",
            "_PERIOD_RU",
            "_PERIOD_TITLES",
            "_PERIOD_PLURAL",
            "_WEEKDAY_NOM_RU",
            "_WEEKDAY_ACC_RU",
            "_WEEKDAY_SHORT_RU",
            "_build_digest_candidate",
            "_nth_weekday_of_month",
            "_refresh_digest_settings",
            "_sort_subscriptions_for_display",
            "_subscription_status_marker",
            "build_digest_channel_actions",
            "build_digest_monthday_keyboard",
            "build_digest_monthly_mode_keyboard",
            "build_digest_schedule_keyboard",
            "build_digest_settings_keyboard",
            "build_digest_time_hours_keyboard",
            "build_digest_time_minutes_keyboard",
            "build_digest_weekday_keyboard",
            "build_post_subscribe_keyboard",
            "build_subscription_folder_keyboard",
            "clear_subscription_folder",
            "confirm_unsubscribe_all",
            "edit_or_answer",
            "format_digest_schedule",
            "get_default_digest_settings",
            "get_email_delivery_periods",
            "get_next_digest_time",
            "move_digest_subscription",
            "open_digest_minute_picker",
            "open_digest_monthday_picker",
            "open_digest_monthly_mode_picker",
            "open_digest_schedule",
            "open_digest_settings",
            "open_digest_subscription_actions",
            "open_digest_time_picker",
            "open_digest_weekday_picker",
            "pause_all_subscriptions",
            "pause_subscription",
            "process_new_subscription_folder",
            "prompt_new_subscription_folder",
            "render_digest_schedule_text",
            "render_digest_settings_text",
            "render_subscription_actions",
            "reschedule_user_period",
            "resolve_digest_settings",
            "resume_all_subscriptions",
            "resume_subscription",
            "save_digest_monthday",
            "save_digest_monthly_mode",
            "save_digest_time",
            "save_digest_weekday",
            "set_subscription_folder",
            "unsubscribe_all_confirmed",
        ]
        for name in exported_symbols:
            self.assertTrue(
                hasattr(main, name),
                f"main is missing re-exported symbol: {name}",
            )
            self.assertIs(
                getattr(main, name),
                getattr(digest_settings, name),
                f"main.{name} is not identical to digest_settings.{name}",
            )

    def test_router_included_before_fallback(self):
        """digest_settings.router должен быть включен в dp.sub_routers до fallback_router."""
        sub_router_names = [r.name for r in main.dp.sub_routers]
        self.assertIn("digest_settings", sub_router_names)
        self.assertIn("fallback", sub_router_names)

        ds_idx = sub_router_names.index("digest_settings")
        fallback_idx = sub_router_names.index("fallback")
        self.assertLess(
            ds_idx,
            fallback_idx,
            "digest_settings router must be evaluated before fallback router",
        )

    def test_handler_order_priority_over_fallback(self):
        """Хендлеры digest_settings проверяются до catch_message из fallback."""
        def collect_handlers(router, update_type):
            handlers = []
            obs = router.observers.get(update_type)
            if obs:
                for h in obs.handlers:
                    handlers.append((router.name or "dp", h.callback.__name__))
            for sub in router.sub_routers:
                handlers.extend(collect_handlers(sub, update_type))
            return handlers

        msg_handlers = collect_handlers(main.dp, "message")
        handler_names = [h[1] for h in msg_handlers]

        catch_idx = handler_names.index("catch_message")
        self.assertIn("process_new_subscription_folder", handler_names)
        folder_idx = handler_names.index("process_new_subscription_folder")
        self.assertLess(
            folder_idx,
            catch_idx,
            "process_new_subscription_folder must be evaluated before catch_message",
        )


class TestDigestSettingsFlow(unittest.IsolatedAsyncioTestCase):
    """Функциональные тесты обработки настроек дайджеста через роутер."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_ds.db")
        database.DB_PATH = self.db_path
        database.init_db()

    def tearDown(self):
        self.temp_dir.cleanup()

    async def test_sub_tag_state_routed_to_digest_settings_not_catch_message(self):
        """В состоянии SubTagState.waiting_for_folder ввод текста обрабатывается process_new_subscription_folder,
        а не перехватывается catch_message."""
        user_id = 998811
        sub_id = database.add_subscription(
            user_id,
            "tech_news",
            "Tech News",
            "daily",
            _now_str(),
            _now_str(),
        )

        mock_bot = AsyncMock()
        mock_bot.id = 12345
        mock_bot.send_message = AsyncMock()
        mock_bot.delete_message = AsyncMock()

        user = User(id=user_id, is_bot=False, first_name="Test")
        chat = Chat(id=user_id, type="private")
        msg = Message(
            message_id=80,
            date=datetime.now(timezone.utc),
            chat=chat,
            from_user=user,
            text="Новости IT",
        )

        fsm_ctx = main.dp.fsm.get_context(bot=mock_bot, chat_id=user_id, user_id=user_id)
        await fsm_ctx.set_state(digest_settings.SubTagState.waiting_for_folder)
        await fsm_ctx.update_data(folder_sub_id=sub_id)

        update = Update(update_id=201, message=msg)
        with patch.object(main, "catch_message", new_callable=AsyncMock) as mock_catch:
            await main.dp.feed_update(mock_bot, update)
            mock_catch.assert_not_called()

        sub = database.get_subscription_by_id(user_id, sub_id)
        self.assertIsNotNone(sub)
        self.assertEqual(sub["tag"], "Новости IT")

        current_state = await fsm_ctx.get_state()
        self.assertIsNone(current_state)

    async def test_pause_and_resume_subscription_callback(self):
        """Колбэки dpause_<id> и dresume_<id> переключают статус паузы подписки."""
        user_id = 998812
        sub_id = database.add_subscription(
            user_id,
            "science_chan",
            "Science Channel",
            "weekly",
            _now_str(),
            _now_str(),
        )

        mock_bot = AsyncMock()
        mock_bot.id = 12345
        mock_bot.edit_message_text = AsyncMock()
        mock_bot.answer_callback_query = AsyncMock()

        user = User(id=user_id, is_bot=False, first_name="Test")
        chat = Chat(id=user_id, type="private")
        msg = Message(
            message_id=90,
            date=datetime.now(timezone.utc),
            chat=chat,
            from_user=user,
            text="Карточка канала",
        )

        # 1. Поставить на паузу
        cb_pause = CallbackQuery(
            id="cb_pause",
            from_user=user,
            chat_instance="ci1",
            message=msg,
            data=f"dpause_{sub_id}",
        )
        update_pause = Update(update_id=202, callback_query=cb_pause)
        await main.dp.feed_update(mock_bot, update_pause)

        sub = database.get_subscription_by_id(user_id, sub_id)
        self.assertEqual(sub["is_paused"], 1)

        # 2. Возобновить
        cb_resume = CallbackQuery(
            id="cb_resume",
            from_user=user,
            chat_instance="ci2",
            message=msg,
            data=f"dresume_{sub_id}",
        )
        update_resume = Update(update_id=203, callback_query=cb_resume)
        await main.dp.feed_update(mock_bot, update_resume)

        sub = database.get_subscription_by_id(user_id, sub_id)
        self.assertEqual(sub["is_paused"], 0)

    async def test_save_time_callback_updates_settings(self):
        """Колбэк dtm_<period>_<hour>_<minute> сохраняет время в настройках пользователя."""
        user_id = 998813
        mock_bot = AsyncMock()
        mock_bot.id = 12345
        mock_bot.edit_message_text = AsyncMock()
        mock_bot.answer_callback_query = AsyncMock()

        user = User(id=user_id, is_bot=False, first_name="Test")
        chat = Chat(id=user_id, type="private")
        msg = Message(
            message_id=95,
            date=datetime.now(timezone.utc),
            chat=chat,
            from_user=user,
            text="Пикер минут",
        )

        cb_time = CallbackQuery(
            id="cb_time",
            from_user=user,
            chat_instance="ci3",
            message=msg,
            data="dtm_daily_18_30",
        )
        update = Update(update_id=204, callback_query=cb_time)
        await main.dp.feed_update(mock_bot, update)

        settings = digest_settings.resolve_digest_settings(user_id, "daily")
        self.assertEqual(settings["send_hour"], 18)
        self.assertEqual(settings["send_minute"], 30)
