"""Тесты модуля data_transfer.py и его интеграции с main.py (MS3)."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.base import StorageKey
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    Chat,
    Document,
    Message,
    User,
)

os.environ.setdefault("BOT_TOKEN", "123456:TEST_TOKEN_FOR_UNIT_TESTS")

import data.database as database  # noqa: E402
import data_transfer  # noqa: E402
import main  # noqa: E402


def _now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class TestDataTransferArchitecture(unittest.TestCase):
    """Архитектурные тесты изоляции, отсутствия импортов main и регистрации роутеров."""

    def test_guard_no_main_imports_in_data_transfer(self):
        """data_transfer.py НЕ должен импортировать main."""
        current_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        dt_path = os.path.join(current_dir, "data_transfer.py")
        with open(dt_path, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertNotIn("from main import", content)
        self.assertNotIn("import main", content)

    def test_reexports_on_main(self):
        """Все перемещённые символы должны быть доступны через main.<имя>."""
        exported_symbols = [
            "EXPORT_VERSION",
            "MAX_IMPORT_BOOKMARKS",
            "MAX_IMPORT_FEED",
            "MAX_IMPORT_FILE_BYTES",
            "MAX_IMPORT_REMINDERS",
            "MAX_IMPORT_SUBSCRIPTIONS",
            "ImportState",
            "_import_user_payload",
            "_prompt_import",
            "_send_export",
            "cb_export",
            "cb_import",
            "cb_import_cancel",
            "cmd_export",
            "cmd_import",
            "import_wrong_type",
            "process_import_file",
        ]
        for name in exported_symbols:
            self.assertTrue(
                hasattr(main, name),
                f"main is missing re-exported symbol: {name}",
            )
            self.assertIs(
                getattr(main, name),
                getattr(data_transfer, name),
                f"main.{name} is not identical to data_transfer.{name}",
            )

    def test_router_included_before_fallback(self):
        """data_transfer.router должен быть включен в dp.sub_routers до fallback_router."""
        sub_router_names = [r.name for r in main.dp.sub_routers]
        self.assertIn("data_transfer", sub_router_names)
        self.assertIn("fallback", sub_router_names)

        dt_idx = sub_router_names.index("data_transfer")
        fallback_idx = sub_router_names.index("fallback")
        self.assertLess(
            dt_idx,
            fallback_idx,
            "data_transfer router must be evaluated before fallback router",
        )

    def test_handler_order_priority_over_fallback(self):
        """Хендлеры data_transfer проверяются до catch_message из fallback."""

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
        for dt_handler in ["cmd_export", "cmd_import", "process_import_file", "import_wrong_type"]:
            self.assertIn(dt_handler, handler_names)
            idx = handler_names.index(dt_handler)
            self.assertLess(
                idx,
                catch_idx,
                f"{dt_handler} must be evaluated before catch_message",
            )


class TestDataTransferFlow(unittest.IsolatedAsyncioTestCase):
    """Функциональные тесты экспорта/импорта данных через роутер и функции модуля."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_dt.db")
        database.DB_PATH = self.db_path
        database.init_db()

        self.storage = MemoryStorage()
        self.bot = AsyncMock()
        self.bot.id = 12345
        self.bot.send_message = AsyncMock()
        self.bot.send_document = AsyncMock()
        self.bot.download = AsyncMock()
        data_transfer.configure(bot=self.bot)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _get_fsm_context(self, user_id: int) -> FSMContext:
        key = StorageKey(bot_id=self.bot.id, chat_id=user_id, user_id=user_id)
        return FSMContext(storage=self.storage, key=key)

    async def test_cmd_export_sends_json_document(self):
        """Команда /export формирует и отправляет JSON-документ со структурой данных."""
        user_id = 9001
        database.add_subscription(
            user_id=user_id,
            channel_username="habr_test",
            channel_title="Хабр",
            period="daily",
            last_scraped_at=_now_str(),
            next_send_at=_now_str(),
        )
        database.add_saved_message(
            user_id=user_id,
            full_text="Тестовая закладка",
            source_name="Канал",
            tag="работа",
            saved_at=_now_str(),
        )

        message = AsyncMock(spec=Message)
        message.from_user = User(id=user_id, is_bot=False, first_name="Test")
        message.chat = Chat(id=user_id, type="private")
        message.bot = self.bot

        state = self._get_fsm_context(user_id)
        await state.set_state(data_transfer.ImportState.waiting_for_file)

        await data_transfer.cmd_export(message, state)

        # Состояние должно быть очищено
        self.assertIsNone(await state.get_state())

        self.bot.send_document.assert_awaited_once()
        call_args = self.bot.send_document.call_args
        sent_user_id = call_args.args[0]
        sent_doc = call_args.args[1]
        caption = call_args.kwargs.get("caption", "")

        self.assertEqual(sent_user_id, user_id)
        self.assertIsInstance(sent_doc, BufferedInputFile)
        self.assertIn("unagi_export_9001_", sent_doc.filename)
        self.assertIn("Экспорт данных Unagi", caption)
        self.assertIn("Подписки: 1", caption)
        self.assertIn("Закладки: 1", caption)

        # Проверяем распаковку JSON из BufferedInputFile
        payload = json.loads(sent_doc.data.decode("utf-8"))
        self.assertEqual(payload["version"], data_transfer.EXPORT_VERSION)
        self.assertEqual(payload["app"], "unagi-reminder-bot")
        self.assertEqual(len(payload["subscriptions"]), 1)
        self.assertEqual(payload["subscriptions"][0]["channel_username"], "habr_test")
        self.assertEqual(len(payload["bookmarks"]), 1)
        self.assertEqual(payload["bookmarks"][0]["full_text"], "Тестовая закладка")

    async def test_cb_export_calls_send_export(self):
        """Нажатие callback-кнопки data_export отвечает на запрос и отправляет документ."""
        user_id = 9002
        callback = AsyncMock(spec=CallbackQuery)
        callback.from_user = User(id=user_id, is_bot=False, first_name="Test")
        callback.bot = self.bot
        callback.answer = AsyncMock()

        await data_transfer.cb_export(callback)

        callback.answer.assert_awaited_once_with("📤 Готовлю файл...")
        self.bot.send_document.assert_awaited_once()
        self.assertEqual(self.bot.send_document.call_args.args[0], user_id)

    async def test_cmd_import_and_cb_import_prompt(self):
        """Команда /import и кнопка data_import переводят в ImportState.waiting_for_file и отправляют prompt."""
        user_id = 9003
        state = self._get_fsm_context(user_id)

        message = AsyncMock(spec=Message)
        message.from_user = User(id=user_id, is_bot=False, first_name="Test")
        message.bot = self.bot

        await data_transfer.cmd_import(message, state)

        self.assertEqual(await state.get_state(), data_transfer.ImportState.waiting_for_file.state)
        self.bot.send_message.assert_awaited_once()
        prompt_call = self.bot.send_message.call_args
        self.assertEqual(prompt_call.args[0], user_id)
        self.assertIn("Пришли JSON-файл", prompt_call.args[1])
        kb = prompt_call.kwargs.get("reply_markup")
        self.assertIsNotNone(kb)
        self.assertEqual(kb.inline_keyboard[0][0].callback_data, "import_cancel")

        # Проверка cb_import
        callback = AsyncMock(spec=CallbackQuery)
        callback.from_user = User(id=user_id, is_bot=False, first_name="Test")
        callback.bot = self.bot
        callback.answer = AsyncMock()
        await data_transfer.cb_import(callback, state)
        callback.answer.assert_awaited_once()

    async def test_cb_import_cancel(self):
        """Callback import_cancel очищает FSM и редактирует сообщение."""
        user_id = 9004
        state = self._get_fsm_context(user_id)
        await state.set_state(data_transfer.ImportState.waiting_for_file)

        callback = AsyncMock(spec=CallbackQuery)
        callback.from_user = User(id=user_id, is_bot=False, first_name="Test")
        callback.answer = AsyncMock()
        callback_msg = AsyncMock(spec=Message)
        callback_msg.edit_text = AsyncMock()
        callback.message = callback_msg

        await data_transfer.cb_import_cancel(callback, state)

        self.assertIsNone(await state.get_state())
        callback.answer.assert_awaited_once()
        callback_msg.edit_text.assert_awaited_once()
        self.assertIn("Импорт отменён", callback_msg.edit_text.call_args.args[0])

    async def test_process_import_file_not_json(self):
        """Файл с не-JSON расширением и неподходящим mime-типом отклоняется."""
        user_id = 9005
        state = self._get_fsm_context(user_id)
        await state.set_state(data_transfer.ImportState.waiting_for_file)

        doc = Document(
            file_id="doc1",
            file_unique_id="uniq1",
            file_name="picture.png",
            mime_type="image/png",
            file_size=1000,
        )
        msg = AsyncMock(spec=Message)
        msg.document = doc
        msg.from_user = User(id=user_id, is_bot=False, first_name="Test")
        msg.answer = AsyncMock()
        msg.bot = self.bot

        await data_transfer.process_import_file(msg, state)

        msg.answer.assert_awaited_once()
        self.assertIn("Это не похоже на JSON-файл", msg.answer.call_args.args[0])
        # Состояние не сброшено
        self.assertEqual(await state.get_state(), data_transfer.ImportState.waiting_for_file.state)

    async def test_process_import_file_too_large(self):
        """Файл, превышающий MAX_IMPORT_FILE_BYTES, отклоняется."""
        user_id = 9006
        state = self._get_fsm_context(user_id)
        await state.set_state(data_transfer.ImportState.waiting_for_file)

        doc = Document(
            file_id="doc2",
            file_unique_id="uniq2",
            file_name="huge.json",
            mime_type="application/json",
            file_size=data_transfer.MAX_IMPORT_FILE_BYTES + 100,
        )
        msg = AsyncMock(spec=Message)
        msg.document = doc
        msg.from_user = User(id=user_id, is_bot=False, first_name="Test")
        msg.answer = AsyncMock()
        msg.bot = self.bot

        await data_transfer.process_import_file(msg, state)

        msg.answer.assert_awaited_once()
        self.assertIn("Файл слишком большой", msg.answer.call_args.args[0])

    async def test_import_wrong_type(self):
        """Текст или не-документ в состоянии ImportState.waiting_for_file напоминает прислать .json."""
        msg = AsyncMock(spec=Message)
        msg.answer = AsyncMock()

        await data_transfer.import_wrong_type(msg)

        msg.answer.assert_awaited_once()
        self.assertIn("Пришли именно файл .json", msg.answer.call_args.args[0])

    async def test_export_import_roundtrip_restores_data(self):
        """Полный цикл экспорт -> импорт через существующие тестовые хелперы корректно восстанавливает данные."""
        user_a = 9101
        user_b = 9102

        # Создаем исходные данные у user_a:
        # 1. Настройки дайджеста
        database.upsert_digest_settings(
            user_a,
            "daily",
            send_hour=11,
            send_minute=30,
        )
        # 2. Подписка на дайджест с тегом и паузой
        sub_id = database.add_subscription(
            user_id=user_a,
            channel_username="daily_channel",
            channel_title="Дневной канал",
            period="daily",
            last_scraped_at=_now_str(),
            next_send_at=_now_str(),
            tag="новости",
        )
        database.set_subscription_paused(user_a, sub_id, True)

        # 3. Подписка на ленту
        feed_sub_id = database.add_feed_subscription(
            user_id=user_a,
            channel_username="feed_chan",
            channel_title="Канал ленты",
            tag="технологии",
        )
        database.set_feed_paused(user_a, feed_sub_id, True)

        # 4. Закладка
        database.add_saved_message(
            user_id=user_a,
            full_text="Текст важной закладки",
            source_name="Источник",
            tag="исследования",
            saved_at=_now_str(),
        )

        # 5. Будущее напоминание
        future_send = database.serialize_datetime(database.utc_now() + timedelta(days=2))
        database.add_message(
            user_id=user_a,
            message_id=None,
            send_at=future_send,
            text_preview="Короткое превью",
            source_name="Напоминание",
            full_text="Полный текст напоминания",
            note="Важная заметка",
            source_url="https://t.me/c/123/456",
        )

        # Экспортируем данные user_a
        exported_data = database.export_user_data(user_a)
        payload = {
            "app": "unagi-reminder-bot",
            "version": data_transfer.EXPORT_VERSION,
            "exported_at": database.serialize_datetime(database.utc_now()),
            **exported_data,
        }

        # Эмулируем загрузку файла через process_import_file для user_b
        raw_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        async def fake_download(doc, destination):
            destination.write(raw_bytes)

        self.bot.download.side_effect = fake_download

        state_b = self._get_fsm_context(user_b)
        await state_b.set_state(data_transfer.ImportState.waiting_for_file)

        doc = Document(
            file_id="doc_valid",
            file_unique_id="uniq_valid",
            file_name="unagi_export_9101_20261018.json",
            mime_type="application/json",
            file_size=len(raw_bytes),
        )
        msg_b = AsyncMock(spec=Message)
        msg_b.document = doc
        msg_b.from_user = User(id=user_b, is_bot=False, first_name="Recipient")
        msg_b.answer = AsyncMock()
        msg_b.bot = self.bot

        await data_transfer.process_import_file(msg_b, state_b)

        # FSM сброшен
        self.assertIsNone(await state_b.get_state())

        # Ответ с отчетом отправлен
        msg_b.answer.assert_awaited_once()
        report_text = msg_b.answer.call_args.args[0]
        self.assertIn("Импорт завершён", report_text)
        self.assertIn("Подписки: +1", report_text)
        self.assertIn("Лента: +1", report_text)
        self.assertIn("Закладки: +1", report_text)
        self.assertIn("Напоминания: +1", report_text)

        # Проверяем созданные данные в БД для user_b:
        subs_b = database.get_user_subscriptions(user_b)
        self.assertEqual(len(subs_b), 1)
        self.assertEqual(subs_b[0]["channel_username"], "daily_channel")
        self.assertEqual(subs_b[0]["tag"], "новости")
        self.assertEqual(subs_b[0]["is_paused"], 1)

        feed_sub_b = database.get_feed_subscription_by_username(user_b, "feed_chan")
        self.assertIsNotNone(feed_sub_b)
        self.assertEqual(feed_sub_b["tag"], "технологии")
        self.assertEqual(feed_sub_b["is_paused"], 1)

        bms_b = database.get_saved_messages(user_b)
        self.assertEqual(len(bms_b), 1)
        self.assertEqual(bms_b[0]["full_text"], "Текст важной закладки")
        self.assertEqual(bms_b[0]["tag"], "исследования")

        rems_b = database.get_user_messages(user_b)
        self.assertEqual(len(rems_b), 1)
        self.assertEqual(rems_b[0]["full_text"], "Полный текст напоминания")
        self.assertEqual(rems_b[0]["note"], "Важная заметка")
        self.assertEqual(rems_b[0]["source_url"], "https://t.me/c/123/456")

        ds_b = database.get_digest_settings(user_b)["daily"]
        self.assertEqual(ds_b["send_hour"], 11)
        self.assertEqual(ds_b["send_minute"], 30)
