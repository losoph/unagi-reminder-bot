import json
import logging
import os

import aiohttp

from data.database import delete_app_meta, get_app_meta, set_app_meta
from post_text import (
    _SENTENCE_END_RE,
    _clean_post_for_digest,
    _clean_post_text,
    _extract_bare_url_domain,
    _extract_title_and_essence,
    _is_banner_line,
    _normalize_caps_title,
    _split_first_sentence,
    _strip_banner_line,
    _strip_forwarding_lines,
    _truncate_by_word,
    clean_post_for_render,
)

__all__ = [
    "_MAX_CONTENT_BYTES",
    "_SENTENCE_END_RE",
    "_clean_post_for_digest",
    "_clean_post_text",
    "_extract_bare_url_domain",
    "_extract_title_and_essence",
    "_is_banner_line",
    "_is_post_node",
    "_normalize_caps_title",
    "_split_first_sentence",
    "_strip_banner_line",
    "_strip_forwarding_lines",
    "_truncate_by_word",
    "_truncate_to_limit",
    "build_content",
    "clean_post_for_render",
    "describe_counts",
    "plural_ru",
    "publish_digest",
    "publish_page",
]

logger = logging.getLogger(__name__)

TELEGRAPH_API = "https://api.telegra.ph"
_TOKEN_META_KEY = "telegraph_access_token"
_ENV_TOKEN = os.getenv("TELEGRAPH_TOKEN")
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=20)
# Telegraph hard-limits a page to ~64 KB of content nodes.
_MAX_CONTENT_BYTES = 60_000
# Maximum characters for a single post on a Telegraph page.
TELEGRAPH_POST_CHARS = int(os.getenv("TELEGRAPH_POST_CHARS", "1500"))
# Average adult silent-reading speed, words/minute — used only for the digest's
# rough "~N мин" estimate at the top.
_READING_WPM = 200


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
    if _ENV_TOKEN:
        return _ENV_TOKEN
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
        {
            "tag": "li",
            "children": [
                f"{section.get('title') or 'Канал'} — {plural_ru(len(section.get('posts', [])), 'пост', 'поста', 'постов')}"
            ],
        }
        for section in sections
    ]
    return [
        {"tag": "p", "children": [{"tag": "b", "children": [summary_line]}]},
        {"tag": "ul", "children": items},
        {"tag": "hr"},
    ]




def _build_card_node(title: str, essence: str, link: str | None = None) -> dict:
    children: list = [{"tag": "b", "children": [title]}]
    if link:
        children.append("  ")
        children.append({"tag": "a", "attrs": {"href": str(link)}, "children": ["Читать"]})
    if essence:
        children.append({"tag": "br"})
        children.append(essence)
    return {"tag": "p", "children": children}


def _build_post_node(post: dict, max_chars: int | None = None) -> dict:
    if max_chars is None:
        max_chars = TELEGRAPH_POST_CHARS
    text = (post.get("text") or "").strip()
    if max_chars and len(text) > max_chars:
        text = text[: max_chars - 1] + "…"
    clean = _clean_post_for_digest(text)
    title, essence = _extract_title_and_essence(clean, max_title=140, max_essence=300)
    if not title:
        title = "Без названия"
    return _build_card_node(title, essence, post.get("link"))


