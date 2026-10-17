"""Чистые функции отбора и ранжирования постов Ленты."""

from __future__ import annotations

import difflib
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from heapq import heappop, heappush
from urllib.parse import urlsplit, urlunsplit

from config import settings

_WEIGHTS = dict(settings().feed_ranking.weights)
_MAX_POSTS = settings().feed_ranking.max_posts
_AD_MIN_PERCENTILE = settings().feed_ranking.ad_min_percentile
_AD_MIN_POOL = settings().feed_ranking.ad_min_pool
_CLUSTER_MIN_TEXT = settings().feed_ranking.cluster_min_text
_AD_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\berid\b",
        r"^\s*реклама\b",
        r"#реклама",
        r"#ad\b",
        r"партн[её]рский материал",
        r"на правах рекламы",
    )
)
_URL_RE = re.compile(r"https?://[^\s<>\]\[(){}\"']+", re.IGNORECASE)
_TEXT_CLEAN_RE = re.compile(r"[^\w\s]", re.UNICODE)
_SPACE_RE = re.compile(r"\s+")
_HALF_LIVES = {"day": 12.0, "week": 72.0, "month": 240.0}
_CAPS = {"day": 5, "week": 10, "month": 20}
# Текстовое сравнение: порог, окно и дешёвый предварительный отсев.
_CLUSTER_SIMILARITY = 0.6
_CLUSTER_WINDOW_SECONDS = 36 * 3600
_CLUSTER_TOP_TEXTS = 300
_SHINGLE_SIZE = 6
# Дешёвый предварительный отсев: настоящие почти-дубли делят длинные подстроки.
# Порог намеренно низкий, чтобы не терять реальные почти-дубли; это практическая
# оценка, а не доказанная верхняя граница, поэтому пары с экстремальными
# посимвольными правками теоретически могут быть пропущены.
_MIN_SHINGLE_MATCHES = 10


@dataclass
class FeedItem:
    rank: int
    score: float
    post: dict
    also_in: list[dict]
    badges: list[str]
    channel_title: str
    tag: str | None
    is_ad: bool


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Время поста и now должны быть aware")
    return value.astimezone(timezone.utc)


def _key(post: dict) -> tuple[datetime, str, int]:
    return (
        _utc(post["time"]),
        str(post.get("channel_username") or "").casefold(),
        int(post.get("post_id") or 0),
    )


def _tie_key(post: dict) -> tuple:
    timestamp, channel, post_id = _key(post)
    return -timestamp.timestamp(), channel, post_id


def _median_for(medians: dict, channel: str, key: str):
    value = medians.get(channel, {})
    return value.get(key) if isinstance(value, dict) else None


def mark_ads(posts: list[dict]) -> list[dict]:
    """Копирует записи и отмечает рекламу, не отбрасывая её."""
    result = []
    for post in posts:
        item = dict(post)
        text = str(item.get("text") or "")
        item["is_ad"] = any(pattern.search(text) for pattern in _AD_PATTERNS)
        result.append(item)
    return result


def _signals(post: dict, medians: dict, now: datetime, period: str) -> dict:
    now = _utc(now)
    post_time = _utc(post["time"])
    age_h = max(0.1, (now - post_time).total_seconds() / 3600)
    maturity = max(0.25, 1 - math.exp(-age_h / 8))
    views, forwards = post.get("views"), post.get("forwards")
    channel = str(post.get("channel_username") or "")
    signals = {name: None for name in _WEIGHTS}
    if views is not None and views > 0:
        views_m = views / maturity
        signals["reach_abs"] = math.log10(1 + views_m)
        median_views = _median_for(medians, channel, "median_views")
        if median_views is not None and median_views > 0:
            signals["reach_rel"] = views_m / median_views
        reactions, replies = post.get("reactions_total"), post.get("replies")
        if reactions is not None or replies is not None:
            signals["engagement"] = ((reactions or 0) + 2 * (replies or 0)) / views
    if forwards is not None:
        signals["fwd_abs"] = math.log10(1 + max(0, forwards))
        median_fwd = _median_for(medians, channel, "median_fwd_rate")
        if views is not None and views > 0 and median_fwd is not None and median_fwd > 0:
            signals["fwd_rel"] = (forwards / views) / median_fwd
    fresh = math.exp(-age_h / _HALF_LIVES[period])
    return {**signals, "fresh": fresh}


