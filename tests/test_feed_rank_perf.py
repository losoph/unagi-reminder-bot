"""RP1: эквивалентность, скорость и вынос ранжирования Ленты из цикла событий."""

import difflib
import math
import os
import random
import tempfile
import threading
import time
import unittest
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("BOT_TOKEN", "123456:TEST_TOKEN_FOR_UNIT_TESTS")

import feed  # noqa: E402
import feed_ranking as fast  # noqa: E402
from data import database  # noqa: E402

NOW = datetime(2026, 10, 14, 12, tzinfo=timezone.utc)


def _reference_cluster_candidates(candidates: list[dict]) -> list[dict]:
    """Дословная копия старой (медленной) cluster_candidates — эталон для сравнения."""
    if len(candidates) < 2:
        return [dict(candidate, members=[candidate]) for candidate in candidates]
    candidates = sorted(candidates, key=lambda candidate: fast._tie_key(candidate["post"]))
    uf = fast._UnionFind(len(candidates))
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
        url = fast._canonical_url(str(post.get("text") or ""))
        if url:
            if ("url", url) in keyed:
                uf.union(index, keyed[("url", url)])
            else:
                keyed[("url", url)] = index
    # Текстовая кластеризация ограничена топ-300; прямой источник и URL остаются без этого лимита.
    text_candidates = sorted(candidates, key=lambda candidate: (-candidate["score"], fast._tie_key(candidate["post"])))[:300]
    normalized = [fast._normalized_text(str(candidate["post"].get("text") or "")) for candidate in text_candidates]
    candidate_positions = {id(candidate): index for index, candidate in enumerate(candidates)}
    eligible_text = [(i, text_candidates[i], text) for i, text in enumerate(normalized)
                     if len(text) >= fast._CLUSTER_MIN_TEXT]
    for left_pos, (_, left_candidate, left_text) in enumerate(eligible_text):
        left_time = fast._utc(left_candidate["post"]["time"])
        left_index = candidate_positions[id(left_candidate)]
        matcher = difflib.SequenceMatcher(None, left_text, "")
        for _, right_candidate, right_text in eligible_text[left_pos + 1:]:
            right_index = candidate_positions[id(right_candidate)]
            if uf.find(left_index) == uf.find(right_index):
                continue
            if abs((left_time - fast._utc(right_candidate["post"]["time"])).total_seconds()) > 36 * 3600:
                continue
            matcher.set_seq2(right_text)
            if matcher.quick_ratio() >= 0.6 and matcher.ratio() >= 0.6:
                uf.union(left_index, right_index)
    groups: dict[int, list[dict]] = defaultdict(list)
    positions = {id(candidate): i for i, candidate in enumerate(candidates)}
    for candidate in candidates:
        groups[uf.find(positions[id(candidate)])].append(candidate)
    clusters = []
    for members in groups.values():
        members.sort(key=lambda c: (-c["score"], fast._tie_key(c["post"])))
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
        representative = min(originals or members, key=lambda c: (-c["score"], fast._tie_key(c["post"])))
        unique_channels = len({str(member["post"].get("channel_username") or "").casefold() for member in members})
        bonus = min(0.15, 0.05 * math.log2(max(1, unique_channels)))
        clusters.append({
            **representative,
            "score": representative["score"] + bonus,
            "members": members,
            "also_in": fast._related_posts(members, representative),
        })
    return sorted(clusters, key=lambda c: (-c["score"], fast._tie_key(c["post"])))


_ALPHABET = "абвгдежзийклмнопрстуфхцчшщыьэюя"


def _random_text(rng: random.Random, words: int = 40) -> str:
    return " ".join("".join(rng.choice(_ALPHABET) for _ in range(rng.randint(3, 9))) for _ in range(words))


def _post(post_id: int, channel: str, text: str, time: datetime | None = None, **kwargs) -> dict:
    result = {
        "post_id": post_id,
        "channel_username": channel,
        "channel_title": channel.title(),
        "tag": None,
        "text": text,
        "link": f"https://t.me/{channel}/{post_id}",
        "time": time or (NOW - timedelta(hours=1)),
        "views": 1000,
        "forwards": 10,
        "reactions_total": 5,
        "replies": 1,
    }
    result.update(kwargs)
    return result


