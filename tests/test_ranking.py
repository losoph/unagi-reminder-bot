import unittest
from datetime import datetime, timezone

from ranking import (
    MAX_POSTS_PER_CHANNEL,
    prioritize_channel_posts,
    score_post,
    should_prioritize_posts,
)


class RankingAndPrioritizationTests(unittest.TestCase):
    def test_volume_rule_threshold(self):
        now = datetime(2026, 7, 24, 12, 0, tzinfo=timezone.utc)
        # Channel with 5 posts (< 20/day) -> all 5 included, omitted = 0
        few_posts = [
            {"id": i, "time": now, "text": f"Post {i}", "link": f"https://t.me/ex/{i}"}
            for i in range(1, 6)
        ]
        self.assertFalse(should_prioritize_posts(few_posts))
        selected, omitted = prioritize_channel_posts("example", few_posts, now=now)
        self.assertEqual(len(selected), 5)
        self.assertEqual(omitted, 0)

        # Channel with 50 posts for a day (>= 20/day) -> top MAX_POSTS_PER_CHANNEL and omitted = 50 - N
        many_posts = [
            {
                "id": i,
                "time": now,
                "text": f"Post {i}",
                "link": f"https://t.me/ex/{i}",
                "views": 100 + i,
                "forwards": 1,
            }
            for i in range(1, 51)
        ]
        self.assertTrue(should_prioritize_posts(many_posts))
        selected_many, omitted_many = prioritize_channel_posts("example", many_posts, now=now)
        self.assertEqual(len(selected_many), MAX_POSTS_PER_CHANNEL)
        self.assertEqual(omitted_many, 50 - MAX_POSTS_PER_CHANNEL)

    def test_forward_standout_rule_and_cap(self):
        now = datetime(2026, 7, 24, 12, 0, tzinfo=timezone.utc)
        medians = {
            "median_views": 1000.0,
            "median_fwd_rate": 0.02,  # 2%
            "median_reaction_rate": 0.05,
        }

        # Build 30 posts. Give the first 10 posts high views (so they have highest base score).
        # Give post 15 a fwd_rate of 3% (30 forwards / 1000 views -> rel_fwd = 1.5 >= 1.25),
        # but very few views (say 100 views, 3 forwards) so its base score is low (outside top 5).
        posts = []
        for i in range(1, 31):
            if i <= 10:
                posts.append(
                    {
                        "id": i,
                        "time": now,
                        "text": f"Top post {i}",
                        "link": f"https://t.me/ex/{i}",
                        "views": 5000,
                        "forwards": 10,  # 10/5000 = 0.002 (low fwd_rate)
                        "reactions_total": 200,
                    }
                )
            elif i == 15:
                # Standout post: 3% forwards rate -> rel_fwd = 1.5
                posts.append(
                    {
                        "id": i,
                        "time": now,
                        "text": "Standout post",
                        "link": f"https://t.me/ex/{i}",
                        "views": 100,
                        "forwards": 3,  # 3/100 = 0.03 -> rel_fwd = 0.03 / 0.02 = 1.5
                        "reactions_total": 1,
                    }
                )
            else:
                posts.append(
                    {
                        "id": i,
                        "time": now,
                        "text": f"Normal post {i}",
                        "link": f"https://t.me/ex/{i}",
                        "views": 200,
                        "forwards": 1,
                        "reactions_total": 2,
                    }
                )

        selected, omitted = prioritize_channel_posts("example", posts, medians=medians, max_posts=5, now=now)
        # Post 15 must be included despite being outside top-5 by general score!
        selected_ids = [p["id"] for p in selected]
        self.assertIn(15, selected_ids)
        self.assertEqual(len(selected), 6)  # 5 base + 1 standout
        self.assertEqual(omitted, 30 - 6)

        # Now test cap: 5 standout posts with rel_fwd >= 1.25
        # At most FORWARD_STANDOUT_MAX_EXTRA (3) should be added extra.
        standout_posts = list(posts)
        for idx, post_id in enumerate([20, 21, 22, 23, 24]):
            # Different fwd rates above 1.25: 3.5%, 3.4%, 3.3%, 3.2%, 3.1%
            fwd_rate = 0.03 + (5 - idx) * 0.001
            standout_posts[post_id - 1] = {
                "id": post_id,
                "time": now,
                "text": f"Standout extra {post_id}",
                "link": f"https://t.me/ex/{post_id}",
                "views": 1000,
                "forwards": int(1000 * fwd_rate),
                "reactions_total": 1,
            }
        selected_cap, _ = prioritize_channel_posts(
            "example",
            standout_posts,
            medians=medians,
            max_posts=5,
            max_extra_standout=3,
            now=now,
        )
        # Exactly 5 base + 3 extra = 8 posts
        self.assertEqual(len(selected_cap), 8)

    def test_reactions_degradation(self):
        now = datetime(2026, 7, 24, 12, 0, tzinfo=timezone.utc)
        medians = {
            "median_views": 1000.0,
            "median_fwd_rate": 0.02,
            "median_reaction_rate": None,  # Disabled on channel
        }
        post_disabled = {
            "id": 1,
            "time": now,
            "text": "Post with disabled reactions",
            "link": "https://t.me/ex/1",
            "views": 1500,
            "forwards": 30,
            "reactions_total": None,  # Disabled
        }
        score, rel_fwd = score_post(post_disabled, medians=medians, now=now)
        self.assertGreater(score, 0.0)
        self.assertIsNotNone(rel_fwd)

        post_low = {
            "id": 2,
            "time": now,
            "text": "Post with low engagement",
            "link": "https://t.me/ex/2",
            "views": 500,
            "forwards": 2,
            "reactions_total": None,
        }
        score_low, _ = score_post(post_low, medians=medians, now=now)
        self.assertGreater(score, score_low)

    def test_cold_start_without_medians(self):
        now = datetime(2026, 7, 24, 12, 0, tzinfo=timezone.utc)
        # Empty medians (cold start)
        post = {
            "id": 1,
            "time": now,
            "text": "Cold start post",
            "link": "https://t.me/ex/1",
            "views": 500,
            "forwards": 10,
            "reactions_total": 20,
        }
        score, rel_fwd = score_post(post, medians={}, now=now)
        self.assertGreater(score, 0.0)
        # Under cold start, rel_fwd is None so standout rule is safely skipped
        self.assertIsNone(rel_fwd)

    def test_rendering_tail_links(self):
        from email_digest import build_digest_email
        from main import append_digest_channel_lines
        from telegraph_publisher import build_content

        sections = [
            {
                "sub_id": 1,
                "period": "daily",
                "title": "Active Channel",
                "username": "active_channel",
                "posts": [{"id": 1, "text": "Post 1", "link": "https://t.me/active_channel/1"}],
                "omitted_count": 45,
            }
        ]

        # 1. Telegram preview lines
        lines = []
        append_digest_channel_lines(
            lines,
            1,
            "daily",
            "Active Channel",
            sections[0]["posts"],
            omitted_count=45,
            channel_username="active_channel",
        )
        rendered_tg = "\n".join(lines)
        self.assertIn("ещё 45 постов в канале", rendered_tg)
        self.assertIn("https://t.me/active_channel", rendered_tg)

        # 2. Telegraph content nodes
        nodes = build_content(sections)
        nodes_str = str(nodes)
        self.assertIn("ещё 45 постов в канале", nodes_str)
        self.assertIn("https://t.me/active_channel", nodes_str)

        # 3. Email plain text and HTML
        html_body, text_body = build_digest_email("Digest", sections)
        self.assertIn("ещё 45 постов", text_body)
        self.assertIn("https://t.me/active_channel", text_body)
        self.assertIn("ещё 45 постов в канале", html_body)


if __name__ == "__main__":
    unittest.main()
