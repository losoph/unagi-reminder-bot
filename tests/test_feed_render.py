import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from feed_render import build_feed_title, compact_number, render_feed
from telegraph_publisher import publish_digest, publish_page

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)


def item(rank=1, *, tag=None, channel="alpha", post_id=1, also_in=None, badges=None, text="Первое. Второе предложение"):
    return SimpleNamespace(
        rank=rank,
        score=.8,
        post={
            "post_id": post_id,
            "channel_username": channel,
            "channel_title": channel.title(),
            "tag": tag,
            "text": text,
            "link": f"https://t.me/{channel}/{post_id}",
            "time": NOW,
            "views": 120000,
            "forwards": 340,
            "reactions_total": 1200,
            "replies": 50,
        },
        also_in=also_in or [],
        badges=badges or [],
        channel_title=channel.title(),
        tag=tag,
        is_ad=False,
    )


class FeedRenderTests(unittest.TestCase):
    def test_title_period_range_and_validation(self):
        self.assertEqual(build_feed_title("day", NOW), "Лента за день · 14.10")
        self.assertEqual(build_feed_title("week", NOW), "Лента за неделю · 07.10–14.10")
        self.assertEqual(build_feed_title("month", NOW), "Лента за месяц · 14.09–14.10")
        with self.assertRaises(ValueError):
            build_feed_title("year", NOW)

    def test_compact_number_formats(self):
        self.assertEqual(compact_number(999), "999")
        self.assertEqual(compact_number(1200), "1,2K")
        self.assertEqual(compact_number(12000), "12K")
        self.assertEqual(compact_number(1200000), "1,2M")
        self.assertEqual(compact_number(None), "—")

    def test_card_nodes_metrics_text_link_and_badges(self):
        rendered = render_feed([item(badges=["🔥 ×3 к норме", "📢 Реклама"])], period="day", now=NOW)
        nodes = repr(rendered)
        self.assertIn("h4", nodes)
        self.assertIn("1. Alpha", nodes)
        self.assertIn("👁 120K · ↗️ 340 · ❤️ 1,2K · 💬 50", nodes)
        self.assertIn("🔥 ×3 к норме", nodes)
        self.assertIn("📢 Реклама", nodes)
        self.assertIn("Первое.", nodes)
        self.assertIn("Второе предложение", nodes)
        self.assertIn("https://t.me/alpha/1", nodes)

    def test_also_in_links(self):
        rendered = render_feed([item(also_in=[{
            "channel_title": "Канал B", "channel_username": "b", "link": "https://t.me/b/2"
        }])], period="week", now=NOW)
        self.assertIn("также в:", repr(rendered))
        self.assertIn("Канал B", repr(rendered))
        self.assertIn("https://t.me/b/2", repr(rendered))

    def test_tag_sections_order_and_continuous_numbering(self):
        # Untagged precedes alphabetized casefold tags; rank order within a section is retained.
        items = [item(1, tag="спорт", channel="a"), item(2, tag=None, channel="b"),
                 item(3, tag="Аналитика", channel="c"), item(4, tag="спорт", channel="d")]
        nodes = render_feed(items, period="day", now=NOW)
        texts = [node.get("children", [""])[0] for node in nodes if node["tag"] in ("h3", "h4")]
        self.assertEqual(texts, ["Без тега", "1. B", "#Аналитика · 1", "2. C", "#спорт · 2", "3. A", "4. D"])

    def test_no_tag_sections_when_no_tags(self):
        nodes = render_feed([item(1), item(2, channel="b")], period="day", now=NOW)
        self.assertFalse(any(node["tag"] == "h3" for node in nodes))

    def test_untagged_heading_only_when_other_tag_exists(self):
        nodes = render_feed([item(1), item(2, tag="тема", channel="b")], period="day", now=NOW)
        self.assertEqual([node["children"][0] for node in nodes if node["tag"] == "h3"], ["Без тега", "#тема · 1"])

    def test_long_content_and_web_notice(self):
        items = [item(index, channel=f"c{index}", post_id=index, text="Очень длинный текст. " * 300)
                 for index in range(1, 51)]
        nodes = render_feed(items, period="month", now=NOW, has_web_source=True)
        self.assertEqual(sum(node["tag"] == "h4" for node in nodes), 50)
        text_nodes = [node for node in nodes if node["tag"] == "p" and isinstance(node.get("children", [None])[0], dict)
                      and node["children"][0].get("tag") == "b"]
        self.assertTrue(all(len("".join(child if isinstance(child, str) else "".join(child.get("children", []))
                                        for child in node["children"])) <= 600 for node in text_nodes))
        self.assertIn("ℹ️ Метрики части каналов неполные", repr(nodes))

    def test_post_count_and_channel_count_header(self):
        nodes = render_feed([item(1), item(2, channel="b")], period="week", now=NOW, channel_count=23)
        self.assertEqual(nodes[0]["children"], ["2 постов из 23 каналов · отбор по охвату и пересылкам · окно 07.10–14.10"])

    def test_publication_shared_and_digest_behavior_uses_nodes(self):
        response = SimpleNamespace(json=AsyncMock(return_value={"ok": True, "result": {"url": "https://telegra.ph/x"}}))

        class FakeResponse:
            async def __aenter__(self):
                return response

            async def __aexit__(self, *args):
                return None

        class FakeSession:
            def __init__(self):
                self.payloads = []

            def post(self, url, data):
                self.payloads.append((url, data))
                return FakeResponse()

        async def get_token(session):
            return "token"

        fake_session = FakeSession()
        nodes = [{"tag": "p", "children": ["feed"]}]
        with patch("telegraph_publisher._get_token", side_effect=get_token):
            self.assertEqual(__import__("asyncio").run(publish_page("Feed", nodes, session=fake_session)), "https://telegra.ph/x")
            self.assertEqual(__import__("asyncio").run(publish_digest("Digest", [], session=fake_session)), "https://telegra.ph/x")
        self.assertIn('"feed"', fake_session.payloads[0][1]["content"])
        self.assertIn('"children": []', fake_session.payloads[1][1]["content"])


if __name__ == "__main__":
    unittest.main()
