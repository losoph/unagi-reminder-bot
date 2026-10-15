"""SEC1 — регрессионные тесты по 12 пунктам ревью безопасности.

Каждый тест воспроизводит конкретный дефект из
docs/reviews/2026-10-16-security-dsh.md и падал на коде до исправления.
"""

import os

os.environ.setdefault("BOT_TOKEN", "123456:TEST_TOKEN_FOR_UNIT_TESTS")

import asyncio  # noqa: E402
import sqlite3  # noqa: E402
import tempfile  # noqa: E402
import unittest  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402
from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

from aiogram.types import CallbackQuery, Chat, Message, User  # noqa: E402

import data.database as db  # noqa: E402
import main  # noqa: E402

UTC = timezone.utc


def _now_str() -> str:
    return db.serialize_datetime(db.utc_now())


class Sec1TestBase(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp_dir.name, "sec1.db")
        self.orig_db_path = db.DB_PATH
        db.DB_PATH = self.db_path
        db.init_db()
        main._expensive_command_last.clear()

    def tearDown(self):
        db.DB_PATH = self.orig_db_path
        main._expensive_command_last.clear()
        self.tmp_dir.cleanup()

    def _add_sub(self, user_id, username="chan_one", period="daily"):
        return db.add_subscription(
            user_id,
            username,
            f"Канал {username}",
            period,
            _now_str(),
            _now_str(),
        )

    def _callback(self, user_id, data):
        cb = MagicMock()
        cb.from_user.id = user_id
        cb.message.chat.id = user_id
        cb.message.text = "original"
        cb.message.caption = None
        cb.message.edit_text = AsyncMock()
        cb.answer = AsyncMock()
        cb.data = data
        return cb


class TestP1Ownership(Sec1TestBase):
    """П.1 — IDOR через callback_data и аудит обработчиков с id из callback."""

    def test_db_retry_execution_checks_owner(self):
        owner, attacker = 101, 202
        sched = _now_str()
        self._add_sub(owner)
        exec_id = db.record_digest_execution_start(owner, "daily", sched)
        db.record_digest_execution_finish(exec_id, status="failed", error_message="boom")

        self.assertFalse(db.retry_digest_execution(exec_id, attacker))
        exec_row = db.get_digest_execution_by_id(exec_id)
        self.assertEqual(exec_row["status"], "failed")
        self.assertEqual(exec_row["error_message"], "boom")

        self.assertTrue(db.retry_digest_execution(exec_id, owner))
        self.assertEqual(db.get_digest_execution_by_id(exec_id)["status"], "retrying")

    def test_alert_retry_foreign_callback_denied(self):
        owner, attacker = 111, 222
        sched = _now_str()
        self._add_sub(owner)
        exec_id = db.record_digest_execution_start(owner, "daily", sched)
        db.record_digest_execution_finish(exec_id, status="failed", error_message="boom")

        cb = self._callback(attacker, f"alert_retry_{exec_id}")
        asyncio.run(main.handle_alert_retry(cb))

        self.assertTrue(any("Нет доступа" in str(a) for a in cb.answer.call_args[0]))
        self.assertEqual(db.get_digest_execution_by_id(exec_id)["status"], "failed")
        cb.message.edit_text.assert_not_called()

    def test_unsub_foreign_callback_denied(self):
        owner, attacker = 333, 444
        sub_id = self._add_sub(owner)
        cb = self._callback(attacker, f"unsub_{sub_id}")
        asyncio.run(main.handle_unsub(cb))

        self.assertTrue(any("Нет доступа" in str(a) for a in cb.answer.call_args[0]))
        self.assertEqual(db.count_user_subscriptions(owner), 1)

    def test_delete_helpers_are_user_scoped(self):
        owner, attacker = 555, 666
        rem_id = db.add_message(owner, None, _now_str(), "текст", "источник")
        saved_id = db.add_saved_message(owner, "закладка", "источник", "тег", _now_str())
        sub_id = self._add_sub(owner)

        db.delete_message(attacker, rem_id)
        db.delete_saved_message(attacker, saved_id)
        db.delete_subscription(attacker, sub_id)
        db.set_subscription_paused(attacker, sub_id, True)

        self.assertEqual(len(db.get_user_messages(owner)), 1)
        self.assertEqual(len(db.get_saved_messages(owner)), 1)
        self.assertEqual(db.count_user_subscriptions(owner), 1)
        self.assertFalse(db.get_subscription_by_id(owner, sub_id)["is_paused"])

    def test_callback_identity_prefers_from_user(self):
        cb = MagicMock()
        cb.from_user.id = 777
        cb.message.chat.id = 888
        self.assertEqual(main.user_id_from_callback(cb), 777)


