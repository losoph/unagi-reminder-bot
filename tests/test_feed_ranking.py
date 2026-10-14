import random
import os
import math
import time
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
        # Пять просмотровых уровней дают монотонные перцентили при пропусках всех остальных сигналов.
        posts = [post(i, f"c{i}", views=views, forwards=None, reactions_total=None, replies=None)
                 for i, views in enumerate((100, 200, 300, 400, 500), 1)]
        result = score_candidates(posts, {}, "day", NOW)
        self.assertEqual([entry["percentiles"]["reach_abs"] for entry in result], [0, .25, .5, .75, 1])

    def test_incomplete_signal_quality_is_shrunk_to_neutral(self):
        # При одном кандидате доступны веса .30+.10=.40 из общей суммы .95;
        # Q=(.40/.95)*1+(1-.40/.95)*.5, затем score=.95*Q+.05*fresh.
        posts = [post(1, "only", views=100, forwards=None, reactions_total=1, replies=None)]
        result = score_candidates(posts, {}, "day", NOW)
        coverage = (.30 + .10) / (.30 + .25 + .15 + .15 + .10)
        quality = coverage * 1 + (1 - coverage) * .5
        self.assertAlmostEqual(result[0]["score"], .95 * quality + .05 * result[0]["signals"]["fresh"])

    def test_external_url_skips_telegram_and_canonicalizes(self):
        # Первая внешняя ссылка нормализуется к https, без www и хвостового слэша.
        posts = [post(1, "a", text="via https://t.me/x/1 потом http://www.Example.com/news/"),
                 post(2, "b", text="https://example.com/news")]
        self.assertEqual(len(cluster_candidates(score_candidates(posts, {}, "day", NOW))), 1)

    def test_short_text_does_not_cluster_but_long_text_at_36h_does(self):
        # «Доброе утро!» → 11 символов после нормализации; тексты >60 на границе 36 ч сливаются.
        short = [post(1, "a", text="Доброе утро!"), post(2, "b", text="Доброе утро!")]
        self.assertEqual(len(cluster_candidates(score_candidates(short, {}, "day", NOW))), 2)
        text = "важное сообщение " * 5
        long = [post(3, "a", text=text, time=NOW), post(4, "b", text=text, time=NOW-timedelta(hours=36))]
        self.assertEqual(len(cluster_candidates(score_candidates(long, {}, "day", NOW))), 1)

    def test_cluster_also_in_excludes_primary_and_deduplicates_channel(self):
        # От представителя канал не показывается; два поста одного канала схлопываются до лучшего.
        posts = [post(1, "a", text="https://site.test/x", views=100),
                 post(2, "b", text="https://site.test/x", views=20),
                 post(3, "b", text="https://site.test/x", views=10),
                 post(4, "c", text="https://site.test/x", views=30)]
        cluster = cluster_candidates(score_candidates(posts, {}, "day", NOW))[0]
        related_channels = [p["channel_username"] for p in cluster["also_in"]]
        self.assertEqual(len(related_channels), len(set(related_channels)))
        self.assertNotIn(cluster["post"]["channel_username"], related_channels)

    def test_single_ad_candidate_is_not_admitted_without_percentile_pool(self):
        # Один кандидат не задаёт верхнюю четверть; рекламный пост исключается при пуле меньше четырёх.
        self.assertEqual(rank_feed([post(1, "ad", text="Реклама erid")], {}, "day", NOW), [])

    def test_week_synthetic_top_three_and_source_cap(self):
        # Перцентили заданы просмотрами внутри выпуска: после top крупного/среднего третий large обходит small;
        # потолок недели — 10, крупный не забирает всю выдачу.
        posts = []
        for channel, views in (("large", 100_000), ("middle", 10_000), ("small", 1_000)):
            for index in range(12):
                posts.append(post(index + 1, channel, views=views-index, forwards=20-index,
                                  reactions_total=None, replies=None,
                                  text=f"сюжет {channel} {index}", time=NOW-timedelta(hours=index+1)))
        items = rank_feed(posts, {}, "week", NOW)
        self.assertEqual([item.post["channel_username"] for item in items[:3]], ["large", "middle", "large"])
        from collections import Counter
        counts = Counter(item.post["channel_username"] for item in items)
        self.assertLessEqual(counts["large"], 10)
        self.assertLess(counts["large"], len(items))

    def test_tied_percentiles_use_average_rank(self):
        # [1,2,2,4]: ничья занимает ранги 2 и 3, средний ранг 2.5, P=(2.5−1)/3=.5.
        posts = [post(i, str(i), views=value, forwards=None, reactions_total=None, replies=None)
                 for i, value in enumerate((1, 2, 2, 4), 1)]
        result = score_candidates(posts, {}, "day", NOW)
        self.assertEqual(result[1]["percentiles"]["reach_abs"], .5)
        self.assertEqual(result[2]["percentiles"]["reach_abs"], .5)

    def test_single_channel_cluster_has_no_bonus(self):
        # Уникальных каналов один, поэтому 0.05*log2(1)=0.
        posts = [post(1, "same", text="https://site.test/x"), post(2, "same", text="https://site.test/x")]
        scored = score_candidates(posts, {}, "day", NOW)
        cluster = cluster_candidates(scored)[0]
        self.assertEqual(cluster["score"], max(candidate["score"] for candidate in scored))

    def test_monotonicity_with_missing_signals(self):
        # Набор из пяти постов с пропуском просмотров проверяет неубывание среди измеримых reach_abs.
        posts = [post(i, f"c{i}", views=views, forwards=None, reactions_total=None, replies=None)
                 for i, views in enumerate((10, None, 30, 40, 50), 1)]
        result = score_candidates(posts, {}, "day", NOW)
        values = [candidate["percentiles"]["reach_abs"] for candidate in result if "reach_abs" in candidate["percentiles"]]
        self.assertEqual(values, sorted(values))

    def test_week_and_month_channel_caps(self):
        # Лимиты канала из формулы: 10 за неделю и 20 за месяц.
        clusters = [candidate(post(i, "one", text=f"уникальный {i}"), 1 - i / 100) for i in range(25)]
        self.assertEqual(len(select_diverse(clusters, "week", 50)), 10)
        self.assertEqual(len(select_diverse(clusters, "month", 50)), 20)

    @unittest.skipUnless(os.getenv("FEED_PERF_TEST") == "1", "замер включается только вручную")
    def test_30000_candidates_performance(self):
        # Полный синтетический конвейер, 100 каналов и уникальный текст для запрета схлопывания.
        posts = [post(i, f"channel{i % 100}", views=(i % 1000) + 1, forwards=i % 100,
                      text=f"уникальный сюжет номер {i} с разными подробностями") for i in range(30_000)]
        started = time.perf_counter()
        rank_feed(posts, {}, "month", NOW)
        elapsed = time.perf_counter() - started
        print(f"FEED_PERF_TEST rank_feed(30000): {elapsed:.3f}s")
        self.assertLessEqual(elapsed, 10)

    @unittest.skipUnless(os.getenv("FEED_PERF_TEST") == "1", "замер включается только вручную")
    def test_300_similar_texts_performance(self):
        # 300 почти одинаковых копий пресс-релиза во всех каналах должны объединиться менее чем за секунду.
        text = "Важное сообщение пресс-релиз о развитии компании и новых проектах " * 8
        posts = [post(i, f"channel{i}", text=text, time=NOW - timedelta(minutes=i))
                 for i in range(300)]
        started = time.perf_counter()
        clusters = cluster_candidates(score_candidates(posts, {}, "month", NOW))
        elapsed = time.perf_counter() - started
        print(f"FEED_PERF_TEST cluster_candidates(300 similar): {elapsed:.3f}s")
        self.assertEqual(len(clusters), 1)
        self.assertLess(elapsed, 1)

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
        # Идентичные метрики имеют одинаковые перцентили; свежесть поднимает только более новый пост.
        fresh, old = post(1, "a", time=NOW - timedelta(hours=1)), post(2, "b", time=NOW - timedelta(hours=12))
        result = score_candidates([old, fresh], {}, "day", NOW)
        self.assertGreater(result[1]["score"], result[0]["score"])

        # При разных перцентилях старший по сигналам пост остаётся выше, несмотря на меньшую свежесть.
        weak_fresh = post(3, "c", views=1, forwards=0, time=NOW - timedelta(hours=1))
        strong_old = post(4, "d", views=100000, forwards=100, time=NOW - timedelta(hours=12))
        result = score_candidates([weak_fresh, strong_old], {}, "day", NOW)
        self.assertGreater(result[1]["score"], result[0]["score"])

    def test_weak_erid_ad_is_excluded(self):
        # Ad percentiles .0/.0 < .75; it remains in the percentile pool but fails selection.
        posts = [post(1, "ad", text="Реклама erid", views=1, forwards=0), post(2, "b"), post(3, "c"), post(4, "d")]
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
        self.assertAlmostEqual(clusters[0]["score"], max(x["score"] for x in scored) + 0.05 * math.log2(3))

    def test_forwarded_post_uses_original_as_representative_despite_higher_score(self):
        # Связь пересылки указывает первоисточник; его score=.4 остаётся представителем против .9 у пересылки.
        original = candidate(post(1, "source", channel_id=10, text="короткий текст"), .4)
        forwarded = candidate(post(2, "forward", channel_id=20, text="короткий текст",
                                   fwd_from_channel_id=10, fwd_from_post_id=1), .9)
        cluster = cluster_candidates([forwarded, original])[0]
        self.assertEqual(cluster["post"]["channel_username"], "source")
        self.assertEqual(cluster["score"], .4 + .05 * math.log2(2))

    def test_reach_relative_badge_threshold(self):
        # views_m=100/(1-exp(-12/8)) и median=views_m/2 дают reach_rel=2, значит 🔥 ×2 к норме.
        now = NOW
        age_h = 12
        views_m = 100 / (1 - math.exp(-age_h / 8))
        median = views_m / 2
        item = rank_feed([post(1, "reach", views=100, forwards=None, reactions_total=None,
                               replies=None, time=now - timedelta(hours=age_h))],
                         {"reach": {"median_views": median}}, "day", now)[0]
        self.assertEqual(item.badges, ["🔥 ×2 к норме"])

    def test_forward_relative_badge_threshold(self):
        # (forwards/views)/median_fwd= (2/100)/.01=2, значит ↗️ ×2 пересылок к норме.
        item = rank_feed([post(1, "forward", views=100, forwards=2, reactions_total=None,
                               replies=None)],
                         {"forward": {"median_fwd_rate": .01}}, "day", NOW)[0]
        self.assertEqual(item.badges, ["↗️ ×2 пересылок к норме"])

    def test_new_badge_only_for_week_and_month_posts_under_24h(self):
        # Возраст 23 ч <24 ч: 🆕 положен неделе/месяцу; дневному выпуску свежесть не добавляет значок.
        fresh_post = post(1, "fresh", time=NOW - timedelta(hours=23))
        for period in ("week", "month"):
            with self.subTest(period=period):
                self.assertEqual(rank_feed([fresh_post], {}, period, NOW)[0].badges, ["🆕"])
        self.assertEqual(rank_feed([fresh_post], {}, "day", NOW)[0].badges, [])

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
