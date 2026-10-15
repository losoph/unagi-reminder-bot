"""Чистый рендер Ленты в узлы Telegraph."""

from __future__ import annotations

import os
from collections import defaultdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo


FEED_POST_CHARS = max(1, int(os.getenv("FEED_POST_CHARS", "600")))


def compact_number(value) -> str:
    """Форматирует метрику кратко, не добавляя суффикс для целых ниже тысячи."""
    if value is None:
        return "—"
    number = max(0, float(value))
    if number < 1000:
        return str(int(number)) if number.is_integer() else str(round(number, 1)).replace(".", ",")
    for divisor, suffix in ((1_000_000, "M"), (1_000, "K")):
        if number >= divisor:
            amount = number / divisor
            rendered = f"{amount:.1f}".rstrip("0").rstrip(".").replace(".", ",")
            return f"{rendered}{suffix}"
    return str(int(number))


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Дата поста должна иметь часовой пояс")
    return value.astimezone(timezone.utc)


def _zone(tz):
    if tz is None:
        return timezone.utc
    return ZoneInfo(tz) if isinstance(tz, str) else tz


def _plural_ru(number: int, one: str, few: str, many: str) -> str:
    n = abs(number) % 100
    if 11 <= n <= 14:
        return many
    last = n % 10
    return one if last == 1 else few if 2 <= last <= 4 else many


def _plural_channel(number: int) -> str:
    return _plural_ru(number, "канала", "каналов", "каналов")


def _truncate_text(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _first_sentence(text: str) -> tuple[str, str]:
    for index, char in enumerate(text):
        if char in ".!?…" and (index + 1 == len(text) or text[index + 1].isspace()):
            return text[: index + 1].strip(), text[index + 1 :].strip()
    return text, ""


def _post_nodes(item, rank: int, post_chars: int, tz=timezone.utc) -> list[dict]:
    post = item.post
    title = str(item.channel_title or post.get("channel_username") or "Канал")
    nodes = [{"tag": "h4", "children": [f"{rank}. {title}"]}]
    time_value = _aware_utc(post["time"]).astimezone(tz)
    parts = [f"👁 {compact_number(post.get('views'))}", f"↗️ {compact_number(post.get('forwards'))}"]
    if post.get("reactions_total") is not None:
        parts.append(f"❤️ {compact_number(post['reactions_total'])}")
    if post.get("replies") is not None:
        parts.append(f"💬 {compact_number(post['replies'])}")
    parts.extend(item.badges or [])
    parts.append(time_value.strftime("%d.%m %H:%M"))
    nodes.append({"tag": "p", "children": [" · ".join(parts)]})

    text = _truncate_text(str(post.get("text") or ""), post_chars)
    if text:
        first, rest = _first_sentence(text)
        children = [{"tag": "b", "children": [first]}]
        if rest:
            children.append(" " + rest)
        nodes.append({"tag": "p", "children": children})

    links = []
    if post.get("link"):
        links.append({"tag": "a", "attrs": {"href": str(post["link"])}, "children": ["Читать"]})
    unique_related = {}
    primary = str(post.get("channel_username") or "").casefold()
    for related in item.also_in:
        channel = str(related.get("channel_username") or "").casefold()
        if not channel or channel == primary or channel in unique_related:
            continue
        unique_related[channel] = related
    for related in unique_related.values():
        channel_name = str(related.get("channel_title") or related.get("channel_username") or "Канал")
        if related.get("link"):
            links.append({"tag": "a", "attrs": {"href": str(related["link"])}, "children": [channel_name]})
    if links:
        children = []
        if post.get("link"):
            children.extend([links[0]])
            related_links = links[1:]
        else:
            related_links = links
        if related_links:
            children.append(" · также в: ")
            for index, link in enumerate(related_links):
                if index:
                    children.append(", ")
                children.append(link)
        nodes.append({"tag": "p", "children": children})
    return nodes


def _period_range(period: str, now: datetime, tz=timezone.utc) -> str:
    now = _aware_utc(now).astimezone(tz)
    if period == "day":
        return now.strftime("%d.%m")
    hours = {"week": 168, "month": 720}.get(period)
    if hours is None:
        raise ValueError("period должен быть day, week или month")
    start = now.timestamp() - hours * 3600
    start_dt = datetime.fromtimestamp(start, tz=timezone.utc).astimezone(tz)
    return f"{start_dt:%d.%m}–{now:%d.%m}"


def build_feed_title(period: str, now: datetime, *, tz=timezone.utc) -> str:
    labels = {"day": "день", "week": "неделю", "month": "месяц"}
    if period not in labels:
        raise ValueError("period должен быть day, week или month")
    return f"Лента за {labels[period]} · {_period_range(period, now, _zone(tz))}"[:256]


def render_feed(
    items: list,
    *,
    period: str,
    now: datetime,
    channel_count: int | None = None,
    has_web_source: bool = False,
    incomplete_channels: list[str] | None = None,
    post_chars: int | None = None,
    tz=timezone.utc,
) -> list[dict]:
    """Строит Telegraph nodes для уже ранжированных FeedItem без публикации и I/O."""
    if period not in ("day", "week", "month"):
        raise ValueError("period должен быть day, week или month")
    now = _aware_utc(now)
    tz = _zone(tz)
    limit = FEED_POST_CHARS if post_chars is None else max(1, post_chars)
    if channel_count is None:
        channels = {str(item.post.get("channel_username") or "").casefold() for item in items}
        channels.update(str(post.get("channel_username") or "").casefold() for item in items for post in item.also_in)
        channel_count = len(channels)

    nodes: list[dict] = [
        {"tag": "p", "children": [
            f"{len(items)} {_plural_ru(len(items), 'пост', 'поста', 'постов')} из {channel_count} {_plural_channel(channel_count)} · отбор по охвату и пересылкам · окно {_period_range(period, now, tz)}"
        ]},
        {"tag": "hr"},
    ]
    groups: dict[str | None, list] = defaultdict(list)
    for item in items:
        tag = str(item.tag).strip() if item.tag else None
        groups[tag or None].append(item)
    has_tagged = any(tag is not None for tag in groups)
    ordered_groups = []
    if groups.get(None):
        ordered_groups.append((None, groups[None]))
    ordered_groups.extend(
        (tag, group)
        for tag, group in sorted(
            ((tag, group) for tag, group in groups.items() if tag is not None),
            key=lambda pair: (pair[0].casefold(), pair[0]),
        )
    )

    rank = 1
    for tag, group in ordered_groups:
        # Внутри раздела сохраняется глобальный score-порядок, заданный rank_feed.
        group = sorted(group, key=lambda item: item.rank)
        if has_tagged:
            heading = "Без тега" if tag is None else f"#{tag} · {len(group)}"
            nodes.append({"tag": "h3", "children": [heading]})
        for item in group:
            nodes.extend(_post_nodes(item, rank, limit, tz))
            rank += 1
    if has_web_source:
        nodes.append({"tag": "p", "children": ["ℹ️ Метрики части каналов неполные"]})
    if incomplete_channels:
        nodes.append({"tag": "p", "children": [
            f"ℹ️ Часть каналов не загрузилась: {', '.join(incomplete_channels)}"
        ]})
    return nodes


# Публичное имя для вызовов, следующих глагольному стилю остальных рендереров.
build_feed_content = render_feed