class TestP2SubscriptionQuota(Sec1TestBase):
    """П.2 — квота подписок и LIMIT в выборке due_until."""

    def test_add_subscription_enforces_quota(self):
        user = 1001
        with patch.object(db, "MAX_DIGEST_SUBSCRIPTIONS", 2):
            self._add_sub(user, "chan_a")
            self._add_sub(user, "chan_b")
            with self.assertRaises(ValueError):
                self._add_sub(user, "chan_c")
            self.assertEqual(db.count_user_subscriptions(user), 2)

    def test_import_respects_quota(self):
        user = 1002
        payload = {
            "subscriptions": [
                {"channel_username": f"chan_{i}", "period": "daily", "channel_title": "c"}
                for i in range(5)
            ]
        }
        with patch.object(db, "MAX_DIGEST_SUBSCRIPTIONS", 2):
            report = main._import_user_payload(user, payload)
        self.assertIn("Подписки: +2", report)
        self.assertEqual(db.count_user_subscriptions(user), 2)

    def test_due_until_is_limited(self):
        user = 1003
        until = db.serialize_datetime(db.utc_now() + timedelta(days=2))
        self._add_sub(user, "chan_a")
        self._add_sub(user, "chan_b")
        self._add_sub(user, "chan_c")
        with patch.object(db, "MAX_DIGEST_SUBSCRIPTIONS", 2):
            due = db.get_user_subscriptions_due_until(user, until)
        self.assertEqual(len(due), 2)


class TestP3ExpensiveCooldown(Sec1TestBase):
    """П.3 — общий per-user кулдаун для дорогих команд."""

    def test_cooldown_helper(self):
        user = 2001
        self.assertEqual(main.check_expensive_command_cooldown(user), 0)
        self.assertGreater(main.check_expensive_command_cooldown(user), 0)

    def test_check_command_cooldown(self):
        user = 2002
        message = MagicMock()
        message.from_user.id = user
        message.chat.id = user
        message.answer = AsyncMock()
        state = AsyncMock()

        asyncio.run(main.cmd_check(message, state))
        asyncio.run(main.cmd_check(message, state))
        texts = [str(call.args[0]) for call in message.answer.call_args_list]
        self.assertTrue(any("Подожди" in t for t in texts))

    def test_test_digest_cooldown(self):
        user = 2003
        message = MagicMock()
        message.from_user.id = user
        message.chat.id = user
        message.answer = AsyncMock()
        state = AsyncMock()

        asyncio.run(main.cmd_test_digest(message, state))
        asyncio.run(main.cmd_test_digest(message, state))
        texts = [str(call.args[0]) for call in message.answer.call_args_list]
        self.assertTrue(any("Подожди" in t for t in texts))


class TestP4EmailLimits(Sec1TestBase):
    """П.4 — суточные лимиты отправки писем на пользователя и адрес."""

    def test_email_send_log_counters(self):
        now = _now_str()
        db.record_email_send(1, "a@example.com", now)
        db.record_email_send(1, "b@example.com", now)
        db.record_email_send(2, "a@example.com", now)
        self.assertEqual(db.count_email_sends_for_user(1, _now_str()), 2)
        self.assertEqual(db.count_email_sends_for_address("a@example.com", _now_str()), 2)

    def test_address_limit_blocks_second_user(self):
        message = MagicMock()
        message.from_user.id = 3001
        message.chat.id = 3001
        message.text = "target@example.com"
        message.answer = AsyncMock()
        state = AsyncMock()

        with patch.object(main, "EMAIL_DAILY_LIMIT_PER_USER", 10), patch.object(
            main, "EMAIL_DAILY_LIMIT_PER_ADDRESS", 1
        ), patch.object(main.email_digest, "send_email", AsyncMock(return_value=True)):
            asyncio.run(main.process_email_address(message, state))
            self.assertTrue(db.count_email_sends_for_address("target@example.com", _now_str()) >= 1)

            message2 = MagicMock()
            message2.from_user.id = 3002
            message2.chat.id = 3002
            message2.text = "target@example.com"
            message2.answer = AsyncMock()
            asyncio.run(main.process_email_address(message2, AsyncMock()))

        texts = [str(call.args[0]) for call in message2.answer.call_args_list]
        self.assertTrue(any("адрес" in t for t in texts), texts)


