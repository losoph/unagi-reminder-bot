import json
import logging
import re

import aiohttp

from config import settings
from data.database import delete_app_meta, get_app_meta, set_app_meta

logger = logging.getLogger(__name__)

TELEGRAPH_API = "https://api.telegra.ph"
_TOKEN_META_KEY = "telegraph_access_token"
_ENV_TOKEN = settings().telegraph.token
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=20)
# Telegraph hard-limits a page to ~64 KB of content nodes.
_MAX_CONTENT_BYTES = 60_000
# Maximum characters for a single post on a Telegraph page.
TELEGRAPH_POST_CHARS = settings().telegraph.post_chars
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


async def _create_token(session: aiohttp.ClientSession) -> str:
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


async def _get_token(session: aiohttp.ClientSession) -> str:
    global _ENV_TOKEN
    token = _ENV_TOKEN or settings().telegraph.token
    if token:
        return token
    token = get_app_meta(_TOKEN_META_KEY)
    if token:
        return token
    return await _create_token(session)


def plural_ru(count: int, one: str, few: str, many: str) -> str:
    """plural_ru(2, "канал", "канала", "каналов") -> "2 канала"."""
    tail = count % 100
    if 11 <= tail <= 14:
        form = many
    else:
        form = {1: one, 2: few, 3: few, 4: few}.get(count % 10, many)
    return f"{count} {form}"


def describe_counts(channels: int, posts: int) -> str:
    return f"{plural_ru(channels, 'канал', 'канала', 'каналов')} · {plural_ru(posts, 'пост', 'поста', 'постов')}"


def _build_summary(sections: list[dict]) -> list:
    """A short 'what's inside' block at the top: channels, per-channel post counts, read time."""
    total_posts = sum(len(section.get("posts", [])) for section in sections)
    total_words = sum(
        len((post.get("text") or "").split())
        for section in sections
        for post in section.get("posts", [])
    )
    minutes = max(1, round(total_words / _READING_WPM))
    summary_line = f"{describe_counts(len(sections), total_posts)} · ~{minutes} мин чтения"
    items = [
        {"tag": "li", "children": [f"{section.get('title') or 'Канал'} — {len(section.get('posts', []))}"]}
        for section in sections
    ]
    return [
        {"tag": "p", "children": [{"tag": "b", "children": [summary_line]}]},
        {"tag": "ul", "children": items},
        {"tag": "hr"},
    ]


def _build_post_node(post: dict, max_chars: int | None = None) -> dict:
    if max_chars is None:
        max_chars = TELEGRAPH_POST_CHARS
    text = (post.get("text") or "").strip()
    if max_chars and len(text) > max_chars:
        text = text[: max_chars - 3] + "..."
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


def _build_actions_node(actions: dict) -> dict | None:
    """«🚫 Отписаться · ↪️ Перенести: в ежедневный · в ежемесячный» under a channel."""
    children: list = []
    unsubscribe_url = actions.get("unsubscribe")
    if unsubscribe_url:
        children.append({"tag": "a", "attrs": {"href": unsubscribe_url}, "children": ["🚫 Отписаться"]})
    moves = [(label, url) for label, url in actions.get("move") or [] if url]
    if moves:
        if children:
            children.append(" · ")
        children.append("↪️ Перенести: ")
        for index, (label, url) in enumerate(moves):
            if index:
                children.append(" · ")
            children.append({"tag": "a", "attrs": {"href": url}, "children": [label]})
    return {"tag": "p", "children": children} if children else None


def build_content(sections: list[dict], *, with_actions: bool = False) -> list:
    """Convert digest sections into Telegraph Node objects (plain text, no HTML).

    ``with_actions`` adds per-channel «Отписаться / Перенести» links from
    ``section["actions"]``. Only for private delivery (email): a Telegraph page is
    public and must not carry subscription controls.
    """
    nodes: list = _build_summary(sections)
    current_group = None
    for section in sections:
        group = section.get("group")
        if group and group != current_group:
            nodes.append({"tag": "h3", "children": [str(group)]})
        current_group = group
        title = section.get("title") or "Канал"
        nodes.append({"tag": "h4", "children": [str(title)]})
        for post in section.get("posts", []):
            nodes.append(_build_post_node(post))
        omitted_count = section.get("omitted_count", 0)
        ch_user = (section.get("username") or section.get("channel_username") or "").lstrip("@")
        if omitted_count > 0 and ch_user:
            post_str = plural_ru(omitted_count, "пост", "поста", "постов")
            ch_url = f"https://t.me/{ch_user}"
            nodes.append({
                "tag": "p",
                "children": [
                    {"tag": "a", "attrs": {"href": ch_url}, "children": [f"▫️ ещё {post_str} в канале"]}
                ],
            })
        if with_actions and section.get("actions"):
            actions_node = _build_actions_node(section["actions"])
            if actions_node:
                nodes.append(actions_node)
        nodes.append({"tag": "hr"})
    return nodes


