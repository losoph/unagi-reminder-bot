import json
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from feed_render import build_feed_title, compact_number, render_feed
from telegraph_publisher import publish_digest, publish_page

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)


def item(rank=1, *, tag=None, channel="alpha", post_id=1, also_in=None, badges=None, text="Первое. Второе предложение", time=None, views=120000, forwards=340, reactions_total=1200, replies=50, is_ad=False):
    return SimpleNamespace(
        rank=rank,
        score=0.8,
        post={
            "post_id": post_id,
            "channel_username": channel,
            "channel_title": channel.title(),
            "tag": tag,
            "text": text,
            "link": f"https://t.me/{channel}/{post_id}",
            "time": time or NOW,
            "views": views,
            "forwards": forwards,
            "reactions_total": reactions_total,
            "replies": replies,
        },
        also_in=also_in or [],
        badges=badges or [],
        channel_title=channel.title(),
        tag=tag,
        is_ad=is_ad,
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

    def test_card_view_h4_is_post_title_not_channel(self):
        # h4 = заголовок поста, а не канал; канал выводится в p с i
        rendered = render_feed([item(text="Заголовок поста. Текст новости дальше")], period="day", now=NOW)
        h4_nodes = [n for n in rendered if n.get("tag") == "h4"]
        self.assertEqual(len(h4_nodes), 1)
        self.assertEqual(h4_nodes[0]["children"], ["Заголовок поста"])
        self.assertNotIn("Alpha", h4_nodes[0]["children"][0])

        # p с i содержит название канала и время
        italic_nodes = [n for n in rendered if n.get("tag") == "p" and isinstance(n.get("children", [None])[0], dict) and n["children"][0].get("tag") == "i"]
        # первый italic - intro строка, второй - плашка карточки
        self.assertGreaterEqual(len(italic_nodes), 2)
        card_meta = italic_nodes[1]["children"][0]["children"][0]
        self.assertIn("Alpha · сегодня, 12:00", card_meta)

        # blockquote содержит остаток текста
        quote_nodes = [n for n in rendered if n.get("tag") == "blockquote"]
        self.assertEqual(len(quote_nodes), 1)
        self.assertEqual(quote_nodes[0]["children"], ["Текст новости дальше"])

    def test_card_nodes_metrics_text_link_and_badges(self):
        rendered = render_feed([item(badges=["🔥 ×3 к норме", "📢 Реклама"])], period="day", now=NOW)
        nodes = repr(rendered)
        self.assertIn("h4", nodes)
        self.assertIn("Первое", nodes)
        self.assertIn("Alpha · сегодня, 12:00", nodes)
        self.assertIn("Второе предложение", nodes)
        self.assertIn("👁 120K · ↗ 340 · ❤ 1,2K · 💬 50", nodes)
        self.assertIn("🔥 в 3 раза больше просмотров, чем обычно", nodes)
        self.assertIn("📢 Реклама", nodes)
        self.assertIn("Читать в канале →", nodes)
        self.assertIn("https://t.me/alpha/1", nodes)

    def test_word_truncation(self):
        # Заголовок длиннее 90 символов без знака обрезается по слову с «…»
        long_phrase = "Очень длинная первая фраза поста без знаков препинания которая точно превышает лимит девяносто символов и должна аккуратно обрезаться по слову"
        t = render_feed([item(text=long_phrase)], period="day", now=NOW)
        h4_text = [n for n in t if n.get("tag") == "h4"][0]["children"][0]
        self.assertLessEqual(len(h4_text), 90)
        self.assertTrue(h4_text.endswith("…"))
        self.assertFalse(h4_text.endswith(" …"))
        # Проверяем, что обрезка произошла по слову
        self.assertTrue(h4_text[:-1] in long_phrase)

        # Выдержка длиннее 320 символов обрезается по слову с «…»
        long_rest = "Короткий заголовок. " + "Слово " * 80
        nodes = render_feed([item(text=long_rest)], period="day", now=NOW)
        b_node = [n for n in nodes if n.get("tag") == "blockquote"][0]
        quote_text = b_node["children"][0]
        self.assertLessEqual(len(quote_text), 320)
        self.assertTrue(quote_text.endswith("…"))

    def test_title_without_dot(self):
        # Точка на конце первого предложения убирается
        t1 = render_feed([item(text="Первое предложение. Второе предложение.")], period="day", now=NOW)
        h4_1 = [n for n in t1 if n.get("tag") == "h4"][0]["children"][0]
        self.assertEqual(h4_1, "Первое предложение")

        # Восклицательный и вопросительный знаки сохраняются
        t2 = render_feed([item(text="Что нового в мире? Подробности в посте.")], period="day", now=NOW)
        h4_2 = [n for n in t2 if n.get("tag") == "h4"][0]["children"][0]
        self.assertEqual(h4_2, "Что нового в мире?")

        t3 = render_feed([item(text="Внимание! Это экстренное сообщение.")], period="day", now=NOW)
        h4_3 = [n for n in t3 if n.get("tag") == "h4"][0]["children"][0]
        self.assertEqual(h4_3, "Внимание!")

        # Многоточие в конце фразы сохраняется
        t4 = render_feed([item(text="Продолжение следует… Ждите новостей.")], period="day", now=NOW)
        h4_4 = [n for n in t4 if n.get("tag") == "h4"][0]["children"][0]
        self.assertEqual(h4_4, "Продолжение следует…")

    def test_block_glavnoe_thresholds(self):
        # Меньше 3 постов — блока «Главное» нет
        nodes_2 = render_feed([item(1, channel="c1"), item(2, channel="c2")], period="day", now=NOW)
        self.assertFalse(any(n.get("tag") == "h3" and n.get("children") == ["Главное"] for n in nodes_2))
        self.assertFalse(any(n.get("tag") == "ol" for n in nodes_2))

        # От 3 постов — блок «Главное» (h3 + ol)
        items_3 = [item(1, channel="c1", text="Новость 1. Описание"),
                   item(2, channel="c2", text="Новость 2. Описание"),
                   item(3, channel="c3", text="Новость 3. Описание")]
        nodes_3 = render_feed(items_3, period="day", now=NOW)
        self.assertTrue(any(n.get("tag") == "h3" and n.get("children") == ["Главное"] for n in nodes_3))
        ol_node = [n for n in nodes_3 if n.get("tag") == "ol"][0]
        self.assertEqual(len(ol_node["children"]), 3)
        # Проверяем структуру li: жирная ссылка на пост и канал с просмотрами
        li0 = ol_node["children"][0]
        self.assertEqual(li0["children"][0]["tag"], "b")
        self.assertEqual(li0["children"][0]["children"][0]["tag"], "a")
        self.assertEqual(li0["children"][0]["children"][0]["children"], ["Новость 1"])
        self.assertEqual(li0["children"][1], " — C1 · 👁 120K")

        # Если постов 7 — в «Главном» ровно 5 первых по рангу
        items_7 = [item(idx, channel=f"c{idx}", text=f"Заголовок {idx}. Описание") for idx in range(1, 8)]
        nodes_7 = render_feed(items_7, period="day", now=NOW)
        ol_node_7 = [n for n in nodes_7 if n.get("tag") == "ol"][0]
        self.assertEqual(len(ol_node_7["children"]), 5)

    def test_today_yesterday_and_older_dates(self):
        today_post = item(1, time=NOW)
        yesterday_post = item(2, time=NOW - timedelta(days=1))
        older_post = item(3, time=NOW - timedelta(days=5))

        nodes = render_feed([today_post, yesterday_post, older_post], period="week", now=NOW)
        rendered_str = repr(nodes)
        self.assertIn("сегодня, 12:00", rendered_str)
        self.assertIn("вчера, 12:00", rendered_str)
        self.assertIn("9 окт, 12:00", rendered_str)

    def test_human_readable_badges(self):
        entry = item(
            badges=[
                "↗️ ×6 пересылок к норме",
                "🔥 ×3 к норме",
                "🆕",
                "📢 Реклама",
            ]
        )
        nodes = render_feed([entry], period="week", now=NOW)
        rendered = repr(nodes)
        self.assertIn("🔥 в 6 раз больше пересылок, чем обычно", rendered)
        self.assertIn("🔥 в 3 раза больше просмотров, чем обычно", rendered)
        self.assertIn("🆕 Свежее", rendered)
        self.assertIn("📢 Реклама", rendered)
        # Старых служебных формулировок быть не должно
        self.assertNotIn("пересылок к норме", rendered)
        self.assertNotIn("×3 к норме", rendered)

    def test_footer_channel_counts(self):
        # 3 поста из bponline, 2 из truestorymedia, 1 из alpha
        items = [
            item(1, channel="bponline"),
            item(2, channel="truestorymedia"),
            item(3, channel="bponline"),
            item(4, channel="alpha"),
            item(5, channel="bponline"),
            item(6, channel="truestorymedia"),
        ]
        nodes = render_feed(items, period="day", now=NOW)
        footer_node = [n for n in nodes if n.get("tag") == "p" and "Каналы в выпуске:" in str(n.get("children", [""]))][0]
        self.assertEqual(footer_node["children"][0], "Каналы в выпуске: bponline (3), truestorymedia (2), alpha (1)")

    def test_tag_sections_order_and_no_continuous_numbering(self):
        items = [item(1, tag="спорт", channel="a", text="Пост А"),
                 item(2, tag=None, channel="b", text="Пост Б"),
                 item(3, tag="Аналитика", channel="c", text="Пост В"),
                 item(4, tag="спорт", channel="d", text="Пост Г")]
        nodes = render_feed(items, period="day", now=NOW)
        # Заголовки h3 — Главное, Без тега, затем теги по алфавиту
        h3_texts = [node["children"][0] for node in nodes if node["tag"] == "h3"]
        self.assertEqual(h3_texts, ["Главное", "Без тега", "#Аналитика · 1", "#спорт · 2"])
        # В h4 — только заголовок поста, без нумерации «1. B»
        h4_texts = [node["children"][0] for node in nodes if node["tag"] == "h4"]
        self.assertEqual(h4_texts, ["Пост Б", "Пост В", "Пост А", "Пост Г"])

        # Между карточками есть hr
        hr_count = sum(1 for n in nodes if n.get("tag") == "hr")
        # 1 hr после вводной строки + 3 hr между 4 карточками = 4 hr
        self.assertEqual(hr_count, 4)

    def test_no_tag_sections_when_no_tags(self):
        nodes = render_feed([item(1, text="Пост 1"), item(2, channel="b", text="Пост 2")], period="day", now=NOW)
        # Теговых разделов h3 нет (и Главного нет, т.к. постов 2 < 3)
        self.assertFalse(any(node["tag"] == "h3" for node in nodes))

    def test_untagged_heading_only_when_other_tag_exists(self):
        nodes = render_feed([item(1, text="Пост 1"), item(2, tag="тема", channel="b", text="Пост 2")], period="day", now=NOW)
        self.assertEqual([node["children"][0] for node in nodes if node["tag"] == "h3"], ["Без тега", "#тема · 1"])

    def test_empty_metrics(self):
        # Пост без просмотров, пересылок и реакций
        empty_item = item(views=None, forwards=None, reactions_total=None, replies=None, text="Пост без метрик.")
        nodes = render_feed([empty_item], period="day", now=NOW)
        rendered = repr(nodes)
        self.assertNotIn("👁 —", rendered)
        self.assertNotIn("↗ —", rendered)
        self.assertNotIn("❤", rendered)
        self.assertNotIn("💬", rendered)
        self.assertIn("Читать в канале →", rendered)

    def test_very_long_words_and_urls(self):
        long_url = "https://example.com/very/long/path/with/lots/of/parameters/and/no/spaces/123456789012345678901234567890123456789012345678901234567890"
        long_word = "W" * 400
        long_item = item(text=f"{long_url}\n{long_word}")
        nodes = render_feed([long_item], period="day", now=NOW)
        h4_text = [n for n in nodes if n.get("tag") == "h4"][0]["children"][0]
        self.assertLessEqual(len(h4_text), 90)
        self.assertTrue(h4_text.endswith("…"))
        b_text = [n for n in nodes if n.get("tag") == "blockquote"][0]["children"][0]
        self.assertLessEqual(len(b_text), 320)
        self.assertTrue(b_text.endswith("…"))

    def test_total_page_size_under_60kb_json(self):
        # 50 постов с максимальной длиной заголовка и выдержки
        items = [
            item(
                index,
                channel=f"channel_{index}",
                post_id=index,
                text="Заголовок новости из русских букв на девяносто символов ровно вот такой длины здесь. " + ("Очень длинный текст выдержки из русской статьи. " * 30),
            )
            for index in range(1, 51)
        ]
        nodes = render_feed(items, period="month", now=NOW, has_web_source=True)
        self.assertEqual(sum(node["tag"] == "h4" for node in nodes), 50)
        raw_json = json.dumps(nodes, ensure_ascii=False).encode("utf-8")
        self.assertLess(len(raw_json), 60 * 1024, f"JSON size {len(raw_json)} bytes exceeds 60 KB limit")

    def test_also_in_links(self):
        rendered = render_feed([item(also_in=[{
            "channel_title": "Канал B", "channel_username": "b", "link": "https://t.me/b/2"
        }])], period="week", now=NOW)
        self.assertIn("также в:", repr(rendered))
        self.assertIn("Канал B", repr(rendered))
        self.assertIn("https://t.me/b/2", repr(rendered))

    def test_also_in_is_unique_per_channel_and_omits_primary(self):
        entry = item(channel="a", also_in=[
            {"channel_username": "a", "channel_title": "Основной", "link": "https://t.me/a/1"},
            {"channel_username": "b", "channel_title": "B", "link": "https://t.me/b/2"},
            {"channel_username": "b", "channel_title": "B2", "link": "https://t.me/b/3"},
        ])
        nodes = render_feed([entry], period="week", now=NOW)
        self.assertEqual(repr(nodes).count("https://t.me/b/"), 1)
        self.assertEqual(repr(nodes).count("https://t.me/a/1"), 1)

    def test_post_count_and_channel_count_header(self):
        nodes = render_feed([item(1), item(2, channel="b")], period="week", now=NOW, channel_count=23)
        self.assertEqual(nodes[0]["children"], [{"tag": "i", "children": ["2 поста · 23 канала · 7–14 окт"]}])

    def test_telegraph_text_is_not_html_escaped_and_counts_are_declined(self):
        entry = item(channel="lab")
        entry.channel_title = "R&D <лаб>"
        entry.tag = "R&D"
        entry.post["channel_title"] = entry.channel_title
        nodes = render_feed([entry], period="day", now=NOW)
        self.assertIn("R&D <лаб>", repr(nodes))
        self.assertNotIn("&amp;", repr(nodes))
        self.assertNotIn("&lt;", repr(nodes))
        self.assertEqual(nodes[0]["children"][0], {"tag": "i", "children": ["1 пост · 1 канал · 14 окт"]})
        self.assertIn("#R&D · 1", repr(nodes))

    def test_timezone_changes_title_window_and_post_time(self):
        late = datetime(2026, 10, 14, 22, 30, tzinfo=timezone.utc)
        entry = item()
        entry.post["time"] = late
        title = build_feed_title("day", late, tz="Europe/Moscow")
        nodes = render_feed([entry], period="day", now=late, tz="Europe/Moscow")
        self.assertEqual(title, "Лента за день · 15.10")
        self.assertIn("Alpha · сегодня, 01:30", repr(nodes))

    def test_russian_plural_forms(self):
        expected = {
            1: "1 пост · 1 канал",
            3: "3 поста · 3 канала",
            21: "21 пост · 21 канал",
            50: "50 постов · 50 каналов",
        }
        for count, prefix in expected.items():
            with self.subTest(count=count):
                nodes = render_feed([item(index, channel=f"c{index}") for index in range(count)],
                                    period="day", now=NOW, channel_count=count)
                intro_text = nodes[0]["children"][0]["children"][0]
                self.assertTrue(intro_text.startswith(prefix))

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