class TestP5PrivateChat(Sec1TestBase):
    """П.5 — пользовательские роутеры работают только в личных чатах."""

    def _message(self, chat_type: str):
        return Message(
            message_id=1,
            date=datetime.now(UTC),
            chat=Chat(id=100, type=chat_type),
            from_user=User(id=5, is_bot=False, first_name="t"),
            text="hi",
        )

    def _callback_query(self, chat_type: str):
        return CallbackQuery(
            id="1",
            from_user=User(id=5, is_bot=False, first_name="t"),
            chat_instance="c",
            data="x",
            message=self._message(chat_type),
        )

    def test_message_root_filter(self):
        for chat_type in ("group", "supergroup", "channel"):
            result, _ = asyncio.run(
                main.dp.message.check_root_filters(self._message(chat_type))
            )
            self.assertFalse(result, chat_type)
        result, _ = asyncio.run(main.dp.message.check_root_filters(self._message("private")))
        self.assertTrue(result)

    def test_callback_root_filter(self):
        for chat_type in ("group", "supergroup", "channel"):
            result, _ = asyncio.run(
                main.dp.callback_query.check_root_filters(self._callback_query(chat_type))
            )
            self.assertFalse(result, chat_type)
        result, _ = asyncio.run(
            main.dp.callback_query.check_root_filters(self._callback_query("private"))
        )
        self.assertTrue(result)

    def test_identity_helpers_prefer_from_user(self):
        message = MagicMock()
        message.from_user.id = 42
        message.chat.id = 99
        self.assertEqual(main.user_id_from_message(message), 42)


class TestP6DatabasePermissions(Sec1TestBase):
    """П.6 — БД и WAL/SHM закрыты правами 0600."""

    def test_db_files_are_owner_only(self):
        db.add_message(1, None, _now_str(), "x", "y")
        for suffix in ("", "-wal", "-shm"):
            path = f"{self.db_path}{suffix}"
            if os.path.exists(path):
                mode = os.stat(path).st_mode & 0o777
                self.assertEqual(mode, 0o600, f"{path} has {oct(mode)}")


class TestP9SelectionLimits(Sec1TestBase):
    """П.9 — LIMIT в выборках и квоты напоминаний/закладок."""

    def test_reminder_quota(self):
        user = 4001
        with patch.object(db, "MAX_USER_REMINDERS", 2):
            db.add_message(user, None, _now_str(), "a", "s")
            db.add_message(user, None, _now_str(), "b", "s")
            with self.assertRaises(ValueError):
                db.add_message(user, None, _now_str(), "c", "s")
        self.assertEqual(len(db.get_user_messages(user)), 2)

    def test_saved_quota(self):
        user = 4002
        with patch.object(db, "MAX_USER_SAVED_MESSAGES", 2):
            db.add_saved_message(user, "a", "s", "t", _now_str())
            db.add_saved_message(user, "b", "s", "t", _now_str())
            with self.assertRaises(ValueError):
                db.add_saved_message(user, "c", "s", "t", _now_str())
        self.assertEqual(len(db.get_saved_messages(user)), 2)

    def test_explicit_limits(self):
        user = 4003
        for i in range(5):
            db.add_message(user, None, _now_str(), f"r{i}", "s")
            db.add_saved_message(user, f"b{i}", "s", "t", _now_str())
        self.assertEqual(len(db.get_user_messages(user, limit=3)), 3)
        self.assertEqual(len(db.get_saved_messages(user, limit=2)), 2)
        exported = db.export_user_data(user)
        self.assertLessEqual(len(exported["reminders"]), db.MAX_USER_REMINDERS)
        self.assertLessEqual(len(exported["bookmarks"]), db.MAX_USER_SAVED_MESSAGES)