def percentiles(candidates: list[dict]) -> list[dict]:
    """Заполняет percentiles на всех кандидатах, пропуская None."""
    result = [dict(candidate) for candidate in candidates]
    for signal in _WEIGHTS:
        valid = [(candidate["signals"][signal], index) for index, candidate in enumerate(result)
                 if candidate["signals"].get(signal) is not None]
        valid.sort(key=lambda pair: pair[0])
        n = len(valid)
        position = 0
        while position < n:
            end = position + 1
            while end < n and valid[end][0] == valid[position][0]:
                end += 1
            percentile = 1.0 if n == 1 else (((position + 1 + end) / 2) - 1) / (n - 1)
            for _, index in valid[position:end]:
                result[index].setdefault("percentiles", {})[signal] = percentile
            position = end
    return result


def score_candidates(posts: list[dict], medians_by_channel: dict, period: str, now: datetime) -> list[dict]:
    if period not in _HALF_LIVES:
        raise ValueError("period должен быть day, week или month")
    now = _utc(now)
    candidates = []
    for post in mark_ads(posts):
        signals = _signals(post, medians_by_channel or {}, now, period)
        candidates.append({"post": post, "signals": signals, "percentiles": {}})
    candidates = percentiles(candidates)
    pool_sizes = {name: sum(candidate["signals"].get(name) is not None for candidate in candidates)
                  for name in ("reach_abs", "fwd_abs")}
    total_weight = sum(_WEIGHTS.values())
    for candidate in candidates:
        values = candidate["percentiles"]
        weight_sum = sum(_WEIGHTS[name] for name in values)
        quality = (sum(_WEIGHTS[name] * value for name, value in values.items()) / weight_sum
                   if weight_sum else 0.0)
        coverage = weight_sum / total_weight if total_weight else 0.0
        quality = coverage * quality + (1 - coverage) * 0.5
        candidate["score"] = 0.95 * quality + 0.05 * candidate["signals"]["fresh"]
        ad_values = [values[name] for name in ("reach_abs", "fwd_abs")
                     if name in values and pool_sizes[name] >= _AD_MIN_POOL]
        candidate["ad_strength"] = sum(ad_values) / len(ad_values) if ad_values else None
    return candidates


def _canonical_url(text: str) -> str | None:
    for match in _URL_RE.finditer(text):
        raw = match.group(0).rstrip(".,;:!?…")
        try:
            parts = urlsplit(raw)
            host = (parts.hostname or "").casefold().rstrip(".")
            if not host or host == "t.me" or host.endswith(".t.me"):
                continue
            try:
                host = host.encode("idna").decode("ascii")
            except UnicodeError:
                continue
            host = host.removeprefix("www.")
            port = parts.port
            netloc = host if port is None or (parts.scheme.lower(), port) in (("http", 80), ("https", 443)) else f"{host}:{port}"
            query = "&".join(pair for pair in parts.query.split("&")
                             if pair and not pair.split("=", 1)[0].casefold().startswith("utm_"))
            return urlunsplit(("https", netloc, parts.path.rstrip("/"), query, ""))
        except ValueError:
            continue
    return None


def _normalized_text(text: str) -> str:
    text = _URL_RE.sub(" ", text.casefold())
    text = _TEXT_CLEAN_RE.sub(" ", text)
    return _SPACE_RE.sub(" ", text).strip()[:400]


class _UnionFind:
    def __init__(self, size: int):
        self.parent = list(range(size))

    def find(self, index: int) -> int:
        while self.parent[index] != index:
            self.parent[index] = self.parent[self.parent[index]]
            index = self.parent[index]
        return index

    def union(self, left: int, right: int) -> None:
        left_root, right_root = self.find(left), self.find(right)
        if left_root != right_root:
            self.parent[max(left_root, right_root)] = min(left_root, right_root)


