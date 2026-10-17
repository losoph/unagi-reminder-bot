import asyncio
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp

import ai_assistant
import http_session
import scraper


class CS1HttpSessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        await http_session.close_session()

    async def test_session_reused_between_calls(self):
        """Shared session is lazily created and reused within the same event loop."""
        session1 = http_session.get_session()
        self.assertIsInstance(session1, aiohttp.ClientSession)
        self.assertFalse(session1.closed)

        session2 = http_session.get_session()
        self.assertIs(session1, session2)
        self.assertFalse(session2.closed)

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

    def test_get_session_outside_event_loop_raises_runtime_error(self):
        """get_session() must raise RuntimeError if called without a running loop."""
        # Ensure no event loop is running on this thread
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop is None:
            with self.assertRaises(RuntimeError) as ctx:
                http_session.get_session()
            self.assertIn("running event loop", str(ctx.exception))


class CS1EventLoopIsolationTests(unittest.IsolatedAsyncioTestCase):
    """Verify session is not bound to a dead event loop between IsolatedAsyncioTestCase runs."""

    _first_test_session: aiohttp.ClientSession | None = None
    _first_test_loop: asyncio.AbstractEventLoop | None = None

    async def test_step_1_create_session_in_first_loop(self):
        session = http_session.get_session()
        self.assertFalse(session.closed)
        self.__class__._first_test_session = session
        self.__class__._first_test_loop = asyncio.get_running_loop()
        # Deliberately do not close_session here to test cross-test loop isolation

    async def test_step_2_recreates_session_in_new_loop(self):
        current_loop = asyncio.get_running_loop()
        self.assertIsNot(current_loop, self.__class__._first_test_loop)

        # get_session in a new event loop must detect loop change and create a fresh session
        session2 = http_session.get_session()
        self.assertIsNot(session2, self.__class__._first_test_session)
        self.assertFalse(session2.closed)
        await http_session.close_session()


class FakeAsyncContextManager:
    def __init__(self, target):
        self.target = target

    async def __aenter__(self):
        return self.target

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass


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

        mock_session = MagicMock()
        mock_session.post.return_value = FakeAsyncContextManager(mock_response)
        mock_session.close = AsyncMock()

        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}), \
             patch("ai_assistant.DEEPSEEK_API_KEY", "test-key"):
            answer = await ai_assistant.ask("sys", "user", session=mock_session)

        self.assertEqual(answer, "DeepSeek answer")
        mock_session.post.assert_called_once()
        call_args, call_kwargs = mock_session.post.call_args
        self.assertIn("chat/completions", call_args[0])
        self.assertEqual(call_kwargs["headers"]["Authorization"], "Bearer test-key")
        self.assertEqual(call_kwargs["timeout"], ai_assistant.REQUEST_TIMEOUT)
        # Passed session must NOT be closed by the caller
        mock_session.close.assert_not_called()

    async def test_ai_assistant_uses_shared_session_when_none_passed(self):
        """ai_assistant.ask uses shared session when session argument is omitted/None and does not close it."""
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
        # The shared session must remain open!
        self.assertFalse(shared_session.closed)

    async def test_scraper_uses_passed_session(self):
        """scraper.get_latest_posts prioritizes caller-provided session over shared session."""
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

        mock_session = MagicMock()
        mock_session.get.return_value = FakeAsyncContextManager(mock_response)
        mock_session.close = AsyncMock()

        posts = await scraper.get_latest_posts(
            "@testchan",
            "2026-10-17 00:00:00",
            session=mock_session,
            max_pages=1,
            page_delay=0,
        )

        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["id"], 100)
        self.assertEqual(posts[0]["text"], "Hello post 100")
        mock_session.get.assert_called_once()
        call_args, call_kwargs = mock_session.get.call_args
        self.assertIn("https://t.me/s/testchan", call_args[0])
        self.assertEqual(call_kwargs["headers"], scraper.REQUEST_HEADERS)
        self.assertEqual(call_kwargs["timeout"], scraper.REQUEST_TIMEOUT)
        # Passed session must NOT be closed
        mock_session.close.assert_not_called()

    async def test_scraper_uses_shared_session_when_none_passed(self):
        """scraper.get_latest_posts uses shared session when session argument is omitted/None and does not close it."""
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
        # The shared session must remain open!
        self.assertFalse(shared_session.closed)
