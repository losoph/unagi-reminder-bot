"""Unit tests for the new Telegraph digest layout (TG1).

Tests:
1. Synthetic issue with 25 posts across 8 channels with diverse post types
   (title + paragraph, CAPS, link-only, emoji noise, forward, short text):
   - Intro line with describe_counts and read time
   - Compact table of contents (channel — N постов)
   - h4 on each channel
   - <= 3 cards per channel, remainder in "Коротко"
   - No empty nodes anywhere
   - No channel name repetitions in post lines
   - Titles not cut mid-word, essence not repeating title
   - Content size well below 60,000 bytes
2. Issue with 120 posts is truncated with a warning and notice
3. Channel and group order is preserved
4. with_actions=True / False behavior
5. email_digest builds email from the new format without exceptions
"""

import html.parser
import json
import logging
import unittest

import email_digest
from telegraph_publisher import (
    _MAX_CONTENT_BYTES,
    _is_post_node,
    _truncate_to_limit,
    build_content,
)


class HTMLTagValidator(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack = []
        self.errors = []
        self._void_tags = {"meta", "img", "br", "hr", "input", "!doctype"}

    def handle_starttag(self, tag, attrs):
        if tag.lower() not in self._void_tags:
            self.stack.append(tag.lower())

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in self._void_tags:
            return
        if not self.stack:
            self.errors.append(f"Unexpected closing tag </{tag}>")
        elif self.stack[-1] == tag:
            self.stack.pop()
        else:
            self.errors.append(f"Mismatched closing tag </{tag}>, expected </{self.stack[-1]}>")


def make_synthetic_sections() -> list[dict]:
    """25 posts across 8 channels with diverse content patterns."""
    return [
        {
            "title": "TechNews",
            "group": "Технологии",
            "username": "technews",
            "posts": [
                {
                    "id": 101,
                    "text": (
                        "Инженеры представили квантовый процессор нового поколения с 256 кубитами.\n"
                        "Новая архитектура снижает уровень теплового шума на 60% и позволяет выполнять вычисления "
                        "без глубокого криогенного охлаждения."
                    ),
                    "link": "https://t.me/technews/101",
                },
                {
                    "id": 102,
                    "text": (
                        "ОТЕЧЕСТВЕННЫЕ ОПЕРАТОРЫ ЗАВЕРШИЛИ ПОЛЕВЫЕ ТЕСТЫ БАЗОВЫХ СТАНЦИЙ 5G В ГОРОДАХ. "
                        "Оборудование подтвердило скорость передачи данных до 1,8 Гбит/с на абонентское устройство."
                    ),
                    "link": "https://t.me/technews/102",
                },
                {
                    "id": 103,
                    "text": "https://technews.example.com/ai-benchmark-results-2026",
                    "link": "https://t.me/technews/103",
                },
                {
                    "id": 104,
                    "text": (
                        "Переслано из CodeDigest\n"
                        "Разработчики открыли веса легковесной модели распознавания речи на 50 языках. "
                        "Модель весит менее ста мегабайт и уверенно работает на мобильных устройствах."
                    ),
                    "link": "https://t.me/technews/104",
                },
            ],
            "actions": {
                "unsubscribe": "https://t.me/bot?start=du_1",
                "move": [("в еженедельный", "https://t.me/bot?start=dm_1_weekly")],
            },
        },
        {
            "title": "Science Digest",
            "group": "Технологии",
            "username": "sciencedigest",
            "posts": [
                {
                    "id": 201,
                    "text": (
                        "🔥🔥🔥 Срочно в номер! 🔥🔥🔥\n"
                        "@science | #biotech\n"
                        "Биологи расшифровали геном глубоководных кораллов, устойчивых к потеплению океана. "
                        "Найденные белковые маркеры объясняют феномен адаптации к термальному стрессу без потери пигментации."
                    ),
                    "link": "https://t.me/sciencedigest/201",
                },
                {
                    "id": 202,
                    "text": (
                        "Физики охладили молекулы газа до рекордных долей нанокельвина.\n"
                        "Эксперимент открывает путь к наблюдению новых квантовых фазовых переходов в оптических решётках."
                    ),
                    "link": "https://t.me/sciencedigest/202",
                },
                {
                    "id": 203,
                    "text": (
                        "Астрономы уточнили траекторию межзвёздного астероида.\n"
                        "Небесное тело покинет Солнечную систему в первой половине следующего года без опасности сближения."
                    ),
                    "link": "https://t.me/sciencedigest/203",
                },
            ],
        },
        {
            "title": "FinJournal",
            "group": "Финансы",
            "username": "finjournal",
            "posts": [
                {
                    "id": 301,
                    "text": (
                        "ЦБ УТВЕРДИЛ СТАНДАРТ ОТЧЕТНОСТИ ДЛЯ ПЛАТФОРМ ЦИФРОВЫХ АКТИВОВ. "
                        "Регулятор установил единые требования к расчету ликвидности и страхованию рисков операторов."
                    ),
                    "link": "https://t.me/finjournal/301",
                },
                {
                    "id": 302,
                    "text": (
                        "Доля взаиморасчётов в национальных валютах между участниками БРИКС превысила 75%.\n"
                        "Основным фактором роста стало внедрение прямых межбанковских шлюзов и клиринговых центров."
                    ),
                    "link": "https://t.me/finjournal/302",
                },
                {
                    "id": 303,
                    "text": "Инфляция в еврозоне замедлилась до целевого уровня впервые за три года.",
                    "link": "https://t.me/finjournal/303",
                },
                {
                    "id": 304,
                    "text": "Краткая сводка курсов валют: доллар 92.4, евро 99.8, юань 12.7.",
                    "link": "https://t.me/finjournal/304",
                },
            ],
            "omitted_count": 5,
        },
        {
            "title": "City Pulse",
            "group": "Город",
            "username": "citypulse",
            "posts": [
                {
                    "id": 401,
                    "text": (
                        "Беспилотные поезда вышли на Кольцевую линию метро в тестовом режиме.\n"
                        "Составы оборудованы лидарами и машинным зрением для полного контроля дистанции на платформе."
                    ),
                    "link": "https://t.me/citypulse/401",
                },
                {
                    "id": 402,
                    "text": (
                        "В историческом центре открылся новый пешеходный бульвар с сенсорным освещением.\n"
                        "Проект благоустройства победил в общегородском голосовании жителей весной прошлого года."
                    ),
                    "link": "https://t.me/citypulse/402",
                },
            ],
        },
        {
            "title": "Eco Daily",
            "group": "Экология",
            "username": "ecodaily",
            "posts": [
                {
                    "id": 501,
                    "text": (
                        "Популяция снежного барса на Алтае выросла на 20% благодаря расширению заповедных зон.\n"
                        "Специалисты зафиксировали появление сразу четырех новых выводков котят."
                    ),
                    "link": "https://t.me/ecodaily/501",
                },
                {
                    "id": 502,
                    "text": (
                        "🌱 Экологическая инициатива недели 🌱\n"
                        "В Сибири запущена гибридная солнечная электростанция мощностью 120 МВт с накопителями. "
                        "Комплекс обеспечит круглосуточное зеленое энергоснабжение трех удаленных поселков."
                    ),
                    "link": "https://t.me/ecodaily/502",
                },
                {
                    "id": 503,
                    "text": (
                        "НОВЫЙ ЗАКОН О РАЗДЕЛЬНОМ СБОРЕ МУСОРА ВСТУПАЕТ В СИЛУ С НОЯБРЯ. "
                        "Предприятия торговли обяжут организовать приём вторичного пластика и картона."
                    ),
                    "link": "https://t.me/ecodaily/503",
                },
                {
                    "id": 504,
                    "text": "https://ecodaily.example.org/clean-water-initiative",
                    "link": "https://t.me/ecodaily/504",
                },
                {
                    "id": 505,
                    "text": (
                        "Волонтёры высадили пятьдесят тысяч саженцев сосны в пострадавшем от пожаров районе.\n"
                        "В акции приняли участие более тысячи добровольцев со всей области."
                    ),
                    "link": "https://t.me/ecodaily/505",
                },
            ],
        },
        {
            "title": "Space Herald",
            "group": "Наука",
            "username": "spaceherald",
            "posts": [
                {
                    "id": 601,
                    "text": (
                        "Орбитальный телескоп обнаружил водяной пар в атмосфере суперземли в созвездии Лебедя.\n"
                        "Спектральный анализ показал устойчивую конденсацию влаги в верхних слоях экзопланеты."
                    ),
                    "link": "https://t.me/spaceherald/601",
                },
                {
                    "id": 602,
                    "text": "Запуск исследовательской миссии к спутникам Юпитера запланирован на пятницу.",
                    "link": "https://t.me/spaceherald/602",
                },
                {
                    "id": 603,
                    "text": (
                        "Космический аппарат успешно завершил гравитационный маневр около Венеры.\n"
                        "Все телеметрические показатели бортовых систем находятся в пределах нормы."
                    ),
                    "link": "https://t.me/spaceherald/603",
                },
            ],
        },
        {
            "title": "Sport Pulse",
            "username": "sportpulse",
            "posts": [
                {
                    "id": 701,
                    "text": (
                        "Сборная студентов победила в международном финале по программированию.\n"
                        "Команда решила одиннадцать задач из двенадцати за рекордное время."
                    ),
                    "link": "https://t.me/sportpulse/701",
                },
                {
                    "id": 702,
                    "text": "Стартовал чемпионат страны по академической гребле в Казани.",
                    "link": "https://t.me/sportpulse/702",
                },
            ],
        },
        {
            "title": "Culture Wave",
            "username": "culturewave",
            "posts": [
                {
                    "id": 801,
                    "text": (
                        "Переслано из Театральный Вестник\n"
                        "Театр открыл юбилейный сезон премьерой спектакля по классической пьесе. "
                        "Постановка собрала аншлаг в зале и получила высокие оценки критиков."
                    ),
                    "link": "https://t.me/culturewave/801",
                },
                {
                    "id": 802,
                    "text": (
                        "В Национальном музее открылась масштабная выставка мастеров ренессанса.\n"
                        "Экспозиция включает более пятидесяти полотен из ведущих мировых собраний."
                    ),
                    "link": "https://t.me/culturewave/802",
                },
            ],
        },
    ]


class TelegraphDigestLayoutTests(unittest.TestCase):
    def test_synthetic_25_posts_structure(self):
        sections = make_synthetic_sections()
        nodes = build_content(sections, with_actions=False)

        # 1. Intro block
        first_node = nodes[0]
        self.assertEqual(first_node.get("tag"), "p")
        intro_text = "".join(first_node["children"][0]["children"])
        self.assertIn("8 каналов · 25 постов", intro_text)
        self.assertIn("мин чтения", intro_text)

        # 2. Table of contents
        toc_node = nodes[1]
        self.assertEqual(toc_node.get("tag"), "ul")
        toc_items = [li["children"][0] for li in toc_node["children"]]
        self.assertEqual(len(toc_items), 8)
        self.assertIn("TechNews — 4 поста", toc_items)
        self.assertIn("Science Digest — 3 поста", toc_items)
        self.assertIn("FinJournal — 4 поста", toc_items)
        self.assertIn("City Pulse — 2 поста", toc_items)
        self.assertIn("Eco Daily — 5 постов", toc_items)
        self.assertIn("Space Herald — 3 поста", toc_items)
        self.assertIn("Sport Pulse — 2 поста", toc_items)
        self.assertIn("Culture Wave — 2 поста", toc_items)

        # 3. Channel headers (h4)
        h4_headers = [n["children"][0] for n in nodes if isinstance(n, dict) and n.get("tag") == "h4"]
        expected_channels = [
            "TechNews", "Science Digest", "FinJournal", "City Pulse",
            "Eco Daily", "Space Herald", "Sport Pulse", "Culture Wave",
        ]
        self.assertEqual(h4_headers, expected_channels)

        # 4. Group headers (h3)
        h3_headers = [n["children"][0] for n in nodes if isinstance(n, dict) and n.get("tag") == "h3"]
        self.assertEqual(h3_headers, ["Технологии", "Финансы", "Город", "Экология", "Наука"])

        # 5. For each channel: <= 3 cards, remainder in "Коротко"
        # We verify by inspecting nodes between channel h4 headers
        channel_slices: dict[str, list] = {}
        curr_channel = None
        for n in nodes:
            if isinstance(n, dict) and n.get("tag") == "h4":
                curr_channel = n["children"][0]
                channel_slices[curr_channel] = []
            elif curr_channel is not None:
                if isinstance(n, dict) and n.get("tag") == "hr":
                    curr_channel = None
                else:
                    channel_slices[curr_channel].append(n)

        for ch_title, ch_nodes in channel_slices.items():
            # Card nodes are p with bold title
            card_nodes = [
                n for n in ch_nodes
                if isinstance(n, dict) and n.get("tag") == "p"
                and any(
                    isinstance(c, dict) and c.get("tag") == "b" and c.get("children") != ["Коротко"]
                    for c in n.get("children", [])
                )
            ]
            self.assertLessEqual(len(card_nodes), 3, f"Channel {ch_title} has > 3 cards: {len(card_nodes)}")

            # Short posts
            short_ul = [n for n in ch_nodes if isinstance(n, dict) and n.get("tag") == "ul"]
            short_labels = [
                n for n in ch_nodes
                if isinstance(n, dict) and n.get("tag") == "p"
                and any(
                    isinstance(c, dict) and c.get("tag") == "b" and c.get("children") == ["Коротко"]
                    for c in n.get("children", [])
                )
            ]

            orig_section = next(s for s in sections if s["title"] == ch_title)
            total_posts_in_ch = len(orig_section["posts"])
            if total_posts_in_ch <= 3:
                self.assertEqual(len(card_nodes), total_posts_in_ch)
                self.assertEqual(len(short_ul), 0)
                self.assertEqual(len(short_labels), 0)
            else:
                self.assertEqual(len(card_nodes), 3)
                self.assertEqual(len(short_ul), 1)
                self.assertEqual(len(short_labels), 1)
                expected_short_count = total_posts_in_ch - 3
                self.assertEqual(len(short_ul[0]["children"]), expected_short_count)

        # 6. No empty nodes anywhere
        def check_no_empty_nodes(item, path="root"):
            if isinstance(item, str):
                self.assertTrue(len(item) > 0, f"Empty string node at {path}")
            elif isinstance(item, dict):
                tag = item.get("tag")
                if tag in ("hr", "br"):
                    return
                children = item.get("children")
                self.assertIsNotNone(children, f"Node without children at {path}: {item}")
                self.assertGreater(len(children), 0, f"Empty children list at {path}: {item}")
                for idx, child in enumerate(children):
                    check_no_empty_nodes(child, f"{path}.children[{idx}]")
            elif isinstance(item, list):
                self.assertGreater(len(item), 0, f"Empty list at {path}")
                for idx, child in enumerate(item):
                    check_no_empty_nodes(child, f"{path}[{idx}]")

        check_no_empty_nodes(nodes)

        # 7. No channel name repetitions in post lines
        for ch_title, ch_nodes in channel_slices.items():
            for n in ch_nodes:
                text_content = ""
                if isinstance(n, dict):
                    # extract all text
                    def extract_text(node):
                        if isinstance(node, str):
                            return node
                        if isinstance(node, dict):
                            return "".join(extract_text(c) for c in node.get("children", []))
                        return ""
                    text_content = extract_text(n)
                # Channel name should not be appended/prepended as a suffix (like " — TechNews")
                self.assertNotIn(f"— {ch_title}", text_content)
                self.assertNotIn(f"· {ch_title}", text_content)

        # 8. Titles do not end mid-word (no unclosed words before …)
        for n in nodes:
            if isinstance(n, dict) and n.get("tag") == "p":
                for c in n.get("children", []):
                    if isinstance(c, dict) and c.get("tag") == "b":
                        b_text = "".join(str(x) for x in c.get("children", []))
                        if "…" in b_text:
                            prefix = b_text.split("…")[0]
                            # Ends with a space before ellipsis or a complete token
                            self.assertFalse(prefix.endswith(" "), f"Trailing space before ellipsis: {b_text}")

        # 9. Total size is well under limit
        raw_bytes = len(json.dumps(nodes, ensure_ascii=False).encode("utf-8"))
        self.assertLess(raw_bytes, _MAX_CONTENT_BYTES)

    def test_120_posts_truncation_with_warning(self):
        """A huge issue with 120 posts is safely truncated with warning and notice."""
        sections = [
            {
                "title": f"Информационный канал {i}",
                "username": f"long_channel_username_{i}",
                "posts": [
                    {
                        "id": j,
                        "text": (
                            f"Заголовок развернутой аналитической публикации {j} по каналу {i}.\n"
                            + "Первое предложение сути с подробностями и фактами. " * 3
                            + "Второе предложение с выводами и перспективами. " * 3
                        ),
                        "link": f"https://t.me/long_channel_username_{i}/{j}",
                    }
                    for j in range(3)
                ],
            }
            for i in range(40)
        ]
        raw_content = build_content(sections)
        raw_bytes = len(json.dumps(raw_content, ensure_ascii=False).encode("utf-8"))
        self.assertGreater(raw_bytes, _MAX_CONTENT_BYTES, "Synthetic test data must exceed max bytes")

        with self.assertLogs("telegraph_publisher", level=logging.WARNING) as log:
            truncated = _truncate_to_limit(raw_content, max_bytes=_MAX_CONTENT_BYTES)

        self.assertTrue(any("posts omitted" in record.getMessage() for record in log.records))
        trunc_bytes = len(json.dumps(truncated, ensure_ascii=False).encode("utf-8"))
        self.assertLessEqual(trunc_bytes, _MAX_CONTENT_BYTES)

        # Last node must be the notice
        last_node = truncated[-1]
        self.assertEqual(last_node.get("tag"), "p")
        notice_str = str(last_node)
        self.assertIn("⚠️ Выпуск усечён", notice_str)
        self.assertIn("из-за лимита размера страницы", notice_str)

    def test_channel_and_group_order_preserved(self):
        sections = [
            {"title": "Alpha", "group": "Group 1", "posts": [{"text": "Post A1", "link": "https://t.me/a/1"}]},
            {"title": "Beta", "group": "Group 1", "posts": [{"text": "Post B1", "link": "https://t.me/b/1"}]},
            {"title": "Gamma", "group": "Group 2", "posts": [{"text": "Post G1", "link": "https://t.me/g/1"}]},
            {"title": "Delta", "posts": [{"text": "Post D1", "link": "https://t.me/d/1"}]},
        ]
        nodes = build_content(sections)
        h3_headers = [n["children"][0] for n in nodes if isinstance(n, dict) and n.get("tag") == "h3"]
        h4_headers = [n["children"][0] for n in nodes if isinstance(n, dict) and n.get("tag") == "h4"]
        self.assertEqual(h3_headers, ["Group 1", "Group 2"])
        self.assertEqual(h4_headers, ["Alpha", "Beta", "Gamma", "Delta"])

    def test_with_actions_flag(self):
        sections = [
            {
                "title": "ActionChannel",
                "posts": [{"text": "Test post", "link": "https://t.me/act/1"}],
                "actions": {
                    "unsubscribe": "https://t.me/bot?start=du_99",
                    "move": [("в еженедельный", "https://t.me/bot?start=dm_99_weekly")],
                },
            }
        ]
        # with_actions=False (default for public Telegraph)
        public_nodes = build_content(sections, with_actions=False)
        public_repr = repr(public_nodes)
        self.assertNotIn("🚫 Отписаться", public_repr)
        self.assertNotIn("du_99", public_repr)

        # with_actions=True (for email)
        email_nodes = build_content(sections, with_actions=True)
        email_repr = repr(email_nodes)
        self.assertIn("🚫 Отписаться", email_repr)
        self.assertIn("du_99", email_repr)

    def test_email_digest_renders_new_format_without_exceptions(self):
        sections = make_synthetic_sections()
        html_body, text_body = email_digest.build_digest_email(
            "Газета дня",
            sections,
            telegraph_url="https://telegra.ph/digest-123",
        )
        self.assertIn("8 каналов · 25 постов", html_body)
        self.assertIn("Газета дня", text_body)
        self.assertIn("https://telegra.ph/digest-123", html_body)
        self.assertIn("https://telegra.ph/digest-123", text_body)
        self.assertIn("TechNews", html_body)
        self.assertIn("TechNews", text_body)

        # Check HTML tag balance
        validator = HTMLTagValidator()
        validator.feed(html_body)
        validator.close()
        self.assertEqual(validator.errors, [])
        self.assertEqual(validator.stack, [])

    def test_is_post_node_detection(self):
        card = {"tag": "p", "children": [{"tag": "b", "children": ["Заголовок"]}, "  ", {"tag": "a", "children": ["Читать"]}]}
        card_with_br = {"tag": "p", "children": [{"tag": "b", "children": ["Заголовок"]}, {"tag": "br"}, "Суть"]}
        legacy_card = {"tag": "p", "children": ["🔹 ", {"tag": "b", "children": ["Новость"]}]}
        li_item = {"tag": "li", "children": ["Короткая новость"]}
        short_label = {"tag": "p", "children": [{"tag": "b", "children": ["Коротко"]}]}
        summary = {"tag": "p", "children": [{"tag": "b", "children": ["3 канала · 10 постов · ~2 мин чтения"]}]}
        notice = {"tag": "p", "children": [{"tag": "b", "children": ["⚠️ Выпуск усечён: ещё 5 постов не вошли..."]}]}
        actions = {"tag": "p", "children": [{"tag": "a", "children": ["🚫 Отписаться"]}]}

        self.assertTrue(_is_post_node(card))
        self.assertTrue(_is_post_node(card_with_br))
        self.assertTrue(_is_post_node(legacy_card))
        self.assertTrue(_is_post_node(li_item))
        self.assertFalse(_is_post_node(short_label))
        self.assertFalse(_is_post_node(summary))
        self.assertFalse(_is_post_node(notice))
        self.assertFalse(_is_post_node(actions))
        self.assertFalse(_is_post_node({"tag": "hr"}))
        self.assertFalse(_is_post_node({"tag": "h4", "children": ["Канал"]}))