class TestP10HtmlAndUsername(Sec1TestBase):
    """П.10 — экранирование ссылок и валидация имени канала."""

    def test_channel_lines_escape_link_and_username(self):
        lines: list[str] = []
        main.append_digest_channel_lines(
            lines,
            sub_id=1,
            period="daily",
            channel_title="T",
            posts=[{"text": "x", "link": "https://t.me/a'><b>bad</b>"}],
            omitted_count=1,
            channel_username="evil'><b>",
        )
        joined = "\n".join(lines)
        self.assertNotIn("'><b>", joined)
        self.assertIn("&#x27;", joined)

    def test_username_validation(self):
        self.assertTrue(db.is_valid_channel_username("valid_name"))
        self.assertFalse(db.is_valid_channel_username("abc"))
        self.assertFalse(db.is_valid_channel_username("bad-name"))
        self.assertFalse(db.is_valid_channel_username("evil'><b"))

    def test_add_subscription_rejects_bad_username(self):
        with self.assertRaises(ValueError):
            self._add_sub(5001, "evil'><b>")


class TestP11SavedAtValidation(Sec1TestBase):
    """П.11 — импорт не принимает произвольный saved_at."""

    def test_import_invalid_saved_at_is_normalized(self):
        user = 6001
        payload = {
            "bookmarks": [
                {"full_text": "текст", "source_name": "Импорт", "tag": "t", "saved_at": "not-a-date"}
            ]
        }
        main._import_user_payload(user, payload)
        rows = db.get_saved_messages(user)
        self.assertEqual(len(rows), 1)
        # Не должно бросать ValueError при рендере карточки закладки.
        db.parse_db_datetime(rows[0]["saved_at"])


class TestP12EmailAttempts(Sec1TestBase):
    """П.12 — счётчик попыток кода не сбрасывается повторной отправкой."""

    def test_resend_does_not_reset_attempts(self):
        user = 7001
        now = db.utc_now()
        now_str = db.serialize_datetime(now)
        exp = db.serialize_datetime(now + timedelta(minutes=15))
        db.start_email_verification(user, "a@example.com", "111111", now_str, exp)
        for _ in range(db.EMAIL_CODE_MAX_ATTEMPTS):
            db.confirm_email_code(user, "000000", now_str)
        self.assertEqual(db.get_user_email(user)["code_attempts"], db.EMAIL_CODE_MAX_ATTEMPTS)

        db.start_email_verification(user, "a@example.com", "222222", now_str, exp)
        self.assertEqual(db.get_user_email(user)["code_attempts"], db.EMAIL_CODE_MAX_ATTEMPTS)
        self.assertEqual(db.confirm_email_code(user, "222222", now_str), "too_many")


class TestP13SubscriptionIndex(Sec1TestBase):
    """П.13 — индекс подписок не пересоздаётся, add_subscription транзакционен."""

    def test_unique_index_exists_and_survives_restart(self):
        def index_exists():
            with sqlite3.connect(self.db_path) as conn:
                row = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='index' "
                    "AND name='uq_subscriptions_user_channel_period_active'"
                ).fetchone()
            return row is not None

        self.assertTrue(index_exists())
        db.init_db()
        self.assertTrue(index_exists())

    def test_add_subscription_is_idempotent(self):
        user = 8001
        first = self._add_sub(user, "chan_dup")
        second = self._add_sub(user, "chan_dup")
        self.assertEqual(first, second)
        self.assertEqual(db.count_user_subscriptions(user), 1)


class TestP14ChannelPostsLimit(Sec1TestBase):
    """П.14 — get_channel_posts_since не вычитывает весь backlog."""

    def test_posts_since_is_limited(self):
        posts = [
            {"id": i, "text": f"p{i}", "link": f"https://t.me/c/{i}", "time": db.utc_now()}
            for i in range(1, 6)
        ]
        db.upsert_channel_posts("limit_chan", posts, "test")
        marker = db.serialize_datetime(db.utc_now() - timedelta(days=1))
        limited = db.get_channel_posts_since("limit_chan", marker, limit=2)
        self.assertEqual(len(limited), 2)
        limited_by_id = db.get_channel_posts_since("limit_chan", marker, last_post_id=0, limit=2)
        self.assertEqual(len(limited_by_id), 2)


if __name__ == "__main__":
    unittest.main()
