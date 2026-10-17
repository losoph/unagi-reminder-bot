"""Тесты модуля digest_setup.py и его интеграции с main.py (MS4)."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    Chat,
    InaccessibleMessage,
    InlineKeyboardMarkup,
    Message,
    Update,
    User,
)

os.environ.setdefault("BOT_TOKEN", "123456:TEST_TOKEN_FOR_UNIT_TESTS")

import data.database as database  # noqa: E402
import digest_setup  # noqa: E402
import main  # noqa: E402


def _now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class TestDigestSetupArchitecture(unittest.TestCase):
    """Архитектурные тесты изоляции, отсутствия импортов main и регистрации роутеров."""

    def test_guard_no_main_imports_in_digest_setup(self):
        """digest_setup.py НЕ должен импортировать main."""
        current_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        ds_path = os.path.join(current_dir, "digest_setup.py")
        with open(ds_path, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertNotIn("from main import", content)
        self.assertNotIn("import main", content)

    def test_reexports_on_main(self):
        """Все перемещённые символы должны быть доступны через main.<имя>."""
        exported_symbols = [
            "DigestSetupState",
            "build_setup_digest_keyboard",
            "save_subscription",
            "setup_digest",
        ]
        for name in exported_symbols:
            self.assertTrue(
                hasattr(main, name),
                f"main is missing re-exported symbol: {name}",
            )
            self.assertIs(
                getattr(main, name),
                getattr(digest_setup, name),
                f"main.{name} is not identical to digest_setup.{name}",
            )

    def test_router_included_before_fallback(self):
        """digest_setup.router должен быть включен в dp.sub_routers до fallback_router."""
        sub_router_names = [r.name for r in main.dp.sub_routers]
        self.assertIn("digest_setup", sub_router_names)
        self.assertIn("fallback", sub_router_names)

        setup_idx = sub_router_names.index("digest_setup")
        fallback_idx = sub_router_names.index("fallback")
        self.assertLess(
            setup_idx,
            fallback_idx,
            "digest_setup router must be evaluated before fallback router",
        )

    def test_handlers_registered_in_digest_setup_router(self):
        """Роутер digest_setup содержит хендлеры setup_digest и save_subscription."""
        handlers = []
        obs = digest_setup.router.observers.get("callback_query")
        self.assertIsNotNone(obs, "digest_setup router must have callback_query observer")
        for h in obs.handlers:
            handlers.append(h.callback.__name__)

        self.assertIn("setup_digest", handlers)
        self.assertIn("save_subscription", handlers)

    def test_keyboard_builder(self):
        """Клавиатура мастера содержит кнопки для daily, weekly, monthly и feed_add."""
        kb = digest_setup.build_setup_digest_keyboard()
        self.assertIsInstance(kb, InlineKeyboardMarkup)
        all_buttons = [b for row in kb.inline_keyboard for b in row]
        button_datas = [b.callback_data for b in all_buttons]

        self.assertIn("period_daily", button_datas)
        self.assertIn("period_weekly", button_datas)
        self.assertIn("period_monthly", button_datas)
        self.assertIn("feed_add", button_datas)


class TestDigestSetupFlow(unittest.IsolatedAsyncioTestCase):
    """Функциональные тесты мастера подписки через роутер и функции модуля."""

    def setUp(self):
        self._orig_max_subs = database.MAX_DIGEST_SUBSCRIPTIONS
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_ds.db")
        database.DB_PATH = self.db_path
        database.init_db()

        self.storage = MemoryStorage()
        self.bot = AsyncMock()
        self.bot.id = 12345
        self.bot.send_message = AsyncMock()
        self.bot.delete_message = AsyncMock()
        digest_setup.configure(bot=self.bot)

    def tearDown(self):
        database.MAX_DIGEST_SUBSCRIPTIONS = self._orig_max_subs
        self.temp_dir.cleanup()

    def _get_fsm_context(self, user_id: int) -> FSMContext:
        key = StorageKey(bot_id=self.bot.id, chat_id=user_id, user_id=user_id)
        return FSMContext(storage=self.storage, key=key)

    def _make_callback(
        self,
        user_id: int,
        data: str,
        message: Message | InaccessibleMessage | None = None,
    ) -> CallbackQuery:
        user = User(id=user_id, is_bot=False, first_name="Tester")
        cb = AsyncMock(spec=CallbackQuery)
        cb.id = "cb_123"
        cb.from_user = user
        cb.data = data
        cb.bot = self.bot
        cb.answer = AsyncMock()
        cb.message = message
        return cb

    def _make_message(self, user_id: int, reply_to: Message | None = None) -> Message:
        chat = Chat(id=user_id, type="private")
        user = User(id=user_id, is_bot=False, first_name="Tester")
        msg = AsyncMock(spec=Message)
        msg.message_id = 100
        msg.chat = chat
        msg.from_user = user
        msg.reply_to_message = reply_to
        msg.edit_text = AsyncMock()
        msg.answer = AsyncMock()
        return msg

    async def test_setup_digest_shows_period_keyboard(self):
        """При вызове setup_digest пользователю предлагается выбор периодичности."""
        user_id = 5001
        state = self._get_fsm_context(user_id)
        await state.update_data(channel_username="tech_channel", channel_title="Tech Channel")

        msg = self._make_message(user_id)
        cb = self._make_callback(user_id, "digest_setup", message=msg)

        await digest_setup.setup_digest(cb, state)

        msg.edit_text.assert_called_once()
        args, kwargs = msg.edit_text.call_args
        self.assertIn("Как часто присылать", args[0])
        kb = kwargs.get("reply_markup")
        self.assertIsInstance(kb, InlineKeyboardMarkup)
        button_datas = [b.callback_data for row in kb.inline_keyboard for b in row]
        self.assertIn("period_daily", button_datas)
        self.assertIn("period_weekly", button_datas)
        self.assertIn("period_monthly", button_datas)
        self.assertIn("feed_add", button_datas)

    async def test_setup_digest_inaccessible_message(self):
        """Если сообщение недоступно, setup_digest отвечает ошибкой."""
        user_id = 5002
        state = self._get_fsm_context(user_id)
        cb = self._make_callback(user_id, "digest_setup", message=None)

        await digest_setup.setup_digest(cb, state)
        cb.answer.assert_called_once_with("❌ Сообщение недоступно.")

    async def test_save_subscription_success(self):
        """Успешное сохранение подписки daily с удалением оригинального сообщения и очисткой FSM."""
        user_id = 5003
        state = self._get_fsm_context(user_id)
        await state.update_data(channel_username="habr", channel_title="Habr Media")

        orig_msg = self._make_message(user_id)
        orig_msg.message_id = 99
        msg = self._make_message(user_id, reply_to=orig_msg)
        cb = self._make_callback(user_id, "period_daily", message=msg)

        await digest_setup.save_subscription(cb, state)

        cb.answer.assert_called_once_with("✅ Подписка оформлена!")
        self.bot.delete_message.assert_called_once_with(user_id, 99)

        msg.edit_text.assert_called_once()
        args, kwargs = msg.edit_text.call_args
        self.assertIn("Дайджест оформлен", args[0])
        self.assertIn("Habr Media", args[0])
        self.assertIn("каждый день", args[0])

        # Проверяем запись в БД
        subs = database.get_user_subscriptions(user_id)
        self.assertEqual(len(subs), 1)
        sub = subs[0]
        self.assertEqual(sub[1], "habr")
        self.assertEqual(sub[2], "Habr Media")
        self.assertEqual(sub[3], "daily")

        # FSM контекст очищен
        data = await state.get_data()
        self.assertEqual(data, {})

    async def test_save_subscription_missing_username(self):
        """Если данные канала утеряны в FSM, возвращается ошибка."""
        user_id = 5004
        state = self._get_fsm_context(user_id)
        # Данные канала НЕ сохранены в state

        msg = self._make_message(user_id)
        cb = self._make_callback(user_id, "period_weekly", message=msg)

        await digest_setup.save_subscription(cb, state)

        msg.edit_text.assert_called_once_with("❌ Ошибка: данные канала утеряны.")
        self.assertEqual(len(database.get_user_subscriptions(user_id)), 0)

    async def test_save_subscription_invalid_username(self):
        """Если username канала невалиден, выводится алерт."""
        user_id = 5005
        state = self._get_fsm_context(user_id)
        await state.update_data(channel_username="bad-username!", channel_title="Bad")

        msg = self._make_message(user_id)
        cb = self._make_callback(user_id, "period_monthly", message=msg)

        await digest_setup.save_subscription(cb, state)

        cb.answer.assert_called_once_with("❌ Некорректное имя канала.", show_alert=True)
        self.assertEqual(len(database.get_user_subscriptions(user_id)), 0)

    async def test_save_subscription_invalid_callback_data(self):
        """Некорректная callback_data без суффикса периода."""
        user_id = 5006
        state = self._get_fsm_context(user_id)
        msg = self._make_message(user_id)
        cb = self._make_callback(user_id, "period_", message=msg)

        await digest_setup.save_subscription(cb, state)
        cb.answer.assert_called_once_with("❌ Некорректные данные.")

    async def test_save_subscription_limit_reached(self):
        """При превышении лимита подписок выдается информативный алерт."""
        user_id = 5007
        with unittest.mock.patch("data.database.MAX_DIGEST_SUBSCRIPTIONS", 1):
            database.add_subscription(
                user_id,
                "chan_first",
                "First",
                "daily",
                _now_str(),
                _now_str(),
            )

            state = self._get_fsm_context(user_id)
            await state.update_data(channel_username="chan_second", channel_title="Second")

            msg = self._make_message(user_id)
            cb = self._make_callback(user_id, "period_daily", message=msg)

            await digest_setup.save_subscription(cb, state)

            cb.answer.assert_called_once()
            args, kwargs = cb.answer.call_args
            self.assertIn("Достигнут лимит подписок", args[0])
            self.assertTrue(kwargs.get("show_alert"))

    async def test_dispatch_via_dp_feed_update(self):
        """Проверка сквозной диспетчеризации aiogram dp -> digest_setup.router."""
        user_id = 5008
        mock_bot = AsyncMock()
        mock_bot.id = 12345
        mock_bot.edit_message_text = AsyncMock()
        mock_bot.answer_callback_query = AsyncMock()

        user = User(id=user_id, is_bot=False, first_name="Tester")
        chat = Chat(id=user_id, type="private")

        fsm_ctx = main.dp.fsm.get_context(bot=mock_bot, chat_id=user_id, user_id=user_id)
        await fsm_ctx.update_data(channel_username="dispatch_chan", channel_title="Dispatch")

        msg = Message(
            message_id=200,
            date=datetime.now(timezone.utc),
            chat=chat,
            from_user=user,
            text="Канал Dispatch",
        )

        cb = CallbackQuery(
            id="cb_test_dispatch",
            from_user=user,
            chat_instance="ci_1",
            message=msg,
            data="period_daily",
        )

        update = Update(update_id=5001, callback_query=cb)
        await main.dp.feed_update(mock_bot, update)

        self.assertTrue(
            any(
                getattr(call.args[0], "__class__", None).__name__ == "AnswerCallbackQuery"
                for call in mock_bot.call_args_list
            ),
            "Bot should receive AnswerCallbackQuery call",
        )
        subs = database.get_user_subscriptions(user_id)
        self.assertEqual(len(subs), 1)
        self.assertEqual(subs[0][1], "dispatch_chan")