def _candidate(post: dict, score: float) -> dict:
    return {"post": post, "score": score, "signals": {}, "percentiles": {}, "ad_strength": None}


def _cluster_signature(clusters: list[dict]) -> list[tuple]:
    return [
        (
            cluster["post"]["channel_username"],
            cluster["post"]["post_id"],
            round(cluster["score"], 12),
            tuple(sorted((member["post"]["channel_username"], member["post"]["post_id"]) for member in cluster["members"])),
            tuple((related["channel_username"], related["post_id"]) for related in cluster["also_in"]),
        )
        for cluster in clusters
    ]


class ClusterEquivalenceTests(unittest.TestCase):
    def test_reference_is_equivalent_on_random_unrelated_texts(self):
        rng = random.Random(20261014)
        candidates = []
        for index in range(120):
            text = _random_text(rng)
            candidates.append(_candidate(
                _post(index + 1, f"channel{index % 7}", text, NOW - timedelta(minutes=index * 13)),
                round(rng.random(), 6),
            ))
        self.assertEqual(
            _cluster_signature(fast.cluster_candidates(candidates)),
            _cluster_signature(_reference_cluster_candidates(candidates)),
        )

    def test_reference_is_equivalent_on_near_duplicates(self):
        rng = random.Random(42)
        candidates = []
        post_id = 0
        for _ in range(40):
            base = _random_text(rng)
            variants = [base]
            # Добавленный хвост (репост с подписью).
            variants.append(base + " подробности читайте в нашем канале по ссылке")
            # Заменённые/удалённые слова.
            words = base.split()
            changed = list(words)
            for _ in range(2):
                position = rng.randrange(len(changed))
                changed[position] = "".join(rng.choice(_ALPHABET) for _ in range(len(changed[position])))
            variants.append(" ".join(changed))
            # Точечные замены символов (перепост с правками).
            chars = list(base)
            for position in rng.sample(range(len(chars)), max(1, len(chars) // 10)):
                chars[position] = rng.choice(_ALPHABET)
            variants.append("".join(chars))
            # Обрезка хвоста.
            variants.append(base[: max(70, len(base) - 25)])
            for text in variants:
                post_id += 1
                candidates.append(_candidate(
                    _post(post_id, f"near{post_id % 9}", text, NOW - timedelta(minutes=post_id)),
                    round(1.0 - post_id / 100000.0, 6),
                ))
        self.assertEqual(
            _cluster_signature(fast.cluster_candidates(candidates)),
            _cluster_signature(_reference_cluster_candidates(candidates)),
        )

    def test_reference_is_equivalent_with_forwards_links_and_window(self):
        base = _random_text(random.Random(7))
        candidates = [
            _candidate(_post(1, "source", base, NOW - timedelta(hours=35, minutes=59), channel_id=11), 0.4),
            _candidate(_post(2, "copy", base, NOW - timedelta(hours=1), channel_id=22,
                            fwd_from_channel_id=11, fwd_from_post_id=1), 0.9),
            _candidate(_post(3, "link_a", "Новость https://Example.com/story/", NOW - timedelta(hours=2)), 0.7),
            _candidate(_post(4, "link_b", "Пересказ http://www.example.com/story", NOW - timedelta(hours=3)), 0.6),
            _candidate(_post(5, "old", base, NOW - timedelta(hours=40), channel_id=55), 0.5),
            _candidate(_post(6, "repost", base, NOW - timedelta(hours=37), channel_id=66), 0.3),
        ]
        rng = random.Random(99)
        for index in range(40):
            candidates.append(_candidate(
                _post(100 + index, f"mix{index % 5}", _random_text(rng), NOW - timedelta(minutes=index * 7)),
                round(rng.random(), 6),
            ))
        self.assertEqual(
            _cluster_signature(fast.cluster_candidates(candidates)),
            _cluster_signature(_reference_cluster_candidates(candidates)),
        )

    def test_rank_feed_matches_reference_pipeline_on_synthetic_posts(self):
        rng = random.Random(1234)
        posts = []
        post_id = 0
        for _ in range(30):
            base = _random_text(rng, words=25)
            for text in (base, base + " источник", base):
                post_id += 1
                posts.append(_post(post_id, f"s{post_id % 6}", text, NOW - timedelta(minutes=post_id * 11)))
        scored = fast.score_candidates(posts, {}, "day", NOW)
        expected = _cluster_signature(_reference_cluster_candidates(scored))
        actual = _cluster_signature(fast.cluster_candidates(scored))
        self.assertEqual(actual, expected)


class ClusterPerformanceTests(unittest.TestCase):
    def test_2500_diverse_candidates_under_three_seconds(self):
        rng = random.Random(2026)
        posts = []
        for index in range(2500):
            text = _random_text(rng, words=45) + f" выпуск {index}"
            posts.append(_post(index + 1, f"chan{index % 120}", text, NOW - timedelta(minutes=index % 2000)))
        started = time.perf_counter()
        result = fast.rank_feed(posts, {}, "month", NOW)
        elapsed = time.perf_counter() - started
        self.assertGreater(len(result), 0)
        self.assertLessEqual(elapsed, 3.0)

    def test_2500_near_duplicate_candidates_under_three_seconds(self):
        rng = random.Random(777)
        posts = []
        post_id = 0
        while len(posts) < 2500:
            base = _random_text(rng, words=40)
            for _ in range(5):
                post_id += 1
                text = base
                if post_id % 2 == 0:
                    text = base + " " + _random_text(rng, words=2)
                posts.append(_post(post_id, f"dup{post_id % 200}", text, NOW - timedelta(minutes=post_id % 2000)))
        started = time.perf_counter()
        fast.rank_feed(posts, {}, "day", NOW)
        elapsed = time.perf_counter() - started
        self.assertLessEqual(elapsed, 3.0)


class RankFeedToThreadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = os.path.join(self.temp_dir.name, "test_feed_rank_perf.db")
        database.init_db()
        feed._personal_channels_cache.clear()
        feed._backfill_tasks.clear()
        feed._user_build_locks.clear()
        feed._building_users.clear()
        feed._last_build_time.clear()

    async def asyncTearDown(self):
        await feed.cancel_backfill_tasks()
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()
        feed._personal_channels_cache.clear()
        feed._backfill_tasks.clear()
        feed._user_build_locks.clear()
        feed._building_users.clear()
        feed._last_build_time.clear()

    async def test_rank_feed_is_dispatched_to_worker_thread(self):
        user_id = 9101
        now = datetime.now(timezone.utc)
        database.add_feed_subscription(user_id, "perf_chan", "Perf Chan")
        database.upsert_channel_posts("perf_chan", [{
            "id": 1,
            "time": now - timedelta(hours=1),
            "text": "Текст поста для проверки выноса ранжирования в отдельный поток.",
            "link": "https://t.me/perf_chan/1",
            "views": 100,
            "forwards": 1,
            "reactions_total": 1,
            "replies": 0,
        }], "mtproto")

        mock_source = MagicMock()
        mock_source.fetch = AsyncMock(return_value=([], "mtproto"))
        mock_source.refresh_metrics = AsyncMock(return_value=0)
        feed.configure(channel_source=mock_source)

        seen: dict[str, int] = {}
        main_ident = threading.get_ident()

        def fake_rank(posts, medians_by_channel, period, now_dt):
            seen["thread"] = threading.get_ident()
            return []

        with patch("feed.rank_feed", side_effect=fake_rank) as mock_rank, \
             patch("feed.publish_page", new_callable=AsyncMock):
            result = await feed.build_feed_edition(user_id, "day", ignore_rate_limit=True, now=now)

        self.assertTrue(mock_rank.called)
        self.assertEqual(result.status, "empty_posts")
        self.assertIn("thread", seen)
        self.assertNotEqual(seen["thread"], main_ident)


if __name__ == "__main__":
    unittest.main()