def _is_post_node(node: dict) -> bool:
    if not isinstance(node, dict) or node.get("tag") != "p":
        return False
    children = node.get("children", [])
    for child in children:
        if child == "🔹 ":
            return True
        if isinstance(child, dict) and child.get("tag") == "a" and child.get("children") == ["Читать"]:
            return True
    return False


def _build_truncation_notice(omitted_count: int) -> dict:
    post_str = plural_ru(omitted_count, "пост не вошёл", "поста не вошли", "постов не вошли")
    return {
        "tag": "p",
        "children": [
            {
                "tag": "b",
                "children": [f"⚠️ Выпуск усечён: ещё {omitted_count} {post_str} из-за лимита размера страницы."],
            }
        ],
    }


def _content_bytes(content: list) -> int:
    return len(json.dumps(content, ensure_ascii=False).encode("utf-8"))


def _truncate_to_limit(content: list, max_bytes: int = _MAX_CONTENT_BYTES) -> list:
    """Drop trailing nodes until the JSON payload fits Telegraph's size limit,

    adding an explicit notice and warning if posts had to be omitted.
    """
    if _content_bytes(content) <= max_bytes:
        return content

    omitted_posts = 0
    while content:
        notice = _build_truncation_notice(omitted_posts + 1)
        if _content_bytes(content + [notice]) <= max_bytes:
            break
        node = content.pop()
        if _is_post_node(node):
            omitted_posts += 1

    # Remove trailing empty group/channel headers or hr
    while content and content[-1].get("tag") in ("h3", "h4", "hr"):
        content.pop()

    if omitted_posts > 0:
        notice = _build_truncation_notice(omitted_posts)
        while content and _content_bytes(content + [notice]) > max_bytes:
            node = content.pop()
            if _is_post_node(node):
                omitted_posts += 1
                notice = _build_truncation_notice(omitted_posts)
        content.append(notice)
        logger.warning(
            "Telegraph content truncated to %d bytes: %d posts omitted",
            max_bytes,
            omitted_posts,
        )
    elif not content:
        content = [{"tag": "p", "children": ["Дайджест слишком большой для предпросмотра."]}]

    return content


async def publish_page(
    title: str,
    nodes: list,
    *,
    session: aiohttp.ClientSession | None = None,
) -> str | None:
    """Опубликовать готовые Telegraph-узлы и вернуть URL страницы."""
    global _ENV_TOKEN
    own_session = session is None
    if own_session:
        session = aiohttp.ClientSession(timeout=REQUEST_TIMEOUT)
    try:
        token = await _get_token(session)
        content = _truncate_to_limit(nodes)
        payload = {
            "access_token": token,
            "title": (title or "Дайджест")[:256],
            "author_name": "Unagi",
            "content": json.dumps(content, ensure_ascii=False),
        }
        async with session.post(f"{TELEGRAPH_API}/createPage", data=payload) as response:
            data = await response.json()
        if not data.get("ok"):
            error_msg = str(data.get("error", ""))
            if "ACCESS_TOKEN_INVALID" in error_msg.upper():
                logger.warning(
                    "Telegraph access token is invalid (%s); deleting from app_meta, creating new token and retrying",
                    error_msg,
                )
                delete_app_meta(_TOKEN_META_KEY)
                _ENV_TOKEN = None
                new_token = await _create_token(session)
                retry_payload = {**payload, "access_token": new_token}
                async with session.post(f"{TELEGRAPH_API}/createPage", data=retry_payload) as retry_response:
                    retry_data = await retry_response.json()
                if not retry_data.get("ok"):
                    logger.error("Telegraph createPage retry failed: %s", retry_data)
                    return None
                return retry_data["result"]["url"]

            logger.error("Telegraph createPage failed: %s", data)
            return None
        return data["result"]["url"]
    except Exception:
        logger.exception("Telegraph publish failed")
        return None
    finally:
        if own_session and session is not None:
            await session.close()


async def publish_digest(
    title: str,
    sections: list[dict],
    *,
    session: aiohttp.ClientSession | None = None,
) -> str | None:
    """Опубликовать дайджест, сохраняя прежние построение контента и поведение."""
    return await publish_page(title, build_content(sections), session=session)
