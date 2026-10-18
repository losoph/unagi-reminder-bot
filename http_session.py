import asyncio
import logging

import aiohttp

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = aiohttp.ClientTimeout(total=60)

_sessions: dict[asyncio.AbstractEventLoop, aiohttp.ClientSession] = {}


def get_session() -> aiohttp.ClientSession:
    """Return shared aiohttp.ClientSession instance for the current running event loop.

    Lazily creates the session on first call within a running loop, reuses it on subsequent calls,
    and recreates it if closed.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError as exc:
        raise RuntimeError("get_session() must be called within a running event loop") from exc

    # Clean up sessions from loops that are already closed
    for old_loop, old_sess in list(_sessions.items()):
        if old_loop is not loop and old_loop.is_closed():
            _sessions.pop(old_loop, None)
            if not old_sess.closed:
                loop.create_task(old_sess.close())

    session = _sessions.get(loop)
    if session is None or session.closed:
        session = aiohttp.ClientSession(timeout=DEFAULT_TIMEOUT)
        _sessions[loop] = session
    return session


async def close_session() -> None:
    """Close the shared session if open and reset state."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is not None:
        session = _sessions.pop(loop, None)
        if session is not None and not session.closed:
            await session.close()
    else:
        for sess in list(_sessions.values()):
            if not sess.closed:
                await sess.close()
        _sessions.clear()

    # Clean up any remaining sessions from closed loops
    for old_loop, old_sess in list(_sessions.items()):
        if old_loop.is_closed():
            _sessions.pop(old_loop, None)
            if not old_sess.closed:
                await old_sess.close()