def _select_channel_posts(posts: list[dict]) -> tuple[list[dict], list[dict]]:
    """Partition channel posts into cards (up to 3 most substantial) and short items.

    Selection is deterministic: length of essence, presence of link, clean text length.
    Preserves original relative order within both cards and short items.
    """
    if not posts:
        return [], []

    parsed = []
    for idx, post in enumerate(posts):
        text = (post.get("text") or "").strip()
        if TELEGRAPH_POST_CHARS and len(text) > TELEGRAPH_POST_CHARS:
            text = text[: TELEGRAPH_POST_CHARS - 1] + "…"
        clean = _clean_post_for_digest(text)
        title, essence = _extract_title_and_essence(clean, max_title=140, max_essence=300)
        if not title:
            title = "Без названия"
        has_link = 1 if bool(post.get("link")) else 0
        parsed.append({
            "post": post,
            "title": title,
            "essence": essence,
            "clean": clean,
            "orig_idx": idx,
            "score": (len(essence), has_link, len(clean), -idx),
        })

    if len(parsed) <= 3:
        return parsed, []

    scored = sorted(parsed, key=lambda it: it["score"], reverse=True)
    card_indices = {it["orig_idx"] for it in scored[:3]}

    cards = [it for it in parsed if it["orig_idx"] in card_indices]
    shorts = [it for it in parsed if it["orig_idx"] not in card_indices]
    return cards, shorts


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
        cards, shorts = _select_channel_posts(section.get("posts", []))
        for card in cards:
            nodes.append(_build_card_node(card["title"], card["essence"], card["post"].get("link")))
        if shorts:
            nodes.append({"tag": "p", "children": [{"tag": "b", "children": ["Коротко"]}]})
            short_li = []
            for item in shorts:
                clean = item["clean"]
                domain = _extract_bare_url_domain(clean) or _extract_bare_url_domain(item["title"])
                if domain:
                    short_title = f"🔗 ссылка · {domain}"
                else:
                    clean_len = len(" ".join(clean.split()))
                    if clean_len < 80:
                        short_title = _normalize_caps_title(" ".join(clean.split()))
                    else:
                        short_title = item["title"]
                    if len(short_title) > 140:
                        short_title = _truncate_by_word(short_title or "Без названия", 140)
                link = item["post"].get("link")
                if link:
                    li_children = [{"tag": "a", "attrs": {"href": str(link)}, "children": [short_title]}]
                else:
                    li_children = [short_title]
                short_li.append({"tag": "li", "children": li_children})
            nodes.append({"tag": "ul", "children": short_li})
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


def _is_short_label(node: dict) -> bool:
    if isinstance(node, dict) and node.get("tag") == "p":
        children = node.get("children", [])
        if len(children) == 1 and isinstance(children[0], dict) and children[0].get("tag") == "b":
            return children[0].get("children") in (["Коротко"], ["Коротко:"])
    return False


def _is_post_node(node: dict) -> bool:
    if not isinstance(node, dict):
        return False
    tag = node.get("tag")
    if tag == "li":
        return True
    if tag != "p":
        return False
    if _is_short_label(node):
        return False
    children = node.get("children", [])
    for child in children:
        if child == "🔹 ":
            return True
        if isinstance(child, dict):
            if child.get("tag") == "a" and child.get("children") == ["Читать"]:
                return True
            if child.get("tag") == "b":
                b_text = "".join(str(c) for c in child.get("children", []))
                if "⚠️ Выпуск усечён" in b_text or "мин чтения" in b_text:
                    return False
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

    # Keep track of table-of-contents ul (if present) so it is not truncated as a post list
    toc_ul = content[1] if len(content) > 1 and content[1].get("tag") == "ul" else None

    omitted_posts = 0
    while content:
        notice = _build_truncation_notice(omitted_posts + 1)
        if _content_bytes(content + [notice]) <= max_bytes:
            break

        if content[-1].get("tag") == "ul" and content[-1] is not toc_ul:
            ul_children = content[-1].get("children")
            if ul_children:
                ul_children.pop()
                omitted_posts += 1
            if not ul_children:
                content.pop()
            continue

        node = content.pop()
        if _is_post_node(node):
            omitted_posts += 1

    # Remove trailing empty group/channel headers, hr, or short labels
    while content and (content[-1].get("tag") in ("h3", "h4", "hr") or _is_short_label(content[-1])):
        content.pop()

    if omitted_posts > 0:
        notice = _build_truncation_notice(omitted_posts)
        while content and _content_bytes(content + [notice]) > max_bytes:
            if content[-1].get("tag") == "ul" and content[-1] is not toc_ul:
                ul_children = content[-1].get("children")
                if ul_children:
                    ul_children.pop()
                    omitted_posts += 1
                    notice = _build_truncation_notice(omitted_posts)
                if not ul_children:
                    content.pop()
                continue
            node = content.pop()
            if _is_post_node(node):
                omitted_posts += 1
                notice = _build_truncation_notice(omitted_posts)
        while content and (content[-1].get("tag") in ("h3", "h4", "hr") or _is_short_label(content[-1])):
            content.pop()
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
