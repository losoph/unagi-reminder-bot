import json
import logging
import os
import re

import aiohttp

from data.database import get_app_meta, set_app_meta

logger = logging.getLogger(__name__)

TELEGRAPH_API = "https://api.telegra.ph"
_TOKEN_META_KEY = "telegraph_access_token"
_ENV_TOKEN = os.getenv("TELEGRAPH_TOKEN")
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=20)
# Telegraph hard-limits a page to ~64 KB of content nodes.
_MAX_CONTENT_BYTES = 60_000
# Average adult silent-reading speed, words/minute — used only for the digest's
# rough "~N мин" estimate at the top.
_READING_WPM = 200
# End of a sentence: punctuation followed by whitespace/end (not mid-abbreviation
# or decimal, e.g. "т.д." or "1.5" don't match since nothing follows but a digit/letter).
_SENTENCE_END_RE = re.compile(r"[.!?…]+(?=\s|$)")


def _split_first_sentence(text: str) -> tuple[str, str]:
    """Split off the first sentence (up to '.'/'!'/'?'/'…' or a line break)."""
    if not text:
        return "", ""
    newline_idx = text.find("\n")
    match = _SENTENCE_END_RE.search(text)
    end = match.end() if match else None
    if newline_idx != -1 and (end is None or newline_idx < end):
        end = newline_idx
    if end is None:
        return text, ""
    return text[:end].strip(), text[end:].strip()


async def _get_token(session: aiohttp.ClientSession) -> str:
    if _ENV_TOKEN:
        return _ENV_TOKEN
    token = get_app_meta(_TOKEN_META_KEY)
    if token:
        return token
    async with session.post(
        f"{TELEGRAPH_API}/createAccount",
        data={"short_name": "UnagiDigest", "author_name": "Unagi"},
    ) as response:
        data = await response.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegraph createAccount failed: {data}")
    token = data["result"]["access_token"]
    set_app_meta(_TOKEN_META_KEY, token)
    return token


def _build_summary(sections: list[dict]) -> list:
    """A short 'what's inside' block at the top: channels, per-channel post counts, read time."""
    total_posts = sum(len(section.get("posts", [])) for section in sections)
    total_words = sum(
        len((post.get("text") or "").split())
        for section in sections
        for post in section.get("posts", [])
    )
    minutes = max(1, round(total_words / _READING_WPM))
    summary_line = f"{len(sections)} каналов · {total_posts} постов · ~{minutes} мин чтения"
    items = [
        {"tag": "li", "children": [f"{section.get('title') or 'Канал'} — {len(section.get('posts', []))}"]}
        for section in sections
    ]
    return [
        {"tag": "p", "children": [{"tag": "b", "children": [summary_line]}]},
        {"tag": "ul", "children": items},
        {"tag": "hr"},
    ]


def _build_post_node(post: dict) -> dict:
    text = (post.get("text") or "").strip()
    children: list = []
    if text:
        first, rest = _split_first_sentence(text)
        children.append("🔹 ")
        children.append({"tag": "b", "children": [first]})
        if rest:
            children.append(" " + rest)
    link = post.get("link")
    if link:
        if children:
            children.append("  ")
        children.append({"tag": "a", "attrs": {"href": link}, "children": ["Читать"]})
    return {"tag": "p", "children": children or [" "]}


def _build_content(sections: list[dict]) -> list:
    """Convert digest sections into Telegraph Node objects (plain text, no HTML)."""
    nodes: list = _build_summary(sections)
    for section in sections:
        title = section.get("title") or "Канал"
        nodes.append({"tag": "h4", "children": [str(title)]})
        for post in section.get("posts", []):
            nodes.append(_build_post_node(post))
        nodes.append({"tag": "hr"})
    return nodes


def _truncate_to_limit(content: list) -> list:
    """Drop trailing nodes until the JSON payload fits Telegraph's size limit."""
    while content and len(json.dumps(content, ensure_ascii=False).encode("utf-8")) > _MAX_CONTENT_BYTES:
        content.pop()
    if not content:
        content = [{"tag": "p", "children": ["Дайджест слишком большой для предпросмотра."]}]
    return content


async def publish_digest(
    title: str,
    sections: list[dict],
    *,
    session: aiohttp.ClientSession | None = None,
) -> str | None:
    """Publish a digest to telegra.ph and return the page URL (or None on failure)."""
    own_session = session is None
    if own_session:
        session = aiohttp.ClientSession(timeout=REQUEST_TIMEOUT)
    try:
        token = await _get_token(session)
        content = _truncate_to_limit(_build_content(sections))
        payload = {
            "access_token": token,
            "title": (title or "Дайджест")[:256],
            "author_name": "Unagi",
            "content": json.dumps(content, ensure_ascii=False),
        }
        async with session.post(f"{TELEGRAPH_API}/createPage", data=payload) as response:
            data = await response.json()
        if not data.get("ok"):
            logger.error("Telegraph createPage failed: %s", data)
            return None
        return data["result"]["url"]
    except Exception:
        logger.exception("Telegraph publish failed")
        return None
    finally:
        if own_session and session is not None:
            await session.close()
