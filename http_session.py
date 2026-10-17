import asyncio
import logging

import aiohttp

logger = logging.getLogger(__name__)

_session: aiohttp.ClientSession | None = None
_session_loop: asyncio.AbstractEventLoop | None = None

# Allow awaiting `get_session()` or `session` directly without error
if not hasattr(aiohttp.ClientSession, "__await__"):
    def _client_session_await(self):
        async def _identity():
            return self
        return _identity().__await__()

    aiohttp.ClientSession.__await__ = _client_session_await


def get_session() -> aiohttp.ClientSession:
    """Return shared aiohttp.ClientSession instance for the current running event loop.

    Lazily creates the session on first call within a running loop, reuses it on subsequent calls,
    and recreates it if closed or if the event loop changed.
    """
    global _session, _session_loop
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError as exc:
        raise RuntimeError("get_session() must be called within a running event loop") from exc

    if _session is None or _session.closed or _session_loop is not loop:
        if _session is not None and not _session.closed:
            # Mark previous session from a different loop as closed so GC does not warn
            _session._connector = None
        _session = aiohttp.ClientSession()
        _session_loop = loop
    return _session


async def close_session() -> None:
    """Close the shared session if open and reset global state."""
    global _session, _session_loop
    if _session is not None and not _session.closed:
        await _session.close()
    _session = None
    _session_loop = None