def cluster_candidates(candidates: list[dict]) -> list[dict]:
    if len(candidates) < 2:
        return [dict(candidate, members=[candidate]) for candidate in candidates]
    candidates = sorted(candidates, key=lambda candidate: _tie_key(candidate["post"]))
    uf = _UnionFind(len(candidates))
    keyed: dict[tuple, int] = {}
    for index, candidate in enumerate(candidates):
        post = candidate["post"]
        fwd = (post.get("fwd_from_channel_id"), post.get("fwd_from_post_id"))
        own = (post.get("channel_id"), post.get("post_id"))
        for pair in (fwd, own):
            if pair[0] is not None and pair[1] is not None:
                if pair in keyed:
                    uf.union(index, keyed[pair])
                else:
                    keyed[pair] = index
        url = _canonical_url(str(post.get("text") or ""))
        if url:
            if ("url", url) in keyed:
                uf.union(index, keyed[("url", url)])
            else:
                keyed[("url", url)] = index
    # Текстовая кластеризация ограничена топ-300; прямой источник и URL остаются без этого лимита.
    text_candidates = sorted(candidates, key=lambda candidate: (-candidate["score"], _tie_key(candidate["post"])))[:_CLUSTER_TOP_TEXTS]
    positions = {id(candidate): index for index, candidate in enumerate(candidates)}
    eligible = []
    for order, candidate in enumerate(text_candidates):
        text = _normalized_text(str(candidate["post"].get("text") or ""))
        if len(text) < _CLUSTER_MIN_TEXT:
            continue
        eligible.append((
            positions[id(candidate)],
            order,
            text,
            _utc(candidate["post"]["time"]).timestamp(),
            Counter(text),
            len(text),
            frozenset(text[start:start + _SHINGLE_SIZE] for start in range(len(text) - _SHINGLE_SIZE + 1)),
        ))
    # Скользящее окно по времени заменяет перебор всех пар: сравниваются ровно пары
    # в пределах 36 ч. Ориентация ratio() сохраняется как в исходнике (a — текст,
    # идущий раньше по score-порядку), иначе autojunk даёт другой результат.
    eligible.sort(key=lambda entry: (entry[3], entry[0]))
    matchers: dict[int, difflib.SequenceMatcher] = {}
    eligible_count = len(eligible)
    for left_pos in range(eligible_count):
        left_index, left_order, left_text, left_time, left_chars, left_len, left_shingles = eligible[left_pos]
        for right_pos in range(left_pos + 1, eligible_count):
            right_index, right_order, right_text, right_time, right_chars, right_len, right_shingles = eligible[right_pos]
            if right_time - left_time > _CLUSTER_WINDOW_SECONDS:
                break
            if uf.find(left_index) == uf.find(right_index):
                continue
            total_len = left_len + right_len
            # Точные верхние оценки до дорогого ratio(): длины (real_quick_ratio)
            # и мультимножество символов (quick_ratio).
            if 2 * min(left_len, right_len) < _CLUSTER_SIMILARITY * total_len:
                continue
            if left_text == right_text:
                uf.union(left_index, right_index)
                continue
            if left_len <= right_len:
                common = sum(min(amount, right_chars.get(char, 0)) for char, amount in left_chars.items())
            else:
                common = sum(min(amount, left_chars.get(char, 0)) for char, amount in right_chars.items())
            if 2 * common < _CLUSTER_SIMILARITY * total_len:
                continue
            if len(left_shingles & right_shingles) < _MIN_SHINGLE_MATCHES:
                continue
            if left_order < right_order:
                first_text, second_order, second_text = left_text, right_order, right_text
            else:
                first_text, second_order, second_text = right_text, left_order, left_text
            matcher = matchers.get(second_order)
            if matcher is None:
                matcher = difflib.SequenceMatcher(None, "", second_text)
                matchers[second_order] = matcher
            matcher.set_seq1(first_text)
            if matcher.ratio() >= _CLUSTER_SIMILARITY:
                uf.union(left_index, right_index)
    groups: dict[int, list[dict]] = defaultdict(list)
    # Map identity rather than dict equality (candidate dictionaries can nest mutable objects).
    for candidate in candidates:
        groups[uf.find(positions[id(candidate)])].append(candidate)
    clusters = []
    for members in groups.values():
        members.sort(key=lambda c: (-c["score"], _tie_key(c["post"])) )
        primary_sources = {
            (member["post"].get("fwd_from_channel_id"), member["post"].get("fwd_from_post_id"))
            for member in members
            if member["post"].get("fwd_from_channel_id") is not None
            and member["post"].get("fwd_from_post_id") is not None
        }
        originals = [
            member for member in members
            if (member["post"].get("channel_id"), member["post"].get("post_id")) in primary_sources
        ]
        representative = min(originals or members, key=lambda c: (-c["score"], _tie_key(c["post"])))
        unique_channels = len({str(member["post"].get("channel_username") or "").casefold() for member in members})
        bonus = min(0.15, 0.05 * math.log2(max(1, unique_channels)))
        clusters.append({
            **representative,
            "score": representative["score"] + bonus,
            "members": members,
            "also_in": _related_posts(members, representative),
        })
    return sorted(clusters, key=lambda c: (-c["score"], _tie_key(c["post"])))


