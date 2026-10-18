import asyncio
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("BOT_TOKEN", "123456:TEST_TOKEN_FOR_UNIT_TESTS")

import ai_assistant
from digest_alerts import process_pending_digest_alerts, process_pending_recovery_alerts
import main


class TestTH1ToThread(unittest.IsolatedAsyncioTestCase):
    """TH1: Verify heavy SQLite database calls in delivery loops run via asyncio.to_thread."""

    async def test_digest_cycle_calls_get_due_subscriptions_via_to_thread(self):
        real_to_thread = asyncio.to_thread
        to_thread_calls = []

        async def spy_to_thread(func, *args, **kwargs):
            to_thread_calls.append(func)
            if func == main.get_due_subscriptions:
                return []
            return await real_to_thread(func, *args, **kwargs)

        fake_session = MagicMock()
        fake_semaphore = asyncio.Semaphore(1)

        with patch("asyncio.to_thread", side_effect=spy_to_thread), \
             patch("main.count_due_subscriptions", return_value=1):
            backlog = await main.run_digest_cycle(fake_session, fake_semaphore)

        self.assertEqual(backlog, 0)
        self.assertIn(main.get_due_subscriptions, to_thread_calls)

    async def test_digest_cycle_calls_plan_due_editions_via_to_thread(self):
        real_to_thread = asyncio.to_thread
        to_thread_calls = []

        sub = (1, 100, "chan", "Chan", "daily", "2026-10-18 00:00:00", 0, None, "2026-10-18 08:00:00")

        async def spy_to_thread(func, *args, **kwargs):
            to_thread_calls.append(func)
            if func == main.get_due_subscriptions:
                return [sub]
            if func == main.plan_due_editions:
                return {100: [sub]}, 0
            return await real_to_thread(func, *args, **kwargs)

        fake_session = MagicMock()
        fake_semaphore = asyncio.Semaphore(1)

        with patch("asyncio.to_thread", side_effect=spy_to_thread), \
             patch("main.count_due_subscriptions", return_value=1), \
             patch("main.prefetch_due_channels", new_callable=AsyncMock, return_value={}), \
             patch("main.process_user_edition", new_callable=AsyncMock, return_value={"failures": 0, "delivered_users": 1, "delivered_posts": 1}), \
             patch("main.record_digest_cycle_heartbeat"):
            await main.run_digest_cycle(fake_session, fake_semaphore)

        self.assertIn(main.get_due_subscriptions, to_thread_calls)
        self.assertIn(main.plan_due_editions, to_thread_calls)

    async def test_prefetch_calls_upsert_channel_posts_via_to_thread(self):
        real_to_thread = asyncio.to_thread
        to_thread_calls = []

        async def spy_to_thread(func, *args, **kwargs):
            to_thread_calls.append(func)
            if func == main.upsert_channel_posts:
                return None
            return await real_to_thread(func, *args, **kwargs)

        sub = (1, 100, "testchan", "Title", "daily", "2026-10-18 00:00:00", 0, None)
        fake_posts = [{"id": 1, "time": "2026-10-18 01:00:00", "text": "Post 1"}]

        with patch("asyncio.to_thread", side_effect=spy_to_thread), \
             patch("main.is_channel_backlog_overloaded", return_value=False), \
             patch("main.get_channel_scan_cursor", return_value=(None, None)), \
             patch.object(main.channel_source, "fetch", new_callable=AsyncMock, return_value=(fake_posts, "telegram_web")), \
             patch("main.advance_channel_scan_cursor"):
            result = await main.prefetch_due_channels([sub], MagicMock(), asyncio.Semaphore(1))

        self.assertIn(main.upsert_channel_posts, to_thread_calls)
        self.assertEqual(result["testchan"]["posts"], fake_posts)

    async def test_fetch_subscription_posts_calls_db_via_to_thread(self):
        real_to_thread = asyncio.to_thread
        to_thread_calls = []

        now_dt = main.utc_now()

        async def spy_to_thread(func, *args, **kwargs):
            to_thread_calls.append(func)
            if func == main.upsert_channel_posts:
                return None
            if func == main.get_channel_posts_since:
                return [{"id": 1, "time": now_dt, "text": "Post 1"}]
            return await real_to_thread(func, *args, **kwargs)

        sub = (1, 100, "testchan", "Title", "daily", "2026-10-18 00:00:00", 0, None, "2026-10-18 08:00:00")
        fake_posts = [{"id": 1, "time": now_dt, "text": "Post 1"}]

        with patch("asyncio.to_thread", side_effect=spy_to_thread), \
             patch("main.resolve_digest_settings", return_value={}), \
             patch("main.get_next_digest_time", return_value=now_dt), \
             patch("main.get_channel_scan_cursor", return_value=(None, None)), \
             patch.object(main.channel_source, "fetch", new_callable=AsyncMock, return_value=(fake_posts, "telegram_web")), \
             patch("main.advance_channel_scan_cursor"), \
             patch("main.prioritize_channel_posts", return_value=(fake_posts, 0)):
            res = await main.fetch_subscription_posts(sub, MagicMock(), asyncio.Semaphore(1), "2026-10-18 08:00:00")

        self.assertEqual(res["status"], "ok")
        self.assertIn(main.upsert_channel_posts, to_thread_calls)
        self.assertIn(main.get_channel_posts_since, to_thread_calls)

    async def test_check_messages_calls_get_pending_messages_via_to_thread(self):
        to_thread_calls = []

        async def fake_to_thread(func, *args, **kwargs):
            to_thread_calls.append(func)
            if func == main.get_pending_messages:
                # Raise CancelledError to gracefully exit check_messages infinite loop
                raise asyncio.CancelledError()
            return func(*args, **kwargs)

        with patch("asyncio.to_thread", side_effect=fake_to_thread):
            with self.assertRaises(asyncio.CancelledError):
                await main.check_messages()

        self.assertIn(main.get_pending_messages, to_thread_calls)

    async def test_digest_alerts_calls_db_via_to_thread(self):
        to_thread_calls = []

        async def fake_to_thread(func, *args, **kwargs):
            to_thread_calls.append(func)
            if getattr(func, "__name__", "") in (
                "get_failed_digest_executions_pending_alert",
                "get_delivered_executions_pending_recovery_alert",
            ):
                return []
            return func(*args, **kwargs)

        bot_mock = MagicMock()
        with patch("asyncio.to_thread", side_effect=fake_to_thread):
            total = await process_pending_digest_alerts(bot_mock)
            recoveries = await process_pending_recovery_alerts(bot_mock)

        self.assertEqual(total, 0)
        self.assertEqual(recoveries, 0)
        called_names = [getattr(f, "__name__", "") for f in to_thread_calls]
        self.assertIn("get_failed_digest_executions_pending_alert", called_names)
        self.assertIn("get_delivered_executions_pending_recovery_alert", called_names)


