import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from feed_render import build_feed_title, compact_number, render_feed
from telegraph_publisher import publish_digest, publish_page

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)


def item(
    rank=1,
    *,
    tag=None,
    channel="alpha",
    channel_title=None,
    post_id=1,
    also_in=None,
    badges=None,
    text="Первое предложение новости длинное и осмысленное. Второе предложение сути новости. Третье предложение с деталями.",
    time=None,
    views=120000,
    forwards=340,
    reactions_total=1200,
    replies=50,
    is_ad=False,
    link=None,
):
    c_title = channel_title if channel_title is not None else channel.title()
    post_link = f"https://t.me/{channel}/{post_id}" if link is None else link
    return SimpleNamespace(
        rank=rank,
        score=0.8,
        post={
            "post_id": post_id,
            "channel_username": channel,
            "channel_title": c_title,
            "tag": tag,
            "text": text,
            "link": post_link,
            "time": time or NOW,
            "views": views,
            "forwards": forwards,
            "reactions_total": reactions_total,
            "replies": replies,
        },
        also_in=also_in or [],
        badges=badges or [],
        channel_title=c_title,
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
        rendered = render_feed(
            [item(text="Заголовок поста важный и понятный. Текст новости дальше подробно раскрывает суть события.")],
            period="day",
            now=NOW,
        )
        h4_nodes = [n for n in rendered if n.get("tag") == "h4"]
        self.assertEqual(len(h4_nodes), 1)
        self.assertEqual(h4_nodes[0]["children"], ["Заголовок поста важный и понятный"])
        self.assertNotIn("Alpha", h4_nodes[0]["children"][0])

        # Подпись: p с i содержит название канала и время
        p_nodes = [n for n in rendered if n.get("tag") == "p"]
        # первый p - intro, второй - суть (p), третий - мета (p с i)
        meta_p = p_nodes[2]
        self.assertEqual(meta_p["children"][0]["tag"], "i")
        self.assertIn("Alpha · сегодня, 12:00", meta_p["children"][0]["children"][0])

        # Суть: обычный p, не blockquote
        self.assertFalse(any(n.get("tag") == "blockquote" for n in rendered))
        essence_p = p_nodes[1]
        self.assertIn("Текст новости дальше подробно раскрывает суть события.", essence_p["children"][0])

    def test_card_nodes_metrics_text_link_and_badges(self):
        rendered = render_feed([item(badges=["🔥 ×3 к норме", "📢 Реклама"])], period="day", now=NOW)
        nodes = repr(rendered)
        self.assertIn("h4", nodes)
        self.assertIn("Alpha · сегодня, 12:00", nodes)
        self.assertIn("👁 120K · ↗ 340 · ❤ 1,2K · 💬 50", nodes)
        self.assertIn("🔥 в 3 раза больше просмотров, чем обычно", nodes)
        self.assertIn("📢 реклама", nodes)
        self.assertIn("Читать →", nodes)
        self.assertIn("https://t.me/alpha/1", nodes)

    def test_word_truncation(self):
        long_phrase = "Очень длинная первая фраза поста без знаков препинания которая точно превышает лимит сто сорок символов и должна аккуратно обрезаться по границе слова с многоточием"
        t = render_feed([item(text=long_phrase)], period="day", now=NOW)
        h4_text = [n for n in t if n.get("tag") == "h4"][0]["children"][0]
        self.assertLessEqual(len(h4_text), 140)
        self.assertTrue(h4_text.endswith("…"))
        self.assertFalse(h4_text.endswith(" …"))
        self.assertTrue(h4_text[:-1] in long_phrase)

        long_rest = "Короткий заголовок здесь новости. " + ("Слово " * 80)
        nodes = render_feed([item(text=long_rest)], period="day", now=NOW)
        p_nodes = [n for n in nodes if n.get("tag") == "p"]
        essence_text = p_nodes[1]["children"][0]
        self.assertLessEqual(len(essence_text), 300)
        self.assertTrue(essence_text.endswith("…"))

    def test_title_without_dot(self):
        t1 = render_feed(
            [item(text="Первое предложение без точки в конце. Второе предложение с подробностями новости для теста.")],
            period="day",
            now=NOW,
        )
        h4_1 = [n for n in t1 if n.get("tag") == "h4"][0]["children"][0]
        self.assertEqual(h4_1, "Первое предложение без точки в конце")

        t2 = render_feed(
            [item(text="Что нового в мире технологий? Подробности в посте читайте далее в полном тексте новости.")],
            period="day",
            now=NOW,
        )
        h4_2 = [n for n in t2 if n.get("tag") == "h4"][0]["children"][0]
        self.assertEqual(h4_2, "Что нового в мире технологий?")

        t3 = render_feed(
            [item(text="Внимание! Это экстренное сообщение о происшествии в городе для проверки отображения заголовка.")],
            period="day",
            now=NOW,
        )
        h4_3 = [n for n in t3 if n.get("tag") == "h4"][0]["children"][0]
        self.assertEqual(h4_3, "Внимание!")

        t4 = render_feed(
            [item(text="Продолжение следует… Ждите новостей в следующем выпуске нашего регулярного дайджеста событий.")],
            period="day",
            now=NOW,
        )
        h4_4 = [n for n in t4 if n.get("tag") == "h4"][0]["children"][0]
        self.assertEqual(h4_4, "Продолжение следует…")

    def test_block_glavnoe_thresholds(self):
        # Для дня блока «Главное» нет никогда
        items_7_day = [
            item(idx, channel=f"c{idx}", text=f"Заголовок важной новости {idx}. Описание {idx} детально расписано в полном тексте новости для проверки.")
            for idx in range(1, 8)
        ]
        nodes_7_day = render_feed(items_7_day, period="day", now=NOW)
        self.assertFalse(any(n.get("tag") == "h3" and n.get("children") == ["Главное"] for n in nodes_7_day))

        # Для недели: меньше 7 полных карточек — блока «Главное» нет
        items_6_week = [
            item(idx, channel=f"c{idx}", text=f"Заголовок важной новости {idx}. Описание {idx} детально расписано в полном тексте новости для проверки.")
            for idx in range(1, 7)
        ]
        nodes_6_week = render_feed(items_6_week, period="week", now=NOW)
        self.assertFalse(any(n.get("tag") == "h3" and n.get("children") == ["Главное"] for n in nodes_6_week))

        # Для недели: от 7 полных карточек — блок «Главное» как оглавление из 5 строк «заголовок — канал» без сути
        nodes_7_week = render_feed(items_7_day, period="week", now=NOW)
        glavnoe_h3 = [n for n in nodes_7_week if n.get("tag") == "h3" and n.get("children") == ["Главное"]]
        self.assertEqual(len(glavnoe_h3), 1)
        ul_week = [nodes_7_week[i + 1] for i, n in enumerate(nodes_7_week) if n == glavnoe_h3[0]][0]
        self.assertEqual(len(ul_week["children"]), 5)

        # Формат пункта: <b>заголовок</b> — канал (без сути)
        li0 = ul_week["children"][0]
        self.assertEqual(li0["children"][0]["tag"], "b")
        self.assertEqual(li0["children"][0]["children"], ["Заголовок важной новости 1"])
        self.assertEqual(li0["children"][1], " — C1")

    def test_today_yesterday_and_older_dates(self):
        today_post = item(1, channel="c1", time=NOW)
        yesterday_post = item(2, channel="c2", time=NOW - timedelta(days=1))
        # 12 Oct 2026 is Monday (пн) at 08:10 UTC
        monday_post = item(3, channel="c3", time=datetime(2026, 10, 12, 8, 10, tzinfo=timezone.utc))

        nodes = render_feed([today_post, yesterday_post, monday_post], period="week", now=NOW)
        rendered_str = repr(nodes)
        self.assertIn("сегодня, 12:00", rendered_str)
        self.assertIn("вчера, 12:00", rendered_str)
        self.assertIn("пн 12.10, 08:10", rendered_str)

    def test_human_readable_badges(self):
        entry = item(
            badges=[
                "↗️ ×6 пересылок к норме",
                "🔥 ×3 к норме",
                "🆕",
                "📢 Реклама",
                "📌 писали 4 канала",
            ]
        )
        nodes = render_feed([entry], period="week", now=NOW)
        rendered = repr(nodes)
        self.assertIn("🔥 в 6 раз больше пересылок, чем обычно", rendered)
        self.assertIn("🔥 в 3 раза больше просмотров, чем обычно", rendered)
        self.assertIn("🆕 новость", rendered)
        self.assertIn("📢 реклама", rendered)
        self.assertIn("📌 писали 4 канала", rendered)
        self.assertNotIn("пересылок к норме", rendered)
        self.assertNotIn("×3 к норме", rendered)

    def test_footer_channel_counts(self):
        # 11 каналов: максимум 10 названий, без «+N», число постов в выпуске
        items = [
            item(1, channel="bponline", channel_title="bponline"),
            item(2, channel="truestorymedia", channel_title="truestorymedia"),
            item(3, channel="bponline", channel_title="bponline"),
            item(4, channel="c1", channel_title="c1"),
            item(5, channel="c2", channel_title="c2"),
            item(6, channel="c3", channel_title="c3"),
            item(7, channel="c4", channel_title="c4"),
            item(8, channel="c5", channel_title="c5"),
            item(9, channel="c6", channel_title="c6"),
            item(10, channel="c7", channel_title="c7"),
            item(11, channel="c8", channel_title="c8"),
        ]
        nodes = render_feed(items, period="day", now=NOW)
        footer_node = [n for n in nodes if n.get("tag") == "p" and "Каналы в выпуске:" in str(n.get("children", [""]))][0]
        text = footer_node["children"][0]
        self.assertTrue(text.startswith("Каналы в выпуске: bponline (2)"))
        self.assertNotIn("+", text)
        names = text.replace("Каналы в выпуске: ", "").split(", ")
        self.assertLessEqual(len(names), 10)

    def test_tag_sections_order_and_no_continuous_numbering(self):
        items = [
            item(1, tag="спорт", channel="a", text="Пост А длинный текст для формирования заголовка новости и проверки отображения тегов."),
            item(2, tag=None, channel="b", text="Пост Б длинный текст для формирования заголовка новости и проверки отображения тегов."),
            item(3, tag="Аналитика", channel="c", text="Пост В длинный текст для формирования заголовка новости и проверки отображения тегов."),
            item(4, tag="спорт", channel="d", text="Пост Г длинный текст для формирования заголовка новости и проверки отображения тегов."),
        ]
        nodes = render_feed(items, period="day", now=NOW)
        h3_texts = [node["children"][0] for node in nodes if node["tag"] == "h3"]
        self.assertEqual(h3_texts, ["Без тега", "#Аналитика · 1", "#спорт · 2"])
        h4_texts = [node["children"][0] for node in nodes if node["tag"] == "h4"]
        self.assertEqual(h4_texts, [
            "Пост Б длинный текст для формирования заголовка новости и проверки отображения тегов",
            "Пост В длинный текст для формирования заголовка новости и проверки отображения тегов",
            "Пост А длинный текст для формирования заголовка новости и проверки отображения тегов",
            "Пост Г длинный текст для формирования заголовка новости и проверки отображения тегов",
        ])

    def test_no_tag_sections_when_no_tags(self):
        nodes = render_feed([
            item(1, text="Пост 1 длинный текст для формирования заголовка новости и подробной проверки сюжета выпуска."),
            item(2, channel="b", text="Пост 2 длинный текст для формирования заголовка новости и подробной проверки сюжета выпуска."),
        ], period="day", now=NOW)
        h3_texts = [node["children"][0] for node in nodes if node["tag"] == "h3"]
        self.assertEqual(h3_texts, ["Сюжеты"])

    def test_untagged_heading_only_when_other_tag_exists(self):
        nodes = render_feed([
            item(1, text="Пост 1 длинный текст для формирования заголовка новости и подробной проверки сюжета выпуска."),
            item(2, tag="тема", channel="b", text="Пост 2 длинный текст для формирования заголовка новости и подробной проверки сюжета выпуска."),
        ], period="day", now=NOW)
        self.assertEqual([node["children"][0] for node in nodes if node["tag"] == "h3"], ["Без тега", "#тема · 1"])

    def test_empty_metrics(self):
        empty_item = item(views=None, forwards=None, reactions_total=None, replies=None, text="Пост без метрик, но с достаточным развернутым текстом более восьмидесяти символов для проверки сюжета.")
        nodes = render_feed([empty_item], period="day", now=NOW)
        rendered = repr(nodes)
        self.assertNotIn("👁", rendered)
        self.assertNotIn("↗", rendered)
        self.assertNotIn("❤", rendered)
        self.assertNotIn("💬", rendered)
        self.assertIn("Читать →", rendered)

    def test_empty_text_and_h4_never_equals_channel_name(self):
        # Текст < 25 символов без ссылки отсекается полностью
        empty_post = item(channel="Alpha", text="")
        nodes = render_feed([empty_post], period="day", now=NOW)
        self.assertFalse(any(n.get("tag") == "h4" for n in nodes))

        # Пост >= 80 символов, совпадающий с именем канала, получает заголовок «Без названия»
        channel_name_post = item(channel="Alpha", text="Alpha. " + "Очень длинный текст подробностей новости более восьмидесяти символов для проверки карточки.")
        nodes2 = render_feed([channel_name_post], period="day", now=NOW)
        h4_node = [n for n in nodes2 if n.get("tag") == "h4"][0]
        self.assertEqual(h4_node["children"][0], "Без названия")

    def test_very_long_words_and_urls(self):
        long_url = "https://example.com/" + "a" * 150
        long_word = "W" * 400
        long_item = item(text=f"{long_url}\n{long_word}")
        nodes = render_feed([long_item], period="day", now=NOW)
        h4_text = [n for n in nodes if n.get("tag") == "h4"][0]["children"][0]
        self.assertLessEqual(len(h4_text), 140)
        self.assertTrue(h4_text.endswith("…"))
        p_nodes = [n for n in nodes if n.get("tag") == "p"]
        essence_text = p_nodes[1]["children"][0]
        self.assertLessEqual(len(essence_text), 300)
        self.assertTrue(essence_text.endswith("…"))

    def test_total_page_size_under_60kb_json(self):
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
        raw_json = json.dumps(nodes, ensure_ascii=False).encode("utf-8")
        self.assertLess(len(raw_json), 60 * 1024, f"JSON size {len(raw_json)} bytes exceeds 60 KB limit")

    def test_also_in_links(self):
        rendered = render_feed([item(also_in=[{
            "channel_title": "Канал B", "channel_username": "b", "link": "https://t.me/b/2"
        }])], period="week", now=NOW)
        self.assertIn("также: ", repr(rendered))
        self.assertIn("Канал B", repr(rendered))
        self.assertIn("https://t.me/b/2", repr(rendered))

    def test_also_in_is_unique_per_channel_and_omits_primary_and_caps_at_2(self):
        entry = item(channel="a", also_in=[
            {"channel_username": "a", "channel_title": "Основной", "link": "https://t.me/a/1"},
            {"channel_username": "b", "channel_title": "B", "link": "https://t.me/b/2"},
            {"channel_username": "b", "channel_title": "B-dup", "link": "https://t.me/b/3"},
            {"channel_username": "c", "channel_title": "C", "link": "https://t.me/c/1"},
            {"channel_username": "d", "channel_title": "D", "link": "https://t.me/d/1"},
            {"channel_username": "e", "channel_title": "E", "link": "https://t.me/e/1"},
        ])
        nodes = render_feed([entry], period="week", now=NOW)
        rendered = repr(nodes)
        self.assertEqual(rendered.count("https://t.me/b/"), 1)
        self.assertNotIn("Основной", rendered)
        self.assertIn("B", rendered)
        self.assertIn("C", rendered)
        self.assertNotIn("https://t.me/d/1", rendered)
        self.assertIn("(+2)", rendered)

    def test_post_count_and_channel_count_header(self):
        nodes = render_feed([item(1), item(2, channel="b")], period="week", now=NOW, channel_count=23)
        self.assertEqual(nodes[0]["children"], [{"tag": "i", "children": ["Лента за неделю · 7–14 окт — 2 поста из 23 каналов · ≈1 мин чтения"]}])

    def test_telegraph_text_is_not_html_escaped_and_counts_are_declined(self):
        entry = item(channel="lab")
        entry.channel_title = "R&D <лаб>"
        entry.tag = "R&D"
        entry.post["channel_title"] = entry.channel_title
        nodes = render_feed([entry], period="day", now=NOW)
        self.assertIn("R&D <лаб>", repr(nodes))
        self.assertNotIn("&amp;", repr(nodes))
        self.assertNotIn("&lt;", repr(nodes))
        intro_text = nodes[0]["children"][0]["children"][0]
        self.assertTrue(intro_text.startswith("Лента за день · 14 окт — 1 пост из 1 канала"))
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
            1: "Лента за день · 14 окт — 1 пост из 1 канала",
            3: "Лента за день · 14 окт — 3 поста из 3 каналов",
            21: "Лента за день · 14 окт — 21 пост из 21 канала",
            50: "Лента за день · 14 окт — 50 постов из 50 каналов",
        }
        for count, prefix in expected.items():
            with self.subTest(count=count):
                nodes = render_feed([item(index, channel=f"c{index}") for index in range(count)],
                                    period="day", now=NOW, channel_count=count)
                intro_text = nodes[0]["children"][0]["children"][0]
                self.assertTrue(intro_text.startswith(prefix))

    def test_max_two_full_cards_per_channel(self):
        # 5 постов из одного канала c1
        items = [
            item(idx, channel="c1", post_id=idx, text=f"Заголовок {idx} важной новости. Подробное описание новости {idx} для проверки карточки сюжета.")
            for idx in range(1, 6)
        ]
        nodes = render_feed(items, period="day", now=NOW)
        # В Сюжетах (полных карточках) ровно 2 карточки
        h4_nodes = [n for n in nodes if n.get("tag") == "h4"]
        self.assertEqual(len(h4_nodes), 2)

        # Остальные 3 поста ушли в «Коротко»
        self.assertTrue(any(n.get("tag") == "h3" and n.get("children") == ["Коротко"] for n in nodes))
        korotko_ul = [nodes[i+1] for i, n in enumerate(nodes) if n.get("tag") == "h3" and n.get("children") == ["Коротко"]][0]
        self.assertEqual(len(korotko_ul["children"]), 3)

    def test_korotko_filtering_empty_and_no_link(self):
        items = [
            # 2 полных карточки (>= 80 симв)
            item(1, channel="c1", text="Новость раз. Развернутый текст новости номер один для полноценной проверки карточки сюжета."),
            item(2, channel="c1", text="Новость два. Развернутый текст новости номер два для полноценной проверки карточки сюжета."),
            # Кандидаты в Коротко:
            # 1: валидный (между 25 и 80 симв, есть ссылка)
            item(3, channel="c1", text="Новость три с текстом сорока символов."),
            # 2: короткий (<25 симв, без ссылки в тексте) -> не показывается нигде
            item(4, channel="c1", text="Коротко совсем."),
            # 3: без ссылки поста -> не включается
            item(5, channel="c1", text="Новость пять длинный текст для проверки, но без ссылки на сообщение в канале.", link=""),
            # 4: <25 симв, но со ссылкой в тексте -> включается в Коротко
            item(6, channel="c1", text="См: https://t.me/c1/6"),
        ]
        nodes = render_feed(items, period="day", now=NOW)
        korotko_ul = [nodes[i+1] for i, n in enumerate(nodes) if n.get("tag") == "h3" and n.get("children") == ["Коротко"]][0]
        # Пункты 3 и 6 включены в Коротко
        self.assertEqual(len(korotko_ul["children"]), 2)
        li = korotko_ul["children"][0]
        self.assertEqual(li["children"][0]["tag"], "a")
        self.assertEqual(li["children"][0]["children"], ["Новость три с текстом сорока символов."])
        self.assertIn("— C1 · 👁 120K", li["children"][1])

    def test_hr_divider_only_after_first_three_cards(self):
        items = [
            item(idx, channel=f"ch_{idx}", text=f"Заголовок {idx}. Описание новости {idx} полное и развернутое для проверки карточки сюжета.")
            for idx in range(1, 7)
        ]
        nodes = render_feed(items, period="day", now=NOW)
        # Между 6 карточками hr ровно 3 (после 1, 2, 3 карточек)
        hr_count = sum(1 for n in nodes if n.get("tag") == "hr")
        self.assertEqual(hr_count, 3)

    def test_reading_time_calculation(self):
        # 15 постов с текстом, дающих суммарно > 450 слов на странице -> ≈3 мин чтения
        items = [
            item(idx, channel=f"ch_{idx}", text=f"Заголовок {idx}. " + ("слово " * 30))
            for idx in range(1, 16)
        ]
        nodes = render_feed(items, period="day", now=NOW)
        intro_text = nodes[0]["children"][0]["children"][0]
        self.assertIn("мин чтения", intro_text)
        self.assertTrue("≈2 мин чтения" in intro_text or "≈3 мин чтения" in intro_text)

    def test_structure_day_week_month_limits(self):
        items = [
            item(idx, channel=f"ch_{idx}", text=f"Заголовок {idx}. Описание {idx} детально расписано в тексте карточки для обязательной проверки условий задачи.")
            for idx in range(1, 15)
        ]
        def get_card_h4(nodes):
            res = []
            for n in nodes:
                if n.get("tag") == "h3" and n.get("children") == ["Коротко"]:
                    break
                if n.get("tag") == "h4":
                    res.append(n)
            return res

        # День: max 6 полных карточек
        nodes_day = render_feed(items, period="day", now=NOW)
        self.assertEqual(len(get_card_h4(nodes_day)), 6)

        # Неделя: max 8 полных карточек
        nodes_week = render_feed(items, period="week", now=NOW)
        self.assertEqual(len(get_card_h4(nodes_week)), 8)

        # Месяц: max 10 полных карточек
        nodes_month = render_feed(items, period="month", now=NOW)
        self.assertEqual(len(get_card_h4(nodes_month)), 10)

    def test_sample_file_12_posts(self):
        sample_path = os.path.join(os.path.dirname(__file__), "..", "docs", "sprint", "lenta-v3-sample.txt")
        with open(sample_path, "r", encoding="utf-8") as f:
            raw_sample = json.load(f)

        self.assertEqual(len(raw_sample), 12)
        feed_items = []
        for idx, entry in enumerate(raw_sample, 1):
            feed_items.append(
                item(
                    rank=idx,
                    tag=entry.get("tag"),
                    channel=entry["channel_username"],
                    channel_title=entry.get("channel_title"),
                    post_id=entry["post_id"],
                    also_in=entry.get("also_in", []),
                    badges=entry.get("badges", []),
                    text=entry["text"],
                    time=datetime.fromisoformat(entry["time"]),
                    views=entry.get("views"),
                    forwards=entry.get("forwards"),
                    reactions_total=entry.get("reactions_total"),
                    replies=entry.get("replies"),
                )
            )

        now = datetime(2026, 10, 16, 15, 0, tzinfo=timezone.utc)
        nodes = render_feed(feed_items, period="day", now=now)

        # Проверяем структуру для дня:
        # 1. Интро
        intro = nodes[0]["children"][0]["children"][0]
        self.assertTrue(intro.startswith("Лента за день · 16 окт — 12 постов из 7 каналов"))
        self.assertIn("мин чтения", intro)

        # 2. Для дня блока Главное нет
        self.assertFalse(any(n.get("tag") == "h3" and n.get("children") == ["Главное"] for n in nodes))

        # 3. Полные карточки: для дня max 6 постов, не больше 2 на канал
        h4_nodes = [n for n in nodes if n.get("tag") == "h4"]
        self.assertEqual(len(h4_nodes), 6)

        # 4. Коротко: оставшиеся 6 постов
        korotko_h3 = [n for n in nodes if n.get("tag") == "h3" and n.get("children") == ["Коротко"]]
        self.assertEqual(len(korotko_h3), 1)
        ul_korotko = [nodes[i+1] for i, n in enumerate(nodes) if n == korotko_h3[0]][0]
        self.assertEqual(len(ul_korotko["children"]), 6)

        # 5. Подвал со списком каналов
        footer = [n for n in nodes if n.get("tag") == "p" and "Каналы в выпуске:" in str(n.get("children", [""]))][0]
        self.assertIn("TechNews (3)", footer["children"][0])

    def test_caps_title_normalization(self):
        # КАПС-заголовок приводится к обычному регистру, аббревиатуры <= 5 букв и ИИ сохраняются
        covid_text = (
            "🦠КОВИД «УБИВАЕТ РАДОСТЬ» У ЛЮДЕЙ — ВИРУС БУКВАЛЬНО ВЫЖИГАЕТ ДОФАМИНОВЫЕ РЕЦЕПТОРЫ!\n"
            "Учёные обнаружили у переболевших COVID-19 снижение плотности дофаминовых нервных окончаний."
        )
        nodes = render_feed([item(text=covid_text)], period="day", now=NOW)
        h4_text = [n for n in nodes if n.get("tag") == "h4"][0]["children"][0]
        self.assertEqual(h4_text, "🦠КОВИД «убивает радость» у людей — вирус буквально выжигает дофаминовые рецепторы!")

        ai_text = "ПРОМТ, КОТОРЫЙ СОЗДАЕТ КРУТОЙ ОФФЕР В ЛЮБОЙ ИИ. Подробная инструкция для маркетологов и дизайнеров."
        nodes2 = render_feed([item(text=ai_text)], period="day", now=NOW)
        h4_text2 = [n for n in nodes2 if n.get("tag") == "h4"][0]["children"][0]
        self.assertEqual(h4_text2, "Промт, который создает крутой оффер в любой ИИ")

    def test_emoji_chains_collapsing(self):
        # Цепочки эмодзи >=3 подряд схлопываются до одного
        text = "🔴🔴🔴🔴🔴🔴🔴🔴🔴🔴 ▶️ Учёные сделали важное открытие в области биологии и медицины. Подробности исследования далее."
        nodes = render_feed([item(text=text)], period="day", now=NOW)
        h4_text = [n for n in nodes if n.get("tag") == "h4"][0]["children"][0]
        self.assertIn("Учёные сделали важное открытие", h4_text)
        self.assertNotIn("🔴🔴", repr(nodes))

    def test_service_tails_removal(self):
        # Служебные хвосты каналов убираются из сути
        text = (
            "Важная новость о развитии технологий и искусственного интеллекта в мире.\n\n"
            "Подробности читайте в полном отчете аналитиков за текущий квартал года.\n\n"
            "@bugfeature | #chatgpt\n#новости #ai"
        )
        nodes = render_feed([item(text=text)], period="day", now=NOW)
        rendered = repr(nodes)
        self.assertNotIn("@bugfeature", rendered)
        self.assertNotIn("#chatgpt", rendered)
        self.assertNotIn("#новости", rendered)

    def test_title_exact_140_and_141_chars(self):
        # 140 символов: первое предложение целиком становится заголовком
        prefix_140 = ("Слово " * 23) + "да"
        self.assertEqual(len(prefix_140), 140)
        text_140 = f"{prefix_140}. Второе предложение сути новости подробно описывает все детали произошедшего события."
        nodes_140 = render_feed([item(text=text_140)], period="day", now=NOW)
        h4_140 = [n for n in nodes_140 if n.get("tag") == "h4"][0]["children"][0]
        self.assertEqual(len(h4_140), 140)
        self.assertEqual(h4_140, prefix_140)
        # Суть — второе предложение
        essence_140 = [n for n in nodes_140 if n.get("tag") == "p"][1]["children"][0]
        self.assertTrue(essence_140.startswith("Второе предложение сути новости"))

        # 141 символ без разделителя: обрезка по слову с «…», суть = полный текст с начала
        words_141 = ("Слово " * 23) + "нет"
        self.assertEqual(len(words_141), 141)
        text_141 = f"{words_141}. Второе предложение новости подробно описывает детали."
        nodes_141 = render_feed([item(text=text_141)], period="day", now=NOW)
        h4_141 = [n for n in nodes_141 if n.get("tag") == "h4"][0]["children"][0]
        self.assertLessEqual(len(h4_141), 140)
        self.assertTrue(h4_141.endswith("…"))
        # Суть — полный исходный текст с начала
        essence_141 = [n for n in nodes_141 if n.get("tag") == "p"][1]["children"][0]
        self.assertTrue(essence_141.startswith("Слово"))

        # 141 символ с разделителем ": " в диапазоне 50-140
        part1 = "Важное заявление исследователей из ведущего университета"  # 56 симв
        part2 = "x" * (141 - len(part1) - 2)
        delim_text = f"{part1}: {part2}. Детали последующего эксперимента."
        self.assertEqual(len(f"{part1}: {part2}"), 141)
        nodes_delim = render_feed([item(text=delim_text)], period="day", now=NOW)
        h4_delim = [n for n in nodes_delim if n.get("tag") == "h4"][0]["children"][0]
        self.assertEqual(h4_delim, part1)

    def test_post_exact_80_chars_boundary(self):
        # Ровно 80 символов: квалифицируется в полную карточку
        text_80 = ("Слово " * 13) + "д."
        self.assertEqual(len(text_80), 80)
        nodes_80 = render_feed([item(text=text_80)], period="day", now=NOW)
        h4_nodes = [n for n in nodes_80 if n.get("tag") == "h4"]
        self.assertEqual(len(h4_nodes), 1)

        # 79 символов: не получает полную карточку, уходит в «Коротко» с заголовком = весь текст
        text_79 = ("Слово " * 13) + "."
        self.assertEqual(len(text_79), 79)
        nodes_79 = render_feed([item(text=text_79)], period="day", now=NOW)
        self.assertFalse(any(n.get("tag") == "h4" for n in nodes_79))
        korotko_ul = [nodes_79[i+1] for i, n in enumerate(nodes_79) if n.get("tag") == "h3" and n.get("children") == ["Коротко"]][0]
        self.assertEqual(len(korotko_ul["children"]), 1)
        self.assertEqual(korotko_ul["children"][0]["children"][0]["children"], [text_79])

    def test_post_under_25_chars_with_and_without_link(self):
        # < 25 символов без ссылки в тексте — не показывается нигде
        text_empty = "Короткая реплика"
        self.assertLess(len(text_empty), 25)
        nodes_empty = render_feed([item(text=text_empty)], period="day", now=NOW)
        self.assertFalse(any(n.get("tag") == "h3" for n in nodes_empty))
        self.assertFalse(any(n.get("tag") == "h4" for n in nodes_empty))

        # < 25 символов со ссылкой в тексте — уходит в «Коротко»
        text_with_link = "Инфо: https://t.me/c/1"
        self.assertLess(len(text_with_link), 25)
        nodes_link = render_feed([item(text=text_with_link)], period="day", now=NOW)
        korotko_h3 = [n for n in nodes_link if n.get("tag") == "h3" and n.get("children") == ["Коротко"]]
        self.assertEqual(len(korotko_h3), 1)

    def test_week_korotko_grouped_by_days(self):
        # Для недели/месяца сюжеты в «Коротко» группируются по дням с h4 внутри Коротко
        # 8 постов для заполнения полной выдачи недели, чтобы следующие ушли в Коротко
        base_items = [
            item(idx, channel=f"ch_{idx}", text=f"Заголовок важной новости {idx}. Полное описание новости {idx} для проверки.")
            for idx in range(1, 9)
        ]
        # Короткие посты за разные даты:
        # сегодня
        p_today = item(9, channel="ch_a", time=NOW, text="Короткая новость сегодня.")
        # вчера
        p_yesterday = item(10, channel="ch_b", time=NOW - timedelta(days=1), text="Короткая новость вчера была.")
        # понедельник 12.10
        p_mon = item(11, channel="ch_c", time=datetime(2026, 10, 12, 10, 0, tzinfo=timezone.utc), text="Короткая новость в понедельник.")

        nodes = render_feed(base_items + [p_today, p_yesterday, p_mon], period="week", now=NOW)
        # Внутри Коротко должны быть h4 с днями
        korotko_idx = [i for i, n in enumerate(nodes) if n.get("tag") == "h3" and n.get("children") == ["Коротко"]][0]
        following = nodes[korotko_idx + 1:]
        h4_day_headings = [n["children"][0] for n in following if n.get("tag") == "h4"]
        self.assertIn("Сегодня", h4_day_headings)
        self.assertIn("Вчера", h4_day_headings)
        self.assertIn("Пн, 12 окт", h4_day_headings)

    def test_also_in_reads_cleanly_and_no_duplicates(self):
        # Подпись вида «Канал · вчера, 18:11 · также: A, B (+2)» читается и один и тот же канал не повторяется
        yesterday_time = NOW - timedelta(days=1, hours=3, minutes=11)  # вчера
        entry = item(
            channel="main_chan",
            channel_title="MainChan",
            time=yesterday_time,
            also_in=[
                {"channel_username": "main_chan", "channel_title": "MainChan", "link": "https://t.me/main/1"},
                {"channel_username": "chan_a", "channel_title": "ChanA", "link": "https://t.me/a/1"},
                {"channel_username": "chan_a", "channel_title": "ChanA-dup", "link": "https://t.me/a/2"},
                {"channel_username": "chan_b", "channel_title": "ChanB", "link": "https://t.me/b/1"},
                {"channel_username": "chan_c", "channel_title": "ChanC", "link": "https://t.me/c/1"},
                {"channel_username": "chan_d", "channel_title": "ChanD", "link": "https://t.me/d/1"},
            ],
        )
        nodes = render_feed([entry], period="week", now=NOW)
        p_nodes = [n for n in nodes if n.get("tag") == "p"]
        meta_node = p_nodes[2]
        meta_text = "".join(
            (c if isinstance(c, str) else "".join(c.get("children", [])))
            for c in meta_node.get("children", [])
        )
        # Проверяем читаемость формата
        self.assertIn("MainChan · вчера, 08:49 · также: ChanA, ChanB (+2)", meta_text)
        # Канал chan_a упомянут ровно один раз
        self.assertEqual(meta_text.count("ChanA"), 1)
        # Основной канал не дублируется в «также:»
        self.assertEqual(meta_text.count("MainChan"), 1)

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