def _related_posts(members: list[dict], representative: dict) -> list[dict]:
    primary_channel = str(representative["post"].get("channel_username") or "").casefold()
    best_by_channel: dict[str, dict] = {}
    for member in members:
        channel = str(member["post"].get("channel_username") or "").casefold()
        if channel == primary_channel:
            continue
        current = best_by_channel.get(channel)
        if current is None or (-member["score"], _tie_key(member["post"])) < (-current["score"], _tie_key(current["post"])):
            best_by_channel[channel] = member
    return [member["post"] for member in sorted(best_by_channel.values(), key=lambda c: (-c["score"], _tie_key(c["post"]))) ]


def select_diverse(clusters: list[dict], period: str, max_posts: int = _MAX_POSTS) -> list[dict]:
    if period not in _CAPS:
        raise ValueError("period должен быть day, week или month")
    eligible = [
        cluster for cluster in clusters
        if not cluster["post"].get("is_ad") or (
            cluster.get("ad_strength") is not None and cluster["ad_strength"] >= _AD_MIN_PERCENTILE
        )
    ]
    counts: Counter[str] = Counter()
    selected = []
    by_channel: dict[str, list[dict]] = defaultdict(list)
    for cluster in eligible:
        by_channel[str(cluster["post"].get("channel_username") or "").casefold()].append(cluster)
    heap = []
    channel_positions: Counter[str] = Counter()
    def push_channel(channel: str) -> None:
        channel_clusters = by_channel[channel]
        index = channel_positions[channel]
        if counts[channel] >= _CAPS[period] or index >= len(channel_clusters):
            return
        cluster = channel_clusters[index]
        value = cluster["score"] * (0.85 ** counts[channel])
        timestamp, username, post_id = _tie_key(cluster["post"])
        heappush(heap, (-value, timestamp, username, post_id, channel, index, cluster))
    for channel in by_channel:
        push_channel(channel)
    while len(selected) < max(0, max_posts):
        while heap and counts[heap[0][3]] >= _CAPS[period]:
            heappop(heap)
        if not heap:
            break
        _, _, _, _, channel, index, winner = heappop(heap)
        selected.append(winner)
        counts[channel] += 1
        next_index = index + 1
        channel_positions[channel] = next_index
        push_channel(channel)
    return selected


def _badges(candidate: dict, period: str, now: datetime) -> list[str]:
    badges = []
    for key, label in (("reach_rel", "🔥"), ("fwd_rel", "↗️")):
        value = candidate["signals"].get(key)
        if value is not None and value >= 2:
            badges.append(f"{label} ×{round(value)} к норме" if key == "reach_rel" else f"{label} ×{round(value)} пересылок к норме")
    if period in ("week", "month") and (_utc(now) - _utc(candidate["post"]["time"])).total_seconds() < 24 * 3600:
        badges.append("🆕")
    if candidate["post"].get("is_ad"):
        badges.append("📢 Реклама")
    return badges


def rank_feed(posts: list[dict], medians_by_channel: dict, period: str, now: datetime) -> list[FeedItem]:
    """Ранжирует кандидатов, кластеризует сюжеты и выполняет разнообразный отбор."""
    if not posts:
        return []
    now = _utc(now)
    scored = score_candidates(posts, medians_by_channel, period, now)
    clusters = cluster_candidates(scored)
    selected = select_diverse(clusters, period)
    return [
        FeedItem(
            rank=index,
            score=candidate["score"],
            post=candidate["post"],
            also_in=candidate.get("also_in", []),
            badges=_badges(candidate, period, now),
            channel_title=str(candidate["post"].get("channel_title") or candidate["post"].get("channel_username") or "Канал"),
            tag=candidate["post"].get("tag") or None,
            is_ad=bool(candidate["post"].get("is_ad")),
        )
        for index, candidate in enumerate(selected, 1)
    ]
