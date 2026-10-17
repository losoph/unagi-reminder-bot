"""Тесты модуля bookmarks.py и его интеграции с main.py (MS1)."""

import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from aiogram.types import CallbackQuery, Chat, Message, Update, User

os.environ.setdefault("BOT_TOKEN", "123456:TEST_TOKEN_FOR_UNIT_TESTS")

import data.database as database  # noqa: E402
import main  # noqa: E402
import bookmarks  # noqa: E402


def _now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class TestBookmarksArchitecture(unittest.TestCase):
    """Архитектурные тесты изоляции, отсутствия импортов main и регистрации роутеров."""

    def test_guard_no_main_imports_in_bookmarks(self):
        """bookmarks.py НЕ должен импортировать main."""
        current_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        bookmarks_path = os.path.join(current_dir, "bookmarks.py")
        with open(bookmarks_path, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertNotIn("from main import", content)
        self.assertNotIn("import main", content)

    def test_reexports_on_main(self):
        """Все перемещённые символы должны быть доступны через main.<имя>."""
        exported_symbols = [
            "SaveState",
            "render_kb_tags_screen",
            "render_kb_list_screen",
            "render_kb_detail_screen",
            "render_kb_move_screen",
            "start_save_flow",
            "cmd_saved",
            "cmd_save_reply",
            "process_new_tag",
            "process_tag",
            "callback_menu_bookmarks",
            "save_sent_reminder",
            "setup_bookmark",
            "handle_kb_tag_selection",
            "handle_kb_tag_all",
            "handle_kb_view_tags",
            "handle_kb_page_prev",
            "handle_kb_page_next",
            "handle_kb_sel_idx",
            "handle_kb_ignore",
            "handle_bkmv_init",
            "handle_kb_mv_idx",
            "handle_bkdel",
        ]
        for name in exported_symbols:
            self.assertTrue(
                hasattr(main, name),
                f"main is missing re-exported symbol: {name}",
            )
            self.assertIs(
                getattr(main, name),
                getattr(bookmarks, name),
                f"main.{name} is not identical to bookmarks.{name}",
            )

    def test_router_included_before_fallback(self):
        """bookmarks.router должен быть включен в dp.sub_routers до fallback_router."""
        sub_router_names = [r.name for r in main.dp.sub_routers]
        self.assertIn("bookmarks", sub_router_names)
        self.assertIn("fallback", sub_router_names)

        bookmarks_idx = sub_router_names.index("bookmarks")
        fallback_idx = sub_router_names.index("fallback")
        self.assertLess(
            bookmarks_idx,
            fallback_idx,
            "bookmarks router must be evaluated before fallback router",
        )

    def test_handler_order_priority_over_fallback(self):
        """Хендлеры bookmarks проверяются до catch_message из fallback."""
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

        # All bookmark message handlers must appear before catch_message
        catch_idx = handler_names.index("catch_message")
        for bkm_handler in ["cmd_saved", "cmd_save_reply", "process_new_tag", "process_tag"]:
            self.assertIn(bkm_handler, handler_names)
            idx = handler_names.index(bkm_handler)
            self.assertLess(
                idx,
                catch_idx,
                f"{bkm_handler} must be evaluated before catch_message",
            )


class TestBookmarksFlow(unittest.IsolatedAsyncioTestCase):
    """Функциональные тесты обработки закладок через диспетчер и роутер."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_bookmarks.db")
        database.DB_PATH = self.db_path
        database.init_db()

    def tearDown(self):
        self.temp_dir.cleanup()

    async def test_save_state_waiting_for_tag_routed_to_bookmarks_not_catch_message(self):
        """В состоянии SaveState.waiting_for_tag текст должен обрабатываться process_tag,
        а не перехватываться catch_message."""
        user_id = 771122
        mock_bot = AsyncMock()
        mock_bot.id = 12345
        mock_bot.send_message = AsyncMock()
        mock_bot.delete_message = AsyncMock()

        user = User(id=user_id, is_bot=False, first_name="Test")
        chat = Chat(id=user_id, type="private")
        msg = Message(
            message_id=50,
            date=datetime.now(timezone.utc),
            chat=chat,
            from_user=user,
            text="Работа",
        )

        fsm_ctx = main.dp.fsm.get_context(bot=mock_bot, chat_id=user_id, user_id=user_id)
        await fsm_ctx.set_state(bookmarks.SaveState.waiting_for_tag)
        await fsm_ctx.update_data(
            full_text="Текст для закладки",
            source="Канал тест",
            prompt_msg_id=49,
        )

        update = Update(update_id=101, message=msg)
        with patch.object(main, "catch_message", new_callable=AsyncMock) as mock_catch:
            await main.dp.feed_update(mock_bot, update)
            mock_catch.assert_not_called()

        # Saved in database
        saved = database.get_saved_messages(user_id)
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["tag"], "Работа")
        self.assertEqual(saved[0]["full_text"], "Текст для закладки")

        # FSM state cleared
        current_state = await fsm_ctx.get_state()
        self.assertIsNone(current_state)

    async def test_save_state_waiting_for_new_tag_updates_bookmark(self):
        """В состоянии SaveState.waiting_for_new_tag текст меняет тег закладки."""
        user_id = 771123
        saved_id = database.add_saved_message(
            user_id,
            "Текст заметки",
            "Источник",
            "старый_тег",
            _now_str(),
        )

        mock_bot = AsyncMock()
        mock_bot.id = 12345
        mock_bot.send_message = AsyncMock()
        mock_bot.delete_message = AsyncMock()

        user = User(id=user_id, is_bot=False, first_name="Test")
        chat = Chat(id=user_id, type="private")
        msg = Message(
            message_id=60,
            date=datetime.now(timezone.utc),
            chat=chat,
            from_user=user,
            text="#новый_тег",
        )

        fsm_ctx = main.dp.fsm.get_context(bot=mock_bot, chat_id=user_id, user_id=user_id)
        await fsm_ctx.set_state(bookmarks.SaveState.waiting_for_new_tag)
        await fsm_ctx.update_data(bookmark_msg_id=saved_id)

        update = Update(update_id=102, message=msg)
        with patch.object(main, "catch_message", new_callable=AsyncMock) as mock_catch:
            await main.dp.feed_update(mock_bot, update)
            mock_catch.assert_not_called()

        row = database.get_saved_message_by_id(user_id, saved_id)
        self.assertIsNotNone(row)
        self.assertEqual(row[2], "новый_тег")

    async def test_delete_bookmark_callback(self):
        """Колбэк bkdel_<id> удаляет закладку из базы."""
        user_id = 771124
        saved_id = database.add_saved_message(
            user_id,
            "Удаляемая закладка",
            "Канал",
            "тег",
            _now_str(),
        )

        mock_bot = AsyncMock()
        mock_bot.id = 12345
        mock_bot.edit_message_text = AsyncMock()
        mock_bot.answer_callback_query = AsyncMock()

        user = User(id=user_id, is_bot=False, first_name="Test")
        chat = Chat(id=user_id, type="private")
        msg = Message(
            message_id=70,
            date=datetime.now(timezone.utc),
            chat=chat,
            from_user=user,
            text="Карточка",
        )

        cb = CallbackQuery(
            id="cb_del",
            from_user=user,
            chat_instance="ci",
            message=msg,
            data=f"bkdel_{saved_id}",
        )

        update = Update(update_id=103, callback_query=cb)
        await main.dp.feed_update(mock_bot, update)

        self.assertIsNone(database.get_saved_message_by_id(user_id, saved_id))
        self.assertTrue(
            any(
                len(call[1]) > 0 and type(call[1][0]).__name__ == "AnswerCallbackQuery"
                for call in mock_bot.mock_calls
            )
        )
