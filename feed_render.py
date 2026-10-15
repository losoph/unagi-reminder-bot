"""Чистый рендер Ленты в узлы Telegraph."""

from __future__ import annotations

import os
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

FEED_POST_CHARS = max(1, int(os.getenv("FEED_POST_CHARS", "300")))

_MONTHS_RU = ["", "янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]
_WEEKDAYS_RU = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
_SENTENCE_END_RE = re.compile(r"[.!?…]+(?=\s|$)")
_ABBREVIATIONS = {"г", "руб", "коп", "ул", "д", "стр", "корп", "кв", "т", "см", "мм", "км", "кг", "шт"}


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


def _plural_channel_nominative(number: int) -> str:
    return _plural_ru(number, "канал", "канала", "каналов")


_plural_channel = _plural_channel_nominative


def _plural_channel_genitive(number: int) -> str:
    return _plural_ru(number, "канала", "каналов", "каналов")


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


def _extract_essence(raw_text: str, title: str, max_sentences: int = 2, limit: int = 300) -> str:
    clean_raw = " ".join(raw_text.split())
    if not clean_raw or len(clean_raw) < len(title) + 40:
        return ""

    prefix = title.rstrip("…").strip()
    idx = clean_raw.find(prefix) if prefix else 0
    if idx != -1:
        rest = clean_raw[idx + len(prefix):].lstrip(" .!?…:;-")
    else:
        rest = clean_raw

    rest = " ".join(rest.split())
    if not rest:
        return ""

    sentences: list[str] = []
    current = rest
    for _ in range(max_sentences):
        b = _find_sentence_boundary(current)
        if b is not None:
            sentences.append(current[:b].strip())
            current = current[b:].strip()
            if not current:
                break
        else:
            if current:
                sentences.append(current.strip())
            break

    if not sentences:
        return ""

    if len(sentences) == 1:
        text = sentences[0]
    else:
        combined = " ".join(sentences[:max_sentences])
        if len(combined) <= limit:
            text = combined
        elif len(sentences[0]) <= limit and len(sentences[0]) >= 40:
            text = sentences[0]
        else:
            text = combined

    if len(text) <= limit:
        return text
    return _truncate_by_word(text, limit)


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
    m_pin = re.search(r"(\d+)\s+канал", badge)
    if m_pin:
        n = int(m_pin.group(1))
        return f"📌 писали {n} {_plural_channel_nominative(n)}"
    if badge.startswith("📌"):
        return badge
    if "🆕" in badge:
        return "🆕 новость"
    if "реклама" in badge.lower():
        return "📢 реклама"
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
    return f"{_WEEKDAYS_RU[p_tz.weekday()]} {p_tz:%d.%m, %H:%M}"


def _channel_display_name(item_or_post) -> str:
    if hasattr(item_or_post, "channel_title") and item_or_post.channel_title:
        return str(item_or_post.channel_title)
    if isinstance(item_or_post, dict):
        return str(item_or_post.get("channel_title") or item_or_post.get("channel_username") or "Канал")
    post = getattr(item_or_post, "post", {})
    return str(post.get("channel_title") or post.get("channel_username") or "Канал")


def _full_card_nodes(item, post_chars: int, now: datetime, tz=timezone.utc) -> list[dict]:
    post = item.post
    raw_text = str(post.get("text") or "")
    title, _ = _extract_title_and_rest(raw_text)
    channel_name = _channel_display_name(item)

    if not title or title.casefold() == channel_name.casefold():
        title = "Без названия"

    nodes: list[dict] = [{"tag": "h4", "children": [title]}]

    essence = _extract_essence(raw_text, title, max_sentences=2, limit=post_chars)
    if essence:
        nodes.append({"tag": "p", "children": [essence]})

    time_str = _format_post_time(post["time"], now, tz)
    meta_children: list = [{"tag": "i", "children": [f"{channel_name} · {time_str}"]}]

    primary = str(post.get("channel_username") or "").casefold()
    unique_related = []
    seen_channels = {primary} if primary else set()
    for related in item.also_in:
        rel_ch = str(related.get("channel_username") or "").casefold()
        if not rel_ch or rel_ch in seen_channels:
            continue
        seen_channels.add(rel_ch)
        unique_related.append(related)

    if unique_related:
        meta_children.append(" · также: ")
        top_related = unique_related[:4]
        for idx, related in enumerate(top_related):
            if idx > 0:
                meta_children.append(", ")
            rel_channel = _channel_display_name(related)
            if related.get("link"):
                meta_children.append({"tag": "a", "attrs": {"href": str(related["link"])}, "children": [rel_channel]})
            else:
                meta_children.append(rel_channel)
        if len(unique_related) > 4:
            rem = len(unique_related) - 4
            meta_children.append(f" (+{rem})")

    if post.get("link"):
        meta_children.append(" · ")
        meta_children.append({"tag": "a", "attrs": {"href": str(post["link"])}, "children": ["Читать →"]})

    metrics = []
    if post.get("views") is not None:
        metrics.append(f"👁 {compact_number(post['views'])}")
    if post.get("forwards") is not None:
        metrics.append(f"↗ {compact_number(post['forwards'])}")
    if post.get("reactions_total") is not None and post["reactions_total"] > 0:
        metrics.append(f"❤ {compact_number(post['reactions_total'])}")
    if post.get("replies") is not None and post["replies"] > 0:
        metrics.append(f"💬 {compact_number(post['replies'])}")

    human_badges = [_human_badge(b) for b in (item.badges or [])]
    if item.is_ad and not any("реклама" in b.lower() for b in human_badges):
        human_badges.append("📢 реклама")

    meta_items = metrics + human_badges
    if meta_items:
        meta_children.append(" · " + " · ".join(meta_items))

    nodes.append({"tag": "p", "children": meta_children})
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


def _extract_text_nodes(node) -> list[str]:
    texts: list[str] = []
    if isinstance(node, str):
        texts.append(node)
    elif isinstance(node, dict):
        for child in node.get("children", []):
            texts.extend(_extract_text_nodes(child))
    elif isinstance(node, list):
        for item in node:
            texts.extend(_extract_text_nodes(item))
    return texts


def _count_words(nodes) -> int:
    return sum(len(text.split()) for text in _extract_text_nodes(nodes))


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
        channels = {str(item.post.get("channel_username") or item.channel_title or "").casefold() for item in items}
        channels.update(
            str(post.get("channel_username") or post.get("channel_title") or "").casefold()
            for item in items
            for post in item.also_in
        )
        channels.discard("")
        channel_count = len(channels)

    sorted_items = sorted(items, key=lambda it: it.rank)

    max_full = 6 if period == "day" else (8 if period == "week" else 10)
    channel_card_counts: Counter = Counter()
    full_items = []
    short_candidates = []

    for it in sorted_items:
        ch_key = str(it.post.get("channel_username") or it.channel_title or "канал").strip().casefold()
        if len(full_items) < max_full and channel_card_counts[ch_key] < 2:
            full_items.append(it)
            channel_card_counts[ch_key] += 1
        else:
            short_candidates.append(it)

    body_nodes: list[dict] = []

    if len(items) >= 3:
        max_glavnoe = 3 if period == "day" else 5
        top_items = sorted_items[:max_glavnoe]
        glavnoe_li = []
        for it in top_items:
            p_text = str(it.post.get("text") or "")
            t_str, _ = _extract_title_and_rest(p_text, title_limit=90)
            if not t_str:
                t_str = "Без названия"
            essence_glavnoe = _extract_essence(p_text, t_str, max_sentences=1, limit=140)
            b_node = {"tag": "b", "children": [t_str]}
            if essence_glavnoe:
                li_children = [b_node, f" — {essence_glavnoe}"]
            else:
                li_children = [b_node]
            glavnoe_li.append({"tag": "li", "children": li_children})
        body_nodes.append({"tag": "h3", "children": ["Главное"]})
        body_nodes.append({"tag": "ul", "children": glavnoe_li})

    groups: dict[str | None, list] = defaultdict(list)
    for item in full_items:
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

    total_full_cards = len(full_items)
    card_index = 0

    if not has_tagged and full_items:
        body_nodes.append({"tag": "h3", "children": ["Сюжеты"]})
        for item in full_items:
            body_nodes.extend(_full_card_nodes(item, limit, now, tz))
            card_index += 1
            if card_index in (1, 2, 3) and card_index < total_full_cards:
                body_nodes.append({"tag": "hr"})
    elif has_tagged:
        for tag, group in ordered_groups:
            heading = "Без тега" if tag is None else f"#{tag} · {len(group)}"
            body_nodes.append({"tag": "h3", "children": [heading]})
            for item in group:
                body_nodes.extend(_full_card_nodes(item, limit, now, tz))
                card_index += 1
                if card_index in (1, 2, 3) and card_index < total_full_cards:
                    body_nodes.append({"tag": "hr"})

    short_items = []
    for it in short_candidates:
        raw_text = str(it.post.get("text") or "")
        clean_text = " ".join(raw_text.split())
        if not it.post.get("link") or len(clean_text) < 40:
            continue
        short_items.append(it)
        if len(short_items) == 25:
            break

    if short_items:
        short_li = []
        for it in short_items:
            raw_text = str(it.post.get("text") or "")
            short_title, _ = _extract_title_and_rest(raw_text, title_limit=80)
            if not short_title:
                short_title = "Без названия"
            ch_name = _channel_display_name(it)
            link_node = {"tag": "a", "attrs": {"href": str(it.post["link"])}, "children": [short_title]}
            suffix = f" — {ch_name}"
            if it.post.get("views") is not None:
                suffix += f" · 👁 {compact_number(it.post['views'])}"
            short_li.append({"tag": "li", "children": [link_node, suffix]})
        body_nodes.append({"tag": "h3", "children": ["Коротко"]})
        body_nodes.append({"tag": "ul", "children": short_li})

    counts: Counter = Counter()
    for item in items:
        name = _channel_display_name(item).lstrip("@")
        counts[name] += 1
    sorted_channels = sorted(counts.items(), key=lambda pair: (-pair[1], pair[0].casefold()))
    if sorted_channels:
        if len(sorted_channels) <= 8:
            ch_str = ", ".join(f"{name} ({cnt})" for name, cnt in sorted_channels)
        else:
            top_8 = sorted_channels[:8]
            rem = len(sorted_channels) - 8
            ch_str = ", ".join(f"{name} ({cnt})" for name, cnt in top_8) + f" · +{rem}"
        body_nodes.append({"tag": "p", "children": [f"Каналы в выпуске: {ch_str}"]})

    if has_web_source:
        body_nodes.append({"tag": "p", "children": ["ℹ️ Метрики части каналов неполные"]})
    if incomplete_channels:
        body_nodes.append({"tag": "p", "children": [
            f"ℹ️ Часть каналов не загрузилась: {', '.join(incomplete_channels)}"
        ]})

    labels = {"day": "день", "week": "неделю", "month": "месяц"}
    date_str = _intro_date(period, now, tz)
    posts_str = f"{len(items)} {_plural_ru(len(items), 'пост', 'поста', 'постов')}"
    channels_str = f"{channel_count} {_plural_channel_genitive(channel_count)}"
    intro_base = f"Лента за {labels[period]} · {date_str} — {posts_str} из {channels_str} · ≈"
    intro_base_words = len(intro_base.split()) + 2
    total_words = intro_base_words + _count_words(body_nodes)
    minutes = max(1, round(total_words / 180))
    intro_text = f"Лента за {labels[period]} · {date_str} — {posts_str} из {channels_str} · ≈{minutes} мин чтения"
    intro_node = {"tag": "p", "children": [{"tag": "i", "children": [intro_text]}]}

    return [intro_node] + body_nodes


build_feed_content = render_feed
