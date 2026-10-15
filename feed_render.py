"""Чистый рендер Ленты в узлы Telegraph."""

from __future__ import annotations

import os
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

FEED_POST_CHARS = max(1, int(os.getenv("FEED_POST_CHARS", "320")))

_MONTHS_RU = ["", "янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]
_SENTENCE_END_RE = re.compile(r"[.!?…]+(?=\s|$)")


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
    return _plural_ru(number, "канал", "канала", "каналов")


def _truncate_by_word(text: str, limit: int) -> str:
    text = " ".join(text.split())
    if not text:
        return ""
    if len(text) <= limit:
        return text
    cut_limit = max(0, limit - 1)
    last_space = text[:cut_limit].rfind(" ")
    if last_space != -1:
        return text[:last_space].rstrip() + "…"
    return text[:cut_limit] + "…"


_ABBREVIATIONS = {"г", "руб", "коп", "ул", "д", "стр", "корп", "кв", "т", "см", "мм", "км", "кг", "шт"}


def _find_sentence_boundary(text: str) -> int | None:
    for m in _SENTENCE_END_RE.finditer(text):
        pos = m.start()
        prefix = text[:pos].rstrip()
        words = prefix.split()
        if not words:
            continue
        token = words[-1]
        if len(words) == 1 and token.isdigit():
            continue
        if token.lower() in _ABBREVIATIONS:
            continue
        return m.end()
    return None


def _extract_title_and_rest(raw_text: str, title_limit: int = 90) -> tuple[str, str]:
    text = raw_text.strip()
    if not text:
        return "", ""

    newline_pos = text.find("\n")
    punct_end = _find_sentence_boundary(text)

    first_end = None
    if newline_pos != -1 and punct_end is not None:
        first_end = min(newline_pos, punct_end)
    elif newline_pos != -1:
        first_end = newline_pos
    elif punct_end is not None:
        first_end = punct_end

    if first_end is not None:
        candidate = text[:first_end].strip()
        candidate_clean = " ".join(candidate.split())
        if len(candidate_clean) <= title_limit and candidate_clean:
            title = candidate_clean
            if title.endswith(".") and not title.endswith("..") and not title.endswith("…"):
                title = title[:-1].rstrip()
            rest = text[first_end:].strip()
            return title, rest

    clean_text = " ".join(text.split())
    if len(clean_text) <= title_limit:
        title = clean_text
        if title.endswith(".") and not title.endswith("..") and not title.endswith("…"):
            title = title[:-1].rstrip()
        return title, ""

    cut_limit = max(0, title_limit - 1)
    last_space = clean_text[:cut_limit].rfind(" ")
    if last_space != -1:
        title = clean_text[:last_space].rstrip() + "…"
        rest = clean_text[last_space:].lstrip()
    else:
        title = clean_text[:cut_limit] + "…"
        rest = clean_text[cut_limit:].lstrip()
    return title, rest


def _format_excerpt(rest: str, limit: int = 320) -> str:
    return _truncate_by_word(rest, limit)


def _human_badge(badge: str) -> str:
    badge = badge.strip()
    m_fwd = re.search(r"×(\d+)\s+пересылок", badge)
    if m_fwd:
        n = int(m_fwd.group(1))
        times_str = _plural_ru(n, "раз", "раза", "раз")
        return f"🔥 в {n} {times_str} больше пересылок, чем обычно"
    m_reach = re.search(r"×(\d+)\s+к\s+норме", badge)
    if m_reach:
        n = int(m_reach.group(1))
        times_str = _plural_ru(n, "раз", "раза", "раз")
        return f"🔥 в {n} {times_str} больше просмотров, чем обычно"
    if badge == "🆕":
        return "🆕 Свежее"
    return badge


def _format_post_time(post_time: datetime, now: datetime, tz=timezone.utc) -> str:
    p_tz = _aware_utc(post_time).astimezone(tz)
    n_tz = _aware_utc(now).astimezone(tz)
    p_date = p_tz.date()
    n_date = n_tz.date()
    if p_date == n_date:
        return f"сегодня, {p_tz:%H:%M}"
    if p_date == n_date - timedelta(days=1):
        return f"вчера, {p_tz:%H:%M}"
    return f"{p_tz.day} {_MONTHS_RU[p_tz.month]}, {p_tz:%H:%M}"


def _post_nodes(item, post_chars: int, now: datetime, tz=timezone.utc) -> list[dict]:
    post = item.post
    raw_text = str(post.get("text") or "")
    title, rest = _extract_title_and_rest(raw_text)
    channel_name = str(item.channel_title or post.get("channel_username") or "Канал")
    if not title:
        title = channel_name

    nodes: list[dict] = [{"tag": "h4", "children": [title]}]

    time_str = _format_post_time(post["time"], now, tz)
    nodes.append({"tag": "p", "children": [{"tag": "i", "children": [f"{channel_name} · {time_str}"]}]})

    excerpt = _format_excerpt(rest, post_chars)
    if excerpt:
        nodes.append({"tag": "blockquote", "children": [excerpt]})

    metrics = []
    if post.get("views") is not None:
        metrics.append(f"👁 {compact_number(post['views'])}")
    if post.get("forwards") is not None:
        metrics.append(f"↗ {compact_number(post['forwards'])}")
    if post.get("reactions_total") is not None:
        metrics.append(f"❤ {compact_number(post['reactions_total'])}")
    if post.get("replies") is not None:
        metrics.append(f"💬 {compact_number(post['replies'])}")

    human_badges = [_human_badge(b) for b in (item.badges or [])]
    if item.is_ad and not any("Реклама" in b for b in human_badges):
        human_badges.append("📢 Реклама")

    prefix_items = metrics + human_badges

    children: list = []
    if prefix_items:
        prefix_text = " · ".join(prefix_items)
        if post.get("link"):
            children.extend([
                prefix_text + " · ",
                {"tag": "a", "attrs": {"href": str(post["link"])}, "children": ["Читать в канале →"]},
            ])
        else:
            children.append(prefix_text)
    elif post.get("link"):
        children.append({"tag": "a", "attrs": {"href": str(post["link"])}, "children": ["Читать в канале →"]})

    unique_related = {}
    primary = str(post.get("channel_username") or "").casefold()
    for related in item.also_in:
        channel = str(related.get("channel_username") or "").casefold()
        if not channel or channel == primary or channel in unique_related:
            continue
        unique_related[channel] = related

    related_links = []
    for related in unique_related.values():
        rel_channel = str(related.get("channel_title") or related.get("channel_username") or "Канал")
        if related.get("link"):
            related_links.append({"tag": "a", "attrs": {"href": str(related["link"])}, "children": [rel_channel]})
        else:
            related_links.append(rel_channel)

    if related_links:
        if children:
            children.append(" · также в: ")
        else:
            children.append("также в: ")
        for idx, r_link in enumerate(related_links):
            if idx > 0:
                children.append(", ")
            children.append(r_link)

    if children:
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


def _intro_date(period: str, now: datetime, tz=timezone.utc) -> str:
    now_tz = _aware_utc(now).astimezone(tz)
    if period == "day":
        return f"{now_tz.day} {_MONTHS_RU[now_tz.month]}"
    hours = {"week": 168, "month": 720}.get(period)
    if hours is None:
        raise ValueError("period должен быть day, week или month")
    start = now_tz.timestamp() - hours * 3600
    start_dt = datetime.fromtimestamp(start, tz=timezone.utc).astimezone(tz)
    if start_dt.month == now_tz.month and start_dt.year == now_tz.year:
        return f"{start_dt.day}–{now_tz.day} {_MONTHS_RU[now_tz.month]}"
    return f"{start_dt.day} {_MONTHS_RU[start_dt.month]} – {now_tz.day} {_MONTHS_RU[now_tz.month]}"


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

    posts_str = f"{len(items)} {_plural_ru(len(items), 'пост', 'поста', 'постов')}"
    channels_str = f"{channel_count} {_plural_channel(channel_count)}"
    date_str = _intro_date(period, now, tz)

    nodes: list[dict] = [
        {"tag": "p", "children": [{"tag": "i", "children": [f"{posts_str} · {channels_str} · {date_str}"]}]},
        {"tag": "hr"},
    ]

    # Блок «Главное» (h3) — первые 5 по рангу постов, если постов >= 3
    if len(items) >= 3:
        top_items = sorted(items, key=lambda it: it.rank)[:5]
        nodes.append({"tag": "h3", "children": ["Главное"]})
        li_nodes = []
        for it in top_items:
            p_text = str(it.post.get("text") or "")
            t_str, _ = _extract_title_and_rest(p_text)
            ch_name = str(it.channel_title or it.post.get("channel_username") or "Канал")
            if not t_str:
                t_str = ch_name
            if it.post.get("link"):
                title_node = {"tag": "b", "children": [{"tag": "a", "attrs": {"href": str(it.post["link"])}, "children": [t_str]}]}
            else:
                title_node = {"tag": "b", "children": [t_str]}
            suffix = f" — {ch_name}"
            if it.post.get("views") is not None:
                suffix += f" · 👁 {compact_number(it.post['views'])}"
            li_nodes.append({"tag": "li", "children": [title_node, suffix]})
        nodes.append({"tag": "ol", "children": li_nodes})

    # Группировка по разделам-тегам
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

    total_cards = sum(len(group) for _, group in ordered_groups)
    card_index = 0
    for tag, group in ordered_groups:
        group = sorted(group, key=lambda item: item.rank)
        if has_tagged:
            heading = "Без тега" if tag is None else f"#{tag} · {len(group)}"
            nodes.append({"tag": "h3", "children": [heading]})
        for item in group:
            nodes.extend(_post_nodes(item, limit, now, tz))
            card_index += 1
            if card_index < total_cards:
                nodes.append({"tag": "hr"})

    # Подвал: счетчики каналов и информационные плашки
    counts = Counter()
    for item in items:
        name = str(item.post.get("channel_username") or item.channel_title or "Канал").lstrip("@")
        counts[name] += 1
    sorted_channels = sorted(counts.items(), key=lambda pair: (-pair[1], pair[0].casefold()))
    if sorted_channels:
        ch_str = ", ".join(f"{name} ({cnt})" for name, cnt in sorted_channels)
        nodes.append({"tag": "p", "children": [f"Каналы в выпуске: {ch_str}"]})

    if has_web_source:
        nodes.append({"tag": "p", "children": ["ℹ️ Метрики части каналов неполные"]})
    if incomplete_channels:
        nodes.append({"tag": "p", "children": [
            f"ℹ️ Часть каналов не загрузилась: {', '.join(incomplete_channels)}"
        ]})

    return nodes


# Публичное имя для вызовов, следующих глагольному стилю остальных рендереров.
build_feed_content = render_feed
