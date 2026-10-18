"""tests/test_screen_registry.py — Тесты реестра экранов FSM (RS1)."""

import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("BOT_TOKEN", "123456:TEST_TOKEN_FOR_UNIT_TESTS")

from aiogram import types  # noqa: E402
from aiogram.fsm.context import FSMContext  # noqa: E402

import main  # noqa: E402
import screen_registry  # noqa: E402
from screen_registry import ScreenRegistry  # noqa: E402


class TestScreenRegistryArchitecture(unittest.TestCase):
    """Архитектурные тесты изоляции и обратной совместимости screen_registry."""

    def test_guard_no_main_imports_in_screen_registry(self):
        """screen_registry.py НЕ должен импортировать main."""
        repo_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        sr_path = os.path.join(repo_dir, "screen_registry.py")
        with open(sr_path, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertNotIn("from main import", content)
        self.assertNotIn("import main", content)

    def test_reexports_on_main(self):
        """Все ключевые символы реестра экранов должны быть доступны через main.<имя>."""
        symbols = [
            "ScreenRegistry",
            "default_registry",
            "get_screen_handler",
            "has_screen",
            "register_screen",
            "unregister_screen",
            "render_screen",
        ]
        for name in symbols:
            self.assertTrue(
                hasattr(main, name),
                f"Символ {name} должен быть доступен в main",
            )


class TestScreenRegistryCore(unittest.IsolatedAsyncioTestCase):
    """Модульные тесты функциональности класса ScreenRegistry."""

    def setUp(self):
        self.registry = ScreenRegistry()
        self.state = MagicMock(spec=FSMContext)
        self.target = MagicMock(spec=types.Message)

    def test_register_and_get(self):
        handler = AsyncMock()
        self.registry.register("test_screen", handler)
        self.assertEqual(self.registry.get("test_screen"), handler)
        self.assertTrue(self.registry.has("test_screen"))
        self.assertIn("test_screen", self.registry)
        self.assertEqual(len(self.registry), 1)

    def test_register_decorator(self):
        @self.registry.register("dec_screen")
        async def my_handler(chat_id, target, state, args):
            pass

        self.assertEqual(self.registry.get("dec_screen"), my_handler)
        self.assertEqual(self.registry.screens, ["dec_screen"])

    def test_unregister(self):
        handler = AsyncMock()
        self.registry.register("to_remove", handler)
        removed = self.registry.unregister("to_remove")
        self.assertEqual(removed, handler)
        self.assertFalse(self.registry.has("to_remove"))
        self.assertIsNone(self.registry.unregister("non_existent"))

    def test_clear(self):
        self.registry.register("s1", AsyncMock())
        self.registry.register("s2", AsyncMock())
        self.assertEqual(len(self.registry), 2)
        self.registry.clear()
        self.assertEqual(len(self.registry), 0)

    def test_screens_sorted(self):
        self.registry.register("beta", AsyncMock())
        self.registry.register("alpha", AsyncMock())
        self.assertEqual(self.registry.screens, ["alpha", "beta"])

    def test_set_default_handler(self):
        fallback = AsyncMock()
        self.registry.set_default(fallback)
        self.assertEqual(self.registry.default_handler, fallback)

    def test_set_default_decorator(self):
        @self.registry.set_default
        async def fallback(chat_id, target, state):
            pass

        self.assertEqual(self.registry.default_handler, fallback)

    async def test_render_invokes_3_arg_handler(self):
        handler = AsyncMock()
        self.registry.register("three_args", handler)
        await self.registry.render(123, {"screen": "three_args"}, self.target, self.state)
        handler.assert_awaited_once_with(123, self.target, self.state)

    async def test_render_invokes_4_arg_handler(self):
        handler = MagicMock()

        async def four_args(chat_id, target, state, args):
            handler(chat_id, target, state, args)

        self.registry.register("four_args", four_args)
        spec = {"screen": "four_args", "args": {"key": "val"}}
        await self.registry.render(456, spec, self.target, self.state)
        handler.assert_called_once_with(456, self.target, self.state, {"key": "val"})

    async def test_render_invokes_kwargs_handler(self):
        handler = MagicMock()

        async def kwargs_handler(chat_id, target, state, **kwargs):
            handler(chat_id, target, state, **kwargs)

        self.registry.register("kw_screen", kwargs_handler)
        spec = {"screen": "kw_screen", "args": {"foo": "bar", "num": 42}}
        await self.registry.render(789, spec, self.target, self.state)
        handler.assert_called_once_with(789, self.target, self.state, foo="bar", num=42)

    async def test_render_invokes_screen_param_handler(self):
        handler = MagicMock()

        async def screen_param_handler(chat_id, screen, target, state):
            handler(chat_id, screen, target, state)

        self.registry.register("spec_screen", screen_param_handler)
        spec = {"screen": "spec_screen", "args": {"foo": 1}}
        await self.registry.render(111, spec, self.target, self.state)
        handler.assert_called_once_with(111, spec, self.target, self.state)

    async def test_render_string_screen_spec(self):
        handler = AsyncMock()
        self.registry.register("simple", handler)
        await self.registry.render(123, "simple", self.target, self.state)
        handler.assert_awaited_once_with(123, self.target, self.state)

    async def test_render_unknown_screen_calls_default(self):
        fallback = AsyncMock()
        self.registry.set_default(fallback)
        await self.registry.render(123, {"screen": "unknown"}, self.target, self.state)
        fallback.assert_awaited_once_with(123, self.target, self.state)

    async def test_render_none_screen_calls_default(self):
        fallback = AsyncMock()
        self.registry.set_default(fallback)
        await self.registry.render(123, None, self.target, self.state)
        fallback.assert_awaited_once_with(123, self.target, self.state)

    async def test_render_empty_dict_calls_default(self):
        fallback = AsyncMock()
        self.registry.set_default(fallback)
        await self.registry.render(123, {}, self.target, self.state)
        fallback.assert_awaited_once_with(123, self.target, self.state)

    async def test_render_unknown_without_default_raises(self):
        with self.assertRaises(KeyError):
            await self.registry.render(123, {"screen": "unregistered"}, self.target, self.state)


class TestMainRenderScreenIntegration(unittest.IsolatedAsyncioTestCase):
    """Интеграционные тесты диспетчеризации экранов через main.render_screen."""

    def setUp(self):
        self.chat_id = 99999
        self.state = MagicMock(spec=FSMContext)
        self.state.clear = AsyncMock()

    async def test_registered_screens_present(self):
        """Все 11 экранов должны быть зарегистрированы в screen_registry."""
        expected = [
            "kb_detail",
            "kb_list",
            "kb_move",
            "kb_tags",
            "manual_date",
            "manual_hour",
            "manual_minute",
            "rem_detail",
            "rem_list",
            "rem_move",
            "rem_tags",
        ]
        for screen_name in expected:
            self.assertTrue(
                screen_registry.has_screen(screen_name),
                f"Экран {screen_name} должен быть зарегистрирован",
            )

    async def test_render_kb_tags(self):
        target = MagicMock(spec=types.Message)
        with patch("main.render_kb_tags_screen", new_callable=AsyncMock) as mock_render:
            await main.render_screen(self.chat_id, {"screen": "kb_tags"}, target, self.state)
            mock_render.assert_awaited_once_with(self.chat_id, target, self.state)

    async def test_render_kb_list(self):
        target = MagicMock(spec=types.Message)
        spec = {"screen": "kb_list", "args": {"tag": "tech", "page": 2}}
        with patch("main.render_kb_list_screen", new_callable=AsyncMock) as mock_render:
            await main.render_screen(self.chat_id, spec, target, self.state)
            mock_render.assert_awaited_once_with(self.chat_id, "tech", 2, target, self.state)

    async def test_render_kb_detail(self):
        target = MagicMock(spec=types.Message)
        spec = {"screen": "kb_detail", "args": {"msg_id": 42}}
        with patch("main.render_kb_detail_screen", new_callable=AsyncMock) as mock_render:
            await main.render_screen(self.chat_id, spec, target, self.state)
            mock_render.assert_awaited_once_with(self.chat_id, 42, target, self.state)

    async def test_render_kb_move(self):
        target = MagicMock(spec=types.Message)
        spec = {"screen": "kb_move", "args": {"msg_id": 42}}
        with patch("main.render_kb_move_screen", new_callable=AsyncMock) as mock_render:
            await main.render_screen(self.chat_id, spec, target, self.state)
            mock_render.assert_awaited_once_with(self.chat_id, 42, target, self.state)

    async def test_render_rem_tags(self):
        target = MagicMock(spec=types.Message)
        with patch("main.render_rem_tags_screen", new_callable=AsyncMock) as mock_render:
            await main.render_screen(self.chat_id, {"screen": "rem_tags"}, target, self.state)
            mock_render.assert_awaited_once_with(self.chat_id, target, self.state)

    async def test_render_rem_list(self):
        target = MagicMock(spec=types.Message)
        spec = {"screen": "rem_list", "args": {"tag": "todo", "page": 3}}
        with patch("main.render_rem_list_screen", new_callable=AsyncMock) as mock_render:
            await main.render_screen(self.chat_id, spec, target, self.state)
            mock_render.assert_awaited_once_with(self.chat_id, "todo", 3, target, self.state)

    async def test_render_rem_detail(self):
        target = MagicMock(spec=types.Message)
        spec = {"screen": "rem_detail", "args": {"msg_id": 101}}
        with patch("main.render_rem_detail_screen", new_callable=AsyncMock) as mock_render:
            await main.render_screen(self.chat_id, spec, target, self.state)
            mock_render.assert_awaited_once_with(self.chat_id, 101, target, self.state)

    async def test_render_rem_move(self):
        target = MagicMock(spec=types.Message)
        spec = {"screen": "rem_move", "args": {"msg_id": 101}}
        with patch("main.render_rem_move_screen", new_callable=AsyncMock) as mock_render:
            await main.render_screen(self.chat_id, spec, target, self.state)
            mock_render.assert_awaited_once_with(self.chat_id, 101, target, self.state)

    async def test_render_manual_date_message(self):
        target = MagicMock(spec=types.Message)
        target.answer = AsyncMock()
        await main.render_screen(self.chat_id, {"screen": "manual_date"}, target, self.state)
        target.answer.assert_awaited_once()
        args, kwargs = target.answer.call_args
        self.assertEqual(args[0], "Выбери дату:")
        self.assertIn("reply_markup", kwargs)

    async def test_render_manual_date_callback(self):
        target = MagicMock(spec=types.CallbackQuery)
        target.answer = AsyncMock()
        target.message = MagicMock(spec=types.Message)
        target.message.edit_text = AsyncMock()
        await main.render_screen(self.chat_id, {"screen": "manual_date"}, target, self.state)
        target.message.edit_text.assert_awaited_once()
        target.answer.assert_awaited_once()

    async def test_render_manual_hour(self):
        target = MagicMock(spec=types.Message)
        target.answer = AsyncMock()
        spec = {"screen": "manual_hour", "args": {"selected_date": "20.10.2026"}}
        await main.render_screen(self.chat_id, spec, target, self.state)
        target.answer.assert_awaited_once()
        args, kwargs = target.answer.call_args
        self.assertIn("20.10.2026", args[0])
        self.assertIn("reply_markup", kwargs)

    async def test_render_manual_minute(self):
        target = MagicMock(spec=types.Message)
        target.answer = AsyncMock()
        spec = {"screen": "manual_minute", "args": {"selected_hour": 15}}
        await main.render_screen(self.chat_id, spec, target, self.state)
        target.answer.assert_awaited_once()
        args, kwargs = target.answer.call_args
        self.assertIn("15:00", args[0])
        self.assertIn("reply_markup", kwargs)

    async def test_render_fallback_unknown_screen_message(self):
        target = MagicMock(spec=types.Message)
        target.answer = AsyncMock()
        await main.render_screen(self.chat_id, {"screen": "non_existent_screen"}, target, self.state)
        self.state.clear.assert_awaited_once()
        target.answer.assert_awaited_once()
        args, kwargs = target.answer.call_args
        self.assertEqual(args[0], main.WELCOME_TEXT)
        self.assertIn("reply_markup", kwargs)

    async def test_render_fallback_unknown_screen_callback(self):
        target = MagicMock(spec=types.CallbackQuery)
        target.answer = AsyncMock()
        target.message = MagicMock(spec=types.Message)
        target.message.answer = AsyncMock()
        await main.render_screen(self.chat_id, {"screen": "non_existent_screen"}, target, self.state)
        self.state.clear.assert_awaited_once()
        target.message.answer.assert_awaited_once()
        target.answer.assert_awaited_once()
