"""Unit tests for TX1: shared post_text module, feed heuristics, counts, absolute dates, and headline truncation."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import feed_render
import post_text
import telegraph_publisher

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)


def _make_item(
    rank=1,
    *,
    tag=None,
    channel="technews",
    channel_title=None,
    post_id=1,
    text="Стандартный текст новости, содержащий более восьмидесяти символов для формирования полноценной карточки.",
    time=None,
    views=5000,
    link=None,
):
    c_title = channel_title if channel_title is not None else channel.capitalize()
    post_link = f"https://t.me/{channel}/{post_id}" if link is None else link
    return SimpleNamespace(
        rank=rank,
        score=0.9,
        tag=tag,
        channel_title=c_title,
        post={
            "post_id": post_id,
            "channel_username": channel,
            "channel_title": c_title,
            "tag": tag,
            "text": text,
            "link": post_link,
            "time": time or NOW,
            "views": views,
        },
        also_in=[],
        badges=[],
        is_ad=False,
    )


class PostTextModuleTests(unittest.TestCase):
    """Test 1: pure post_text module without network/DB/feed imports."""

    def test_no_forbidden_imports_in_post_text(self):
        import sys
        mod = sys.modules["post_text"]
        forbidden_prefixes = ("feed", "telegraph_publisher", "data", "main")
        for attr_name in dir(mod):
            val = getattr(mod, attr_name)
            if hasattr(val, "__module__") and val.__module__:
                for forbidden in forbidden_prefixes:
                    self.assertFalse(
                        val.__module__.startswith(forbidden),
                        f"post_text leaks import from {val.__module__}",
                    )

    def test_reexports_in_feed_render_and_telegraph_publisher(self):
        # Existing names are still accessible on feed_render and telegraph_publisher
        for name in (
            "_clean_post_text",
            "_extract_title_and_essence",
            "_normalize_caps_title",
            "_truncate_by_word",
            "_strip_title_prefix",
            "_find_sentence_boundary",
            "_split_sentences",
            "_find_semantic_cut",
            "_first_sentence",
            "clean_post_for_render",
        ):
            self.assertTrue(hasattr(feed_render, name), f"feed_render missing {name}")

        for name in (
            "_clean_post_for_digest",
            "_strip_forwarding_lines",
            "_is_banner_line",
            "_strip_banner_line",
            "_extract_bare_url_domain",
            "_split_first_sentence",
            "clean_post_for_render",
        ):
            self.assertTrue(hasattr(telegraph_publisher, name), f"telegraph_publisher missing {name}")

    def test_forwarding_prefixes_stripped_when_text_follows(self):
        cases = [
            ("Переслано из Новости ИИ\nНовая открытая модель превосходит аналоги.", "Новая открытая модель превосходит аналоги."),
            ("Forwarded from TechCrunch\nOpenAI announced new features today.", "OpenAI announced new features today."),
            ("Источник: РБК\nЦБ установил новые курсы валют на завтра.", "ЦБ установил новые курсы валют на завтра."),
            ("via @channel\nКороткая заметка с интересными фактами.", "Короткая заметка с интересными фактами."),
            ("repost from daily\nИнженеры создали прототип нового двигателя.", "Инженеры создали прототип нового двигателя."),
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw[:20]):
                self.assertEqual(post_text.strip_forwarding_lines(raw), expected)

    def test_banner_line_stripped_when_text_follows(self):
        cases = [
            ("🔥 Срочно в номер!\nМинцифры запустило новый сервис для граждан.", "Минцифры запустило новый сервис для граждан."),
            ("⚡️⚡️ ВАЖНО ⚡️⚡️\nСовет директоров утвердил новую дивидендную политику компании.", "Совет директоров утвердил новую дивидендную политику компании."),
            ("🚨 Молния!\nЗавершились международные переговоры по климату.", "Завершились международные переговоры по климату."),
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw[:20]):
                self.assertEqual(post_text.strip_banner_line(raw), expected)

    def test_bare_url_domain_extraction(self):
        self.assertEqual(post_text.extract_bare_url_domain("https://example.com/path/to/page"), "example.com")
        self.assertEqual(post_text.extract_bare_url_domain("https://www.rbc.ru/society/16/10/2026"), "rbc.ru")
        self.assertEqual(post_text.extract_bare_url_domain("http://sub.domain.org:8080/test"), "sub.domain.org")
        # Mixed text is not a bare URL
        self.assertIsNone(post_text.extract_bare_url_domain("Ссылка на новость: https://example.com"))
        self.assertIsNone(post_text.extract_bare_url_domain("Не ссылка"))


class TruncationAndHangingWordsTests(unittest.TestCase):
    """Test 5: truncation by word without dangling prepositions, conjunctions, or leading ellipsis."""

    def test_hanging_prepositions_and_conjunctions_dropped_before_ellipsis(self):
        cases = [
            # Dangling "для"
            (
                "Правительство Российской Федерации утвердило новые правила для",
                "Правительство Российской Федерации утвердило новые правила…",
            ),
            # Dangling "и"
            (
                "Компания объявила о выходе на международный рынок и",
                "Компания объявила о выходе на международный рынок…",
            ),
            # Dangling "о"
            (
                "Власти готовят масштабный законопроект о",
                "Власти готовят масштабный законопроект…",
            ),
            # Dangling "в"
            (
                "Команда специалистов приступила к исследованию в",
                "Команда специалистов приступила к исследованию…",
            ),
            # Dangling "из-за"
            (
                "Сделка между крупными корпорациями сорвалась из-за",
                "Сделка между крупными корпорациями сорвалась…",
            ),
            # Dangling comma before hanging conjunction
            (
                "Эксперты подтвердили высокую надёжность системы, но",
                "Эксперты подтвердили высокую надёжность системы…",
            ),
        ]
        for phrase, expected in cases:
            with self.subTest(phrase=phrase):
                # Request truncation slightly longer than the full text to force word boundary cut
                cut = post_text.truncate_by_word(phrase + " продолжение текста дальше", len(phrase) + 3)
                self.assertEqual(cut, expected)
                self.assertFalse(cut.endswith(" …"))
                self.assertTrue(cut.endswith("…"))

    def test_no_trailing_punctuation_before_ellipsis(self):
        phrase = "Соглашение подписано сторонами, однако продолжение"
        cut = post_text.truncate_by_word(phrase, 33)
        self.assertFalse(cut.endswith(",…"))
        self.assertFalse(cut.endswith(" …"))
        self.assertTrue(cut.endswith("сторонами…"))

    def test_essence_never_starts_with_ellipsis(self):
        # When title prefix is stripped, essence must never begin with "…"
        title = "Anthropic вместе с Claude потратил пять месяцев"
        essence = "Anthropic вместе с Claude потратил пять месяцев на создание замены Git и ускорение разработки."
        cleaned_essence = post_text.strip_title_prefix(title, essence)
        self.assertFalse(cleaned_essence.startswith("…"))
        self.assertTrue(cleaned_essence[0].isupper())
        self.assertEqual(cleaned_essence, "На создание замены Git и ускорение разработки.")

    def test_extract_title_and_essence_never_starts_with_ellipsis(self):
        # Raw text with prefix that gets cut
        raw = ("GitHub не смогли смириться с тем, что разработчики больше не используют git, "
               "и выплюнули в соцсети следующее заявление: создание замены системы контроля версий.")
        title, essence = post_text.extract_title_and_essence(raw, max_title=80)
        self.assertFalse(title.startswith("…"))
        self.assertFalse(essence.startswith("…"))
        if essence:
            self.assertTrue(essence[0].isupper())


class FeedHeuristicsTests(unittest.TestCase):
    """Test 2: feed uses heuristics (forwarding, banner, bare URL) without ranking changes."""

    def test_card_strips_forwarding_header(self):
        raw_text = (
            "Переслано из Незыгарь\n"
            "Президент подписал указ о назначении нового министра цифрового развития. "
            "Документ вступил в силу со дня опубликования на официальном портале правовой информации."
        )
        feed_item = _make_item(text=raw_text)
        nodes = feed_render.render_feed([feed_item], period="day", now=NOW)
        h4_nodes = [n for n in nodes if n.get("tag") == "h4"]
        self.assertEqual(len(h4_nodes), 1)
        title = h4_nodes[0]["children"][0]
        self.assertNotIn("Переслано", title)
        self.assertIn("Президент подписал указ", title)

    def test_card_strips_banner_line(self):
        raw_text = (
            "🔥 Срочно в номер!\n"
            "Крупнейший банк объявил о снижении процентной ставки по ипотечным кредитам. "
            "Новые условия начнут действовать со следующего понедельника для всех категорий клиентов."
        )
        feed_item = _make_item(text=raw_text)
        nodes = feed_render.render_feed([feed_item], period="day", now=NOW)
        h4_nodes = [n for n in nodes if n.get("tag") == "h4"]
        self.assertEqual(len(h4_nodes), 1)
        title = h4_nodes[0]["children"][0]
        self.assertNotIn("Срочно в номер", title)
        self.assertIn("Крупнейший банк объявил о снижении", title)

    def test_shorts_bare_url_replaces_with_domain_icon(self):
        # Candidate with bare URL qualifies for shorts (has link, <80 chars)
        feed_item = _make_item(
            post_id=42,
            text="https://technews.example.com/ai-benchmark-results-2026",
            link="https://t.me/technews/42",
        )
        nodes = feed_render.render_feed([feed_item], period="day", now=NOW)
        # Should be in "Коротко"
        korotko_h3 = [n for n in nodes if n.get("tag") == "h3" and n.get("children") == ["Коротко"]]
        self.assertEqual(len(korotko_h3), 1)
        ul_node = nodes[nodes.index(korotko_h3[0]) + 1]
        li = ul_node["children"][0]
        link_node = li["children"][0]
        self.assertEqual(link_node["children"], ["🔗 ссылка · technews.example.com"])
        self.assertEqual(link_node["attrs"]["href"], "https://t.me/technews/42")

    def test_shorts_complete_sentence_under_140_chars_not_chopped(self):
        # A 95-character single complete sentence should NOT be chopped with ellipsis at 80 chars
        sentence_95 = "Правительство утвердило план комплексного развития транспортной инфраструктуры на пять лет."
        self.assertGreater(len(sentence_95), 80)
        self.assertLessEqual(len(sentence_95), 140)

        # Force item into "Коротко" by having 6 full cards already in day edition
        base_cards = [
            _make_item(
                idx,
                channel=f"ch_{idx}",
                text=f"Заголовок важной новости номер {idx}. Подробное описание новости {idx} для проверки отображения полной карточки сюжета.",
            )
            for idx in range(1, 7)
        ]
        short_item = _make_item(7, channel="ch_short", text=sentence_95)

        nodes = feed_render.render_feed(base_cards + [short_item], period="day", now=NOW)
        korotko_h3 = [n for n in nodes if n.get("tag") == "h3" and n.get("children") == ["Коротко"]][0]
        ul_node = nodes[nodes.index(korotko_h3) + 1]
        li = ul_node["children"][0]
        short_title = li["children"][0]["children"][0]
        self.assertEqual(short_title, sentence_95.rstrip("."))
        self.assertFalse(short_title.endswith("…"))


class FeedIntroCountsTests(unittest.TestCase):
    """Test 3: intro line numbers match displayed items exactly."""

    def test_intro_line_matches_shown_cards_plus_shorts_count(self):
        # 50 candidate items from 7 channels
        items = []
        for i in range(50):
            ch = f"chan_{i % 7}"
            items.append(_make_item(
                i + 1,
                channel=ch,
                text=f"Новость номер {i + 1} от канала {ch}. Подробный текст новости раскрывает все существенные обстоятельства.",
            ))
        nodes = feed_render.render_feed(items, period="week", now=NOW)
        # In week feed, glavnoe has 5 items in ul (without h4), stories has cards (each has h4), korotko has day h4s and lis
        # Let's count items in "Сюжеты" and "Коротко":
        h3_tags = [n.get("children", [""])[0] for n in nodes if n.get("tag") == "h3"]
        self.assertIn("Сюжеты", h3_tags)
        self.assertIn("Коротко", h3_tags)

        # Total shown items: 8 full cards + 15 short items = 23 items
        intro_text = nodes[0]["children"][0]["children"][0]
        # Should state "23 поста из 7 каналов"
        self.assertIn("23 поста из 7 каналов", intro_text)
        self.assertNotIn("50 постов", intro_text)


class AbsoluteDatesTests(unittest.TestCase):
    """Test 4: absolute dates everywhere on static page, no 'сегодня'/'вчера'."""

    def test_card_dates_are_absolute_on_today_yesterday_and_older(self):
        post_today = _make_item(1, channel="c1", time=NOW)
        post_yesterday = _make_item(2, channel="c2", time=NOW - timedelta(days=1))
        post_older = _make_item(3, channel="c3", time=datetime(2026, 10, 9, 18, 30, tzinfo=timezone.utc))

        nodes = feed_render.render_feed([post_today, post_yesterday, post_older], period="week", now=NOW)
        rendered = repr(nodes)

        # Expected absolute weekday and dd.mm formats:
        self.assertIn("ср 14.10, 12:00", rendered)
        self.assertIn("вт 13.10, 12:00", rendered)
        self.assertIn("пт 09.10, 18:30", rendered)

        # Forbidden relative terms:
        self.assertNotIn("сегодня", rendered.lower())
        self.assertNotIn("вчера", rendered.lower())

    def test_korotko_day_headings_are_absolute(self):
        base_cards = [
            _make_item(idx, channel=f"ch_{idx}", text=f"Заголовок важной новости {idx}. Полное описание новости {idx} для проверки.")
            for idx in range(1, 9)
        ]
        p_today = _make_item(9, channel="ch_a", time=NOW, text="Короткая новость номер девять.")
        p_yesterday = _make_item(10, channel="ch_b", time=NOW - timedelta(days=1), text="Короткая новость номер десять.")
        p_friday = _make_item(11, channel="ch_c", time=datetime(2026, 10, 9, 10, 0, tzinfo=timezone.utc), text="Короткая новость в прошлую пятницу.")

        nodes = feed_render.render_feed(base_cards + [p_today, p_yesterday, p_friday], period="week", now=NOW)
        korotko_idx = [i for i, n in enumerate(nodes) if n.get("tag") == "h3" and n.get("children") == ["Коротко"]][0]
        h4_day_headings = [n["children"][0] for n in nodes[korotko_idx + 1:] if n.get("tag") == "h4"]

        self.assertIn("Ср, 14 окт", h4_day_headings)
        self.assertIn("Вт, 13 окт", h4_day_headings)
        self.assertIn("Пт, 9 окт", h4_day_headings)
        self.assertNotIn("Сегодня", h4_day_headings)
        self.assertNotIn("Вчера", h4_day_headings)


if __name__ == "__main__":
    unittest.main()