class FakeResponseContextManager:
    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass


class TestHP1DeepSeekPing(unittest.IsolatedAsyncioTestCase):
    """HP1: Verify DeepSeek key check on startup/schedule and throttled logging on 401/403."""

    def setUp(self):
        ai_assistant.reset_auth_error_state()

    def tearDown(self):
        ai_assistant.reset_auth_error_state()

    async def test_ping_401_logs_single_error_without_traceback_and_no_key(self):
        secret_key = "sk-super-secret-deepseek-key-999"
        mock_response = MagicMock()
        mock_response.status = 401

        mock_session = MagicMock()
        mock_session.get.return_value = FakeResponseContextManager(mock_response)

        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": secret_key}), \
             patch("ai_assistant.DEEPSEEK_API_KEY", secret_key), \
             self.assertLogs("ai_assistant", level="ERROR") as cm:
            ok = await ai_assistant.check_api_key(session=mock_session)

        self.assertFalse(ok)
        self.assertEqual(len(cm.records), 1)
        record = cm.records[0]
        self.assertEqual(record.levelname, "ERROR")
        self.assertEqual(record.getMessage(), "ключ DeepSeek недействителен (401)")
        self.assertIsNone(record.exc_info)
        self.assertNotIn(secret_key, record.getMessage())
        for raw_line in cm.output:
            self.assertNotIn(secret_key, raw_line)

    async def test_ping_403_logs_single_error_without_traceback(self):
        secret_key = "sk-super-secret-deepseek-key-403"
        mock_response = MagicMock()
        mock_response.status = 403

        mock_session = MagicMock()
        mock_session.get.return_value = FakeResponseContextManager(mock_response)

        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": secret_key}), \
             patch("ai_assistant.DEEPSEEK_API_KEY", secret_key), \
             self.assertLogs("ai_assistant", level="ERROR") as cm:
            ok = await ai_assistant.check_api_key(session=mock_session)

        self.assertFalse(ok)
        self.assertEqual(len(cm.records), 1)
        record = cm.records[0]
        self.assertEqual(record.levelname, "ERROR")
        self.assertEqual(record.getMessage(), "ключ DeepSeek недействителен (403)")
        self.assertIsNone(record.exc_info)
        self.assertNotIn(secret_key, record.getMessage())

    async def test_ping_throttles_subsequent_calls_within_one_hour(self):
        secret_key = "sk-secret-throttle"
        mock_response = MagicMock()
        mock_response.status = 401

        mock_session = MagicMock()
        mock_session.get.return_value = FakeResponseContextManager(mock_response)

        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": secret_key}), \
             patch("ai_assistant.DEEPSEEK_API_KEY", secret_key), \
             self.assertLogs("ai_assistant", level="INFO") as cm:
            # 1st call: logs ERROR
            ok1 = await ai_assistant.check_api_key(session=mock_session)
            self.assertFalse(ok1)

            # 2nd call (same timestamp / within 1 hour): must NOT log anything
            ok2 = await ai_assistant.check_api_key(session=mock_session)
            self.assertFalse(ok2)

            # 3rd call 500s later: still within 1 hour, must NOT log anything
            with patch("time.monotonic", return_value=ai_assistant._last_auth_error_time + 500):
                ok3 = await ai_assistant.check_api_key(session=mock_session)
                self.assertFalse(ok3)

        # Only the first call produced a log record
        self.assertEqual(len(cm.records), 1)
        self.assertEqual(cm.records[0].levelname, "ERROR")

    async def test_ping_logs_warning_after_one_hour(self):
        secret_key = "sk-secret-warning"
        mock_response = MagicMock()
        mock_response.status = 401

        mock_session = MagicMock()
        mock_session.get.return_value = FakeResponseContextManager(mock_response)

        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": secret_key}), \
             patch("ai_assistant.DEEPSEEK_API_KEY", secret_key), \
             self.assertLogs("ai_assistant", level="INFO") as cm:
            base_time = 1000.0
            with patch("time.monotonic", return_value=base_time):
                await ai_assistant.check_api_key(session=mock_session)

            # Exactly 3601 seconds later: must log WARNING (no more often than once an hour)
            with patch("time.monotonic", return_value=base_time + 3601):
                await ai_assistant.check_api_key(session=mock_session)

            # 100 seconds after WARNING: should not log again
            with patch("time.monotonic", return_value=base_time + 3701):
                await ai_assistant.check_api_key(session=mock_session)

        self.assertEqual(len(cm.records), 2)
        self.assertEqual(cm.records[0].levelname, "ERROR")
        self.assertEqual(cm.records[0].getMessage(), "ключ DeepSeek недействителен (401)")
        self.assertEqual(cm.records[1].levelname, "WARNING")
        self.assertEqual(cm.records[1].getMessage(), "ключ DeepSeek недействителен (401)")
        self.assertIsNone(cm.records[1].exc_info)
        self.assertNotIn(secret_key, cm.records[1].getMessage())

    async def test_ping_success_resets_auth_error_state(self):
        secret_key = "sk-secret-recover"
        resp_401 = MagicMock(status=401)
        resp_200 = MagicMock(status=200)

        mock_session = MagicMock()
        mock_session.get.side_effect = [
            FakeResponseContextManager(resp_401),
            FakeResponseContextManager(resp_200),
            FakeResponseContextManager(resp_401),
        ]

        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": secret_key}), \
             patch("ai_assistant.DEEPSEEK_API_KEY", secret_key), \
             self.assertLogs("ai_assistant", level="INFO") as cm:
            ok1 = await ai_assistant.check_api_key(session=mock_session)
            self.assertFalse(ok1)

            ok2 = await ai_assistant.check_api_key(session=mock_session)
            self.assertTrue(ok2)

            # Since state was reset on 200, the next 401 should trigger ERROR again
            ok3 = await ai_assistant.check_api_key(session=mock_session)
            self.assertFalse(ok3)

        error_records = [r for r in cm.records if r.levelname == "ERROR"]
        self.assertEqual(len(error_records), 2)

    async def test_ask_401_throttled_and_no_key_or_traceback(self):
        secret_key = "sk-secret-ask-401"
        mock_response = MagicMock(status=401)
        mock_response.text = AsyncMock(return_value="Unauthorized body with sensitive tokens")

        mock_session = MagicMock()
        mock_session.post.return_value = FakeResponseContextManager(mock_response)

        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": secret_key}), \
             patch("ai_assistant.DEEPSEEK_API_KEY", secret_key), \
             self.assertLogs("ai_assistant", level="ERROR") as cm:
            res = await ai_assistant.ask("sys", "user", session=mock_session)

        self.assertIsNone(res)
        self.assertEqual(len(cm.records), 1)
        record = cm.records[0]
        self.assertEqual(record.levelname, "ERROR")
        self.assertEqual(record.getMessage(), "ключ DeepSeek недействителен (401)")
        self.assertIsNone(record.exc_info)
        self.assertNotIn(secret_key, record.getMessage())
        self.assertNotIn("sensitive tokens", record.getMessage())

    async def test_ping_disabled_when_no_api_key(self):
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": ""}), \
             patch("ai_assistant.DEEPSEEK_API_KEY", None):
            res = await ai_assistant.check_api_key()
        self.assertFalse(res)


if __name__ == "__main__":
    unittest.main()
