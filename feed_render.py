"""Чистый рендер Ленты в узлы Telegraph."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from config import settings
from post_text import (
    _ABBREVIATIONS,
    _EMOJI_CLUSTER_RE,
    _KNOWN_ABBREVIATIONS,
    _SENTENCE_END_RE,
    _VOWELS,
    _clean_post_text,
    _clean_service_lines,
    _collapse_emoji_chains,
    _extract_bare_url_domain,
    _extract_title_and_essence,
    _find_semantic_cut,
    _find_sentence_boundary,
    _first_sentence,
    _is_abbreviation,
    _is_banner_line,
    _normalize_caps_title,
    _split_first_sentence,
    _split_sentences,
    _strip_banner_line,
    _strip_forwarding_lines,
    _strip_title_prefix,
    _truncate_by_word,
    clean_post_for_render,
)

FEED_POST_CHARS = settings().feed_ranking.post_chars

_MONTHS_RU = ["", "янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]
_WEEKDAYS_RU = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]

__all__ = [
    "FEED_POST_CHARS",
    "_ABBREVIATIONS",
    "_EMOJI_CLUSTER_RE",
    "_KNOWN_ABBREVIATIONS",
    "_MONTHS_RU",
    "_SENTENCE_END_RE",
    "_VOWELS",
    "_WEEKDAYS_RU",
    "_aware_utc",
    "_build_short_item_node",
    "_channel_display_name",
    "_clean_post_text",
    "_clean_service_lines",
    "_collapse_emoji_chains",
    "_count_words",
    "_extract_bare_url_domain",
    "_extract_text_nodes",
    "_extract_title_and_essence",
    "_find_semantic_cut",
    "_find_sentence_boundary",
    "_first_sentence",
    "_format_post_time",
    "_full_card_nodes",
    "_human_badge",
    "_intro_date",
    "_is_abbreviation",
    "_is_banner_line",
    "_normalize_caps_title",
    "_overview_nodes",
    "_period_range",
    "_plural_channel",
    "_plural_channel_genitive",
    "_plural_channel_nominative",
    "_plural_ru",
    "_split_first_sentence",
    "_split_sentences",
    "_strip_banner_line",
    "_strip_forwarding_lines",
    "_strip_title_prefix",
    "_truncate_by_word",
    "_zone",
    "build_feed_content",
    "build_feed_title",
    "clean_post_for_render",
    "compact_number",
    "render_feed",
]


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
    clean_text = clean_post_for_render(raw_text)
    channel_name = _channel_display_name(item)

    if not clean_text:
        title = "Без названия"
        essence = ""
    else:
        title, essence = _extract_title_and_essence(clean_text, max_title=140, max_essence=post_chars)
        if not title or title.casefold() == channel_name.casefold():
            title = "Без названия"

    nodes: list[dict] = [{"tag": "h4", "children": [title]}]

    if essence:
        nodes.append({"tag": "p", "children": [essence]})

    time_str = _format_post_time(post["time"], now, tz)
    meta_children: list = [{"tag": "i", "children": [f"{channel_name} · {time_str}"]}]

    primary_username = str(post.get("channel_username") or "").casefold()
    primary_title = str(item.channel_title or post.get("channel_title") or "").casefold()
    seen_channels = set()
    if primary_username:
        seen_channels.add(primary_username)
    if primary_title:
        seen_channels.add(primary_title)

    unique_related = []
    for related in item.also_in:
        rel_u = str(related.get("channel_username") or "").casefold()
        rel_t = str(related.get("channel_title") or "").casefold()
        if rel_u and rel_u in seen_channels:
            continue
        if rel_t and rel_t in seen_channels:
            continue
        if not rel_u and not rel_t:
            continue
        if rel_u:
            seen_channels.add(rel_u)
        if rel_t:
            seen_channels.add(rel_t)
        unique_related.append(related)

    if unique_related:
        meta_children.append(" · также: ")
        top_related = unique_related[:2]
        for idx, related in enumerate(top_related):
            if idx > 0:
                meta_children.append(", ")
            rel_channel = _channel_display_name(related)
            if related.get("link"):
                meta_children.append({"tag": "a", "attrs": {"href": str(related["link"])}, "children": [rel_channel]})
            else:
                meta_children.append(rel_channel)
        if len(unique_related) > 2:
            rem = len(unique_related) - 2
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


def _build_short_item_node(it) -> dict:
    raw_text = str(it.post.get("text") or "")
    clean_text = clean_post_for_render(raw_text)
    domain = _extract_bare_url_domain(clean_text)
    if domain:
        short_title = f"🔗 ссылка · {domain}"
    else:
        clean_len = len(" ".join(clean_text.split()))
        if clean_len < 80:
            short_title = _normalize_caps_title(" ".join(clean_text.split()))
        else:
            first_sent, _ = _split_first_sentence(clean_text)
            first_sent_norm = " ".join(first_sent.split())
            if first_sent_norm.endswith(".") and not first_sent_norm.endswith("..") and not first_sent_norm.endswith("…"):
                first_sent_norm = first_sent_norm[:-1].rstrip()
            if len(first_sent_norm) <= 140:
                short_title = _normalize_caps_title(first_sent_norm)
            else:
                cut = _find_semantic_cut(first_sent_norm, 50, 140)
                if cut:
                    short_title = _normalize_caps_title(cut)
                else:
                    short_title = _normalize_caps_title(_truncate_by_word(first_sent_norm, 140))
    short_title = short_title.lstrip("…. ").strip()
    if short_title and short_title[0].islower():
        short_title = short_title[0].upper() + short_title[1:]
    if not short_title:
        short_title = "Без названия"
    ch_name = _channel_display_name(it)
    link_node = {"tag": "a", "attrs": {"href": str(it.post["link"])}, "children": [short_title]}
    suffix = f" — {ch_name}"
    if it.post.get("views") is not None:
        suffix += f" · 👁 {compact_number(it.post['views'])}"
    return {"tag": "li", "children": [link_node, suffix]}


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


def _plural_channel(number: int) -> str:
    """Совместимость с feed.py: «из N канала/каналов» (как в прежнем рендере)."""
    return _plural_ru(number, "канала", "каналов", "каналов")



def _overview_nodes(overview: str) -> list[dict]:
    """Блок «Главное за период» из уже очищенного ИИ-обзора."""
    lines = [line.strip() for line in str(overview).splitlines() if line.strip()]
    body_parts = []
    topics = ""
    for line in lines:
        if line.startswith("Темы:"):
            topics = line
        else:
            body_parts.append(line)
    if not topics:
        combined = " ".join(body_parts).strip()
        marker = combined.find("Темы:")
        if marker > 0:
            body_parts = [combined[:marker].strip()]
            topics = combined[marker:].strip()
        elif marker == 0:
            body_parts = []
            topics = combined
    body = " ".join(body_parts).strip()
    children = []
    if body:
        children.append({"tag": "p", "children": [body]})
    if topics:
        children.append({"tag": "p", "children": [{"tag": "i", "children": [topics]}]})
    if not children:
        return []
    return [
        {"tag": "h3", "children": ["Главное за период"]},
        {"tag": "blockquote", "children": children},
    ]


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
    overview: str | None = None,
) -> list[dict]:
    """Строит Telegraph nodes для уже ранжированных FeedItem без публикации и I/O."""
    if period not in ("day", "week", "month"):
        raise ValueError("period должен быть day, week или month")
    now = _aware_utc(now)
    tz = _zone(tz)
    limit = (FEED_POST_CHARS if FEED_POST_CHARS is not None else settings().feed_ranking.post_chars) if post_chars is None else max(1, post_chars)

    sorted_items = sorted(items, key=lambda it: it.rank)

    max_full = 6 if period == "day" else (8 if period == "week" else 10)
    channel_card_counts: Counter = Counter()
    full_items = []
    short_candidates = []

    for it in sorted_items:
        raw_text = str(it.post.get("text") or "")
        clean = _clean_post_text(raw_text)
        clean_len = len(" ".join(clean.split()))
        has_link_in_text = bool(re.search(r"https?://|t\.me/", clean))
        has_post_link = bool(it.post.get("link"))

        # Rule 3: совсем пустые (<25 символов, без ссылки в тексте) не показываются нигде
        if clean_len < 25 and not has_link_in_text:
            continue

        # Rule 3: полная карточка — только для постов с содержательным текстом >= 80 символов (после очистки)
        if clean_len < 80:
            if has_post_link:
                short_candidates.append(it)
            continue

        ch_key = str(it.post.get("channel_username") or it.channel_title or "канал").strip().casefold()
        if len(full_items) < max_full and channel_card_counts[ch_key] < 2:
            full_items.append(it)
            channel_card_counts[ch_key] += 1
        else:
            if has_post_link:
                short_candidates.append(it)

    body_nodes: list[dict] = []
    if overview:  # необязательный ИИ-обзор (feed_ai.py); при его наличии оглавление «Главное» не нужно
        body_nodes.extend(_overview_nodes(overview))

    # Rule 2: Блок «Главное» только когда полных карточек >= 7 (неделя/месяц), 5 строк «заголовок — канал» без сути
    if not overview and period in ("week", "month") and len(full_items) >= 7:
        top_items = full_items[:5]
        glavnoe_li = []
        for it in top_items:
            raw_text = str(it.post.get("text") or "")
            clean = clean_post_for_render(raw_text)
            t_str, _ = _extract_title_and_essence(clean, max_title=140)
            if not t_str:
                t_str = "Без названия"
            ch_name = _channel_display_name(it)
            glavnoe_li.append({
                "tag": "li",
                "children": [{"tag": "b", "children": [t_str]}, f" — {ch_name}"],
            })
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

    # Rule 5: «Коротко» <= 15 строк; для недели/месяца группировка по дням (h4) внутри «Коротко»
    short_items = short_candidates[:15]
    if short_items:
        body_nodes.append({"tag": "h3", "children": ["Коротко"]})
        if period == "day":
            short_li = [_build_short_item_node(it) for it in short_items]
            body_nodes.append({"tag": "ul", "children": short_li})
        else:
            day_groups = defaultdict(list)
            for it in short_items:
                p_time = _aware_utc(it.post["time"]).astimezone(tz)
                day_groups[p_time.date()].append(it)

            sorted_dates = sorted(day_groups.keys(), reverse=True)

            for d in sorted_dates:
                day_heading = f"{_WEEKDAYS_RU[d.weekday()].capitalize()}, {d.day} {_MONTHS_RU[d.month]}"
                day_items = sorted(day_groups[d], key=lambda x: x.rank)
                day_li = [_build_short_item_node(it) for it in day_items]
                body_nodes.append({"tag": "h4", "children": [day_heading]})
                body_nodes.append({"tag": "ul", "children": day_li})

    # Rule 6: Подвал «Каналы в выпуске»: число постов В ВЫПУСКЕ (после отбора), без «+N», макс 10
    selected_items = full_items + short_items
    counts: Counter = Counter()
    for item in selected_items:
        name = _channel_display_name(item).lstrip("@")
        counts[name] += 1
    sorted_channels = sorted(counts.items(), key=lambda pair: (-pair[1], pair[0].casefold()))[:10]
    if sorted_channels:
        ch_str = ", ".join(f"{name} ({cnt})" for name, cnt in sorted_channels)
        body_nodes.append({"tag": "p", "children": [f"Каналы в выпуске: {ch_str}"]})

    if has_web_source:
        body_nodes.append({"tag": "p", "children": ["ℹ️ Метрики части каналов неполные"]})
    if incomplete_channels:
        body_nodes.append({"tag": "p", "children": [
            f"ℹ️ Часть каналов не загрузилась: {', '.join(incomplete_channels)}"
        ]})

    if channel_count is None:
        channels = {str(item.post.get("channel_username") or item.channel_title or "").casefold() for item in (selected_items if selected_items else items)}
        channels.discard("")
        channel_count = len(channels)

    labels = {"day": "день", "week": "неделю", "month": "месяц"}
    date_str = _intro_date(period, now, tz)
    posts_count = len(selected_items)
    posts_str = f"{posts_count} {_plural_ru(posts_count, 'пост', 'поста', 'постов')}"
    channels_str = f"{channel_count} {_plural_channel_genitive(channel_count)}"
    intro_base = f"Лента за {labels[period]} · {date_str} — {posts_str} из {channels_str} · ≈"
    intro_base_words = len(intro_base.split()) + 2
    total_words = intro_base_words + _count_words(body_nodes)
    minutes = max(1, round(total_words / 180))
    intro_text = f"Лента за {labels[period]} · {date_str} — {posts_str} из {channels_str} · ≈{minutes} мин чтения"
    intro_node = {"tag": "p", "children": [{"tag": "i", "children": [intro_text]}]}

    return [intro_node] + body_nodes


build_feed_content = render_feed
