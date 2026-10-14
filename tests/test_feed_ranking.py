import random
import unittest
from datetime import datetime, timedelta, timezone

from feed_ranking import (
    cluster_candidates,
    mark_ads,
    rank_feed,
    score_candidates,
    select_diverse,
)

NOW = datetime(2026, 10, 14, 12, tzinfo=timezone.utc)


def post(post_id, channel="alpha", **kwargs):
    return {
        "post_id": post_id,
        "channel_username": channel,
        "channel_title": channel.title(),
        "tag": None,
        "text": f"Текст поста {post_id}",
        "link": f"https://t.me/{channel}/{post_id}",
        "time": NOW - timedelta(hours=12),
        "views": 100,
        "reactions_total": 1,
        "forwards": 2,
        "replies": 1,
        "metrics_at": NOW,
        "source": "mtproto",
        **kwargs,
    }


def candidate(item, score, signals=None, percentiles=None, ad_strength=None):
    return {
        "post": item,
        "score": score,
        "signals": signals or {},
        "percentiles": percentiles or {},
        "ad_strength": ad_strength,
    }


class FeedRankingTests(unittest.TestCase):
    def test_more_views_do_not_lower_score(self):
        # Одинаковый возраст: 100→reach_abs=2.31, 1000→3.31; при том же ранге остальных сигналов score выше.
        posts = [post(1, "a", views=100, forwards=2), post(2, "b", views=1000, forwards=2)]
        result = score_candidates(posts, {}, "day", NOW)
        self.assertGreater(result[1]["score"], result[0]["score"])

    def test_more_forwards_do_not_lower_score(self):
        # 1 и 10 пересылок дают fwd_abs=0.30 и 1.04; остальные сигналы одинаковы.
        result = score_candidates([post(1, "a", forwards=1), post(2, "b", forwards=10)], {}, "day", NOW)
        self.assertGreater(result[1]["score"], result[0]["score"])

    def test_forward_signals_outweigh_tenfold_engagement(self):
        # 10x forwards меняет два перцентиля fwd_abs/fwd_rel; 10x engagement меняет один 0.10-весовой сигнал.
        posts = [
            post(1, "a", views=1000, forwards=1, reactions_total=1, replies=0),
            post(2, "b", views=1000, forwards=10, reactions_total=1, replies=0),
            post(3, "c", views=1000, forwards=1, reactions_total=10, replies=0),
        ]
        medians = {name: {"median_fwd_rate": 0.001, "median_views": 1000} for name in ("a", "b", "c")}
        result = score_candidates(posts, medians, "day", NOW)
        self.assertGreater(result[1]["score"], result[2]["score"])

    def test_freshness_only_breaks_identical_post_tie(self):
        # Идентичные метрики имеют одинаковые перцентили; 1-часовой пост имеет fresh=e^-1/12 > e^-12/12.
        fresh, old = post(1, "a", time=NOW - timedelta(hours=1)), post(2, "b", time=NOW - timedelta(hours=12))
        result = score_candidates([old, fresh], {}, "day", NOW)
        self.assertGreater(result[1]["score"], result[0]["score"])

    def test_weak_erid_ad_is_excluded(self):
        # Ad percentiles .0/.0 < .75; it remains in the percentile pool but fails selection.
        posts = [post(1, "ad", text="Реклама erid", views=1, forwards=0), post(2, "b"), post(3, "c")]
        result = rank_feed(posts, {}, "day", NOW)
        self.assertNotIn(1, [item.post["post_id"] for item in result])

    def test_top_quartile_ad_is_included_and_badged(self):
        # 4 candidates: ad is unique maximum reach and forwards, ranks 1.0 on both (mean 1.0 ≥ .75).
        posts = [post(1, "ad", text="Реклама #ad", views=10000, forwards=100),
                 post(2, "b", views=10, forwards=0), post(3, "c", views=20, forwards=1),
                 post(4, "d", views=30, forwards=2)]
        result = rank_feed(posts, {}, "day", NOW)
        item = next(item for item in result if item.post["post_id"] == 1)
        self.assertTrue(item.is_ad)
        self.assertIn("📢 Реклама", item.badges)

    def test_channel_cap_and_global_limit(self):
        # 60 candidates, equal scores: greedy tie-break fills lexical channels, but never >5/day/channel or >50.
        posts = [post(i, f"c{i % 3}", text=f"различный пост {i} номер {i}") for i in range(60)]
        result = rank_feed(posts, {}, "day", NOW)
        from collections import Counter

        counts = Counter(item.post["channel_username"] for item in result)
        self.assertLessEqual(len(result), 50)
        self.assertTrue(all(count <= 5 for count in counts.values()))

    def test_three_channel_story_forms_one_cluster_with_bonus(self):
        # Three unique sources: bonus=min(.15,.05*log2(3))=0.079248; exactly one primary + two also_in.
        posts = [post(1, "a", text="Общая ссылка https://news.test/story", channel_id=11),
                 post(2, "b", text="Другая https://news.test/story", channel_id=22),
                 post(3, "c", text="Третья https://news.test/story", channel_id=33)]
        scored = score_candidates(posts, {}, "day", NOW)
        clusters = cluster_candidates(scored)
        self.assertEqual(len(clusters), 1)
        self.assertEqual(len(clusters[0]["also_in"]), 2)
        self.assertAlmostEqual(clusters[0]["score"], max(x["score"] for x in scored) + 0.05 * __import__("math").log2(3))

    def test_missing_signals_and_medians_degrade_gracefully(self):
        # views=0/null, medians=None/0 yield only fwd_abs; all-None metrics yield quality=0 and finite freshness.
        items = [post(1, "a", views=0, forwards=2), post(2, "b", views=None, forwards=None,
                                                       reactions_total=None, replies=None)]
        result = score_candidates(items, {"a": {"median_views": 0, "median_fwd_rate": None}}, "day", NOW)
        self.assertTrue(all(0 <= item["score"] <= 1 and item["score"] == item["score"] for item in result))

    def test_result_is_deterministic_for_shuffled_input(self):
        # Explicit tie-break is time desc, username asc, id asc; shuffled source order must not alter output.
        posts = [post(i, f"c{i % 3}", text=f"уникально {i}") for i in range(1, 12)]
        expected = [(i.post["channel_username"], i.post["post_id"]) for i in rank_feed(posts, {}, "day", NOW)]
        random.Random(42).shuffle(posts)
        actual = [(i.post["channel_username"], i.post["post_id"]) for i in rank_feed(posts, {}, "day", NOW)]
        self.assertEqual(actual, expected)

    def test_empty_singleton_and_none_views_forward_median_cases(self):
        # Empty -> []; singleton has P=1 (quality=1 when signals exist); None/0 medians disable only relative signals.
        self.assertEqual(rank_feed([], {}, "day", NOW), [])
        one = score_candidates([post(1)], {}, "day", NOW)[0]
        self.assertEqual(set(one["percentiles"].values()), {1.0})
        result = score_candidates([post(2, views=None, forwards=None, reactions_total=None, replies=None)],
                                  {"alpha": {"median_views": 0, "median_fwd_rate": None}}, "day", NOW)
        self.assertEqual(result[0]["percentiles"], {})

    def test_tie_break_for_equal_scores(self):
        # Equal score and time resolves by channel ascending then post_id ascending.
        posts = [post(2, "b", text="уникальный B"), post(1, "a", text="уникальный A")]
        scored = [candidate(posts[0], 0.5), candidate(posts[1], 0.5)]
        selected = select_diverse(scored, "day")
        self.assertEqual(selected[0]["post"]["channel_username"], "a")

    def test_transitive_clusters_merge(self):
        # A~B by canonical URL, B~C by forwarded origin: union-find makes A/B/C one component.
        items = [post(1, "a", text="A https://site.test/a"),
                 post(2, "b", text="B https://site.test/a", channel_id=22, fwd_from_channel_id=11,
                      fwd_from_post_id=1),
                 post(3, "c", text="C без ссылки", channel_id=33, fwd_from_channel_id=22,
                      fwd_from_post_id=2)]
        clusters = cluster_candidates(score_candidates(items, {}, "day", NOW))
        self.assertEqual(len(clusters), 1)
        self.assertEqual(len(clusters[0]["also_in"]), 2)

    def test_also_in_do_not_consume_selection_limits(self):
        # 1 cluster is 3 source posts but costs exactly one slot and one channel-cap entry.
        posts = [post(1, "a", text="https://site.test/x"),
                 post(2, "b", text="https://site.test/x"),
                 post(3, "c", text="https://site.test/x")]
        result = rank_feed(posts, {}, "day", NOW)
        self.assertEqual(len(result), 1)
        self.assertEqual(len(result[0].also_in), 2)

    def test_ad_threshold_boundary_is_inclusive(self):
        # Exactly .75 strength is allowed (comparison is >=), while .749 is rejected.
        post_data = post(1, "ad", text="erid marker")
        allowed = candidate(post_data, .5, ad_strength=.75)
        rejected = candidate(post(2, "ad2", text="#ad"), .6, ad_strength=.749)
        rejected["post"]["is_ad"] = True
        selected = select_diverse([allowed, rejected], "day")
        self.assertEqual([item["post"]["post_id"] for item in selected], [1])

    def test_ad_marking_patterns(self):
        # Each marker is case-insensitive; unrelated substrings do not trigger the word-boundary rules.
        texts = ["erid", " РЕКЛАМА канал", "#реклама", "#ad", "Партнёрский материал", "на правах рекламы"]
        marked = mark_ads([post(i, text=text) for i, text in enumerate(texts, 1)])
        self.assertTrue(all(item["is_ad"] for item in marked))
        self.assertFalse(mark_ads([post(99, text="thread #adapter")])[0]["is_ad"])


if __name__ == "__main__":
    unittest.main()
