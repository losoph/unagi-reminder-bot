import asyncio
import inspect
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp

import ai_assistant
import http_session
import scraper


class CS1OutsideEventLoopTests(unittest.TestCase):
    def test_get_session_outside_event_loop_raises_runtime_error(self):
        """get_session() must unconditionally raise RuntimeError if called without a running loop."""
        with self.assertRaises(RuntimeError) as ctx:
            http_session.get_session()
        self.assertIn("running event loop", str(ctx.exception))

    def test_client_session_not_monkeypatched_awaitable(self):
        """aiohttp.ClientSession must not have a global __await__ monkeypatch."""
        self.assertFalse(hasattr(aiohttp.ClientSession, "__await__"))


class CS1SharedHttpSessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        await http_session.close_session()

    async def test_session_reused_between_calls(self):
        """Shared session is lazily created and reused within the same event loop."""
        session1 = http_session.get_session()
        self.assertIsInstance(session1, aiohttp.ClientSession)
        self.assertFalse(session1.closed)
        self.assertFalse(inspect.isawaitable(session1))

        session2 = http_session.get_session()
        self.assertIs(session1, session2)
        self.assertFalse(session2.closed)

    async def test_session_has_default_timeout(self):
        """Shared session has a reasonable default timeout (60s total, not default 300s)."""
        session = http_session.get_session()
        self.assertEqual(session.timeout.total, 60)

    async def test_session_recreated_after_close(self):
        """Closing the session resets state; next call creates a new active session."""
        session1 = http_session.get_session()
        self.assertFalse(session1.closed)

        await http_session.close_session()
        self.assertTrue(session1.closed)

        session2 = http_session.get_session()
        self.assertIsNot(session1, session2)
        self.assertFalse(session2.closed)

    async def test_close_session_idempotent(self):
        """Calling close_session multiple times or when no session exists is safe."""
        await http_session.close_session()
        await http_session.close_session()

        session = http_session.get_session()
        await http_session.close_session()
        self.assertTrue(session.closed)
        await http_session.close_session()


class CS1EventLoopIsolationTests(unittest.TestCase):
    """Verify session lifecycle and connector cleanup across separate event loops."""

    def test_sessions_isolated_and_closed_between_event_loops(self):
        """Separate event loops obtain distinct sessions and close connectors cleanly."""
        session1 = None
        conn1 = None
        session2 = None
        conn2 = None

        async def run_loop_1():
            nonlocal session1, conn1
            session1 = http_session.get_session()
            conn1 = session1.connector
            self.assertFalse(session1.closed)
            self.assertFalse(conn1.closed)
            await http_session.close_session()
            self.assertTrue(session1.closed)
            self.assertTrue(conn1.closed)

        async def run_loop_2():
            nonlocal session2, conn2
            session2 = http_session.get_session()
            conn2 = session2.connector
            self.assertFalse(session2.closed)
            self.assertFalse(conn2.closed)
            await http_session.close_session()
            self.assertTrue(session2.closed)
            self.assertTrue(conn2.closed)

        loop1 = asyncio.new_event_loop()
        try:
            loop1.run_until_complete(run_loop_1())
        finally:
            loop1.close()

        loop2 = asyncio.new_event_loop()
        try:
            loop2.run_until_complete(run_loop_2())
        finally:
            loop2.close()

        self.assertIsNot(session1, session2)
        self.assertTrue(session1.closed)
        self.assertTrue(session2.closed)
        self.assertTrue(conn1.closed)
        self.assertTrue(conn2.closed)

    def test_unclosed_session_connector_closed_on_loop_switch(self):
        """If a session was left unclosed in a closed loop, get_session in a new loop cleans up the connector."""
        session1 = None
        conn1 = None
        session2 = None

        async def run_loop_1():
            nonlocal session1, conn1
            session1 = http_session.get_session()
            conn1 = session1.connector
            self.assertFalse(session1.closed)
            self.assertFalse(conn1.closed)

        async def run_loop_2():
            nonlocal session2
            session2 = http_session.get_session()
            # Allow background cleanup task to run
            await asyncio.sleep(0.01)
            await http_session.close_session()

        loop1 = asyncio.new_event_loop()
        try:
            loop1.run_until_complete(run_loop_1())
        finally:
            loop1.close()

        loop2 = asyncio.new_event_loop()
        try:
            loop2.run_until_complete(run_loop_2())
        finally:
            loop2.close()

        self.assertIsNot(session1, session2)
        self.assertTrue(conn1.closed)
        self.assertTrue(session1.closed)
        self.assertTrue(session2.closed)


class FakeAsyncContextManager:
    def __init__(self, target):
        self.target = target

    async def __aenter__(self):
        return self.target

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass


class StrictTimeoutStubSession:
    """Strict test session that enforces explicit timeout kwarg on HTTP calls."""

    def __init__(self, response):
        self.response = response
        self.get_calls = []
        self.post_calls = []
        self.closed = False

    def post(self, url, *, json=None, headers=None, timeout=None):
        if timeout is None:
            raise TypeError("timeout is strictly required on post()")
        self.post_calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        return FakeAsyncContextManager(self.response)

    def get(self, url, *, headers=None, timeout=None):
        if timeout is None:
            raise TypeError("timeout is strictly required on get()")
        self.get_calls.append({"url": url, "headers": headers, "timeout": timeout})
        return FakeAsyncContextManager(self.response)

    async def close(self):
        self.closed = True


class CS1CallersSessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        await http_session.close_session()

    async def test_ai_assistant_uses_passed_session(self):
        """ai_assistant.ask prioritizes caller-provided session over shared session."""
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={
            "choices": [{"message": {"content": "DeepSeek answer"}}]
        })

        stub_session = StrictTimeoutStubSession(mock_response)

        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}), \
             patch("ai_assistant.DEEPSEEK_API_KEY", "test-key"):
            answer = await ai_assistant.ask("sys", "user", session=stub_session)

        self.assertEqual(answer, "DeepSeek answer")
        self.assertEqual(len(stub_session.post_calls), 1)
        call = stub_session.post_calls[0]
        self.assertIn("chat/completions", call["url"])
        self.assertEqual(call["headers"]["Authorization"], "Bearer test-key")
        self.assertEqual(call["timeout"], ai_assistant.REQUEST_TIMEOUT)
        # Passed session must NOT be closed by the caller
        self.assertFalse(stub_session.closed)

    async def test_ai_assistant_uses_shared_session_when_none_passed(self):
        """ai_assistant.ask uses shared session when session argument is omitted/None and preserves timeout."""
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={
            "choices": [{"message": {"content": "Shared answer"}}]
        })

        shared_session = http_session.get_session()
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}), \
             patch("ai_assistant.DEEPSEEK_API_KEY", "test-key"), \
             patch.object(shared_session, "post", return_value=FakeAsyncContextManager(mock_response)) as mock_post:
            answer = await ai_assistant.ask("sys", "user")

        self.assertEqual(answer, "Shared answer")
        mock_post.assert_called_once()
        _, call_kwargs = mock_post.call_args
        self.assertEqual(call_kwargs.get("timeout"), ai_assistant.REQUEST_TIMEOUT)
        # The shared session must remain open!
        self.assertFalse(shared_session.closed)

    async def test_scraper_uses_passed_session(self):
        """scraper.get_latest_posts prioritizes caller-provided session over shared session and preserves timeout."""
        html_fixture = """
        <div class="tgme_widget_message">
            <a class="tgme_widget_message_date" href="https://t.me/testchan/100">date</a>
            <time class="time" datetime="2026-10-17T12:00:00+00:00"></time>
            <div class="tgme_widget_message_text">Hello post 100</div>
        </div>
        """
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.text = AsyncMock(return_value=html_fixture)

        stub_session = StrictTimeoutStubSession(mock_response)

        posts = await scraper.get_latest_posts(
            "@testchan",
            "2026-10-17 00:00:00",
            session=stub_session,
            max_pages=1,
            page_delay=0,
        )

        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["id"], 100)
        self.assertEqual(posts[0]["text"], "Hello post 100")
        self.assertEqual(len(stub_session.get_calls), 1)
        call = stub_session.get_calls[0]
        self.assertIn("https://t.me/s/testchan", call["url"])
        self.assertEqual(call["headers"], scraper.REQUEST_HEADERS)
        self.assertEqual(call["timeout"], scraper.REQUEST_TIMEOUT)
        # Passed session must NOT be closed
        self.assertFalse(stub_session.closed)

    async def test_scraper_uses_shared_session_when_none_passed(self):
        """scraper.get_latest_posts uses shared session when session argument is omitted/None and preserves timeout."""
        html_fixture = """
        <div class="tgme_widget_message">
            <a class="tgme_widget_message_date" href="https://t.me/testchan/101">date</a>
            <time class="time" datetime="2026-10-17T13:00:00+00:00"></time>
            <div class="tgme_widget_message_text">Hello post 101</div>
        </div>
        """
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.text = AsyncMock(return_value=html_fixture)

        shared_session = http_session.get_session()
        with patch.object(shared_session, "get", return_value=FakeAsyncContextManager(mock_response)) as mock_get:
            posts = await scraper.get_latest_posts(
                "@testchan",
                "2026-10-17 00:00:00",
                max_pages=1,
                page_delay=0,
            )

        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["id"], 101)
        mock_get.assert_called_once()
        _, call_kwargs = mock_get.call_args
        self.assertEqual(call_kwargs.get("timeout"), scraper.REQUEST_TIMEOUT)
        # The shared session must remain open!
        self.assertFalse(shared_session.closed)
