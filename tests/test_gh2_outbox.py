"""Unit and integration tests for ghsub outbox adapter (GH2).

Tests requirements:
1. Default flag (EMAIL_DELIVERY="unagi") -> email sent via email_digest.send_email, no outbox file.
2. EMAIL_DELIVERY="ghsub" -> no email sent, outbox file created, JSON v1 valid by contract.
3. TG1 tier partitioning -> up to 3 cards per channel, rest brief.
4. Text cleaning (TX1) without HTML, all URLs strictly http(s).
5. Deterministic edition_id on repeat -> exact same edition_id.
6. Atomic write -> .tmp replaced, no .tmp files remain after success.
7. manage_url included only when bot username is known.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("BOT_TOKEN", "123456:TEST_TOKEN_FOR_UNIT_TESTS")

from config import reset_settings, settings  # noqa: E402
import email_digest  # noqa: E402
from ghsub_outbox import build_ghsub_edition, format_iso8601, strip_html, write_ghsub_outbox  # noqa: E402
import main  # noqa: E402


class Gh2OutboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="ghsub_test_")
        reset_settings()

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)
        reset_settings()

    def _sample_sections(self, post_count: int = 2) -> list[dict]:
        posts = []
        for i in range(1, post_count + 1):
            posts.append({
                "id": 100 + i,
                "text": f"Заголовок {i}. Подробности новости {i} в канале.",
                "link": f"https://t.me/tech_news/{100 + i}",
                "time": f"2026-10-18 06:3{i}:00",
                "views": 1500 * i,
                "forwards": 25 * i,
            })
        return [
            {
                "sub_id": 10,
                "user_id": 42,
                "period": "daily",
                "username": "tech_news",
                "title": "Tech News",
                "posts": posts,
            }
        ]

    def test_default_flag_sends_email_and_no_outbox_file(self):
        """Under default config (EMAIL_DELIVERY=unagi), email is sent normally and no outbox file is written."""
        sections = self._sample_sections()
        env = {
            "EMAIL_DELIVERY": "unagi",
            "EMAIL_OUTBOX_DIR": self.tmp_dir,
            "BOT_USERNAME": "test_bot",
        }
        with mock.patch.dict(os.environ, env), mock.patch.object(email_digest, "send_email", return_value=True) as mock_send:
            reset_settings()
            self.assertEqual(settings().email.delivery, "unagi")

            ok = asyncio.run(
                main.send_digest_email("user@example.com", "Газета", sections, user_id=42)
            )
            self.assertTrue(ok)
            mock_send.assert_called_once()
            # Outbox directory should contain no json files
            if os.path.exists(self.tmp_dir):
                files = [f for f in os.listdir(self.tmp_dir) if f.endswith(".json")]
                self.assertEqual(files, [])

    def test_ghsub_delivery_creates_valid_contract_json_and_no_email_sent(self):
        """Under EMAIL_DELIVERY=ghsub, email is not sent via SMTP, and a valid JSON v1 file is created."""
        sections = self._sample_sections(2)
        sched_time = "2026-10-18T07:00:00Z"
        env = {
            "EMAIL_DELIVERY": "ghsub",
            "EMAIL_OUTBOX_DIR": self.tmp_dir,
            "BOT_USERNAME": "test_bot",
            "APP_TIMEZONE": "Europe/Moscow",
        }
        with mock.patch.dict(os.environ, env), mock.patch.object(main, "BOT_USERNAME", "test_bot"), mock.patch.object(email_digest, "send_email") as mock_send:
            reset_settings()
            self.assertEqual(settings().email.delivery, "ghsub")

            ok = asyncio.run(
                main.send_digest_email(
                    "user@example.com",
                    "Газета",
                    sections,
                    user_id=42,
                    scheduled_at=sched_time,
                )
            )
            self.assertTrue(ok)
            # SMTP send_email must NOT be called
            mock_send.assert_not_called()

            # Outbox directory should contain exactly 1 JSON file
            files = [f for f in os.listdir(self.tmp_dir) if f.endswith(".json")]
            self.assertEqual(len(files), 1)

            file_path = os.path.join(self.tmp_dir, files[0])
            with open(file_path, encoding="utf-8") as f:
                data = json.load(f)

            # Contract Root Object validation
            self.assertEqual(data["schema_version"], "1.0")
            expected_edition_id = f"unagi:digest:unagi-42:daily:{sched_time}"
            self.assertEqual(data["edition_id"], expected_edition_id)
            self.assertEqual(files[0], f"{expected_edition_id}.json")
            self.assertEqual(data["kind"], "digest")
            self.assertEqual(data["recipient"], "unagi-42")
            # Решение 18.10: адрес едет в выпуске (источник правды — unagi), ghsub своего списка для них не ведёт
            self.assertEqual(data["recipient_email"], "user@example.com")
            self.assertIn("Газета", data["title"])
            self.assertEqual(data["period"], "daily")

            # Contract Window object validation
            self.assertIn("window", data)
            window = data["window"]
            self.assertIsInstance(window["from"], str)
            self.assertTrue(window["from"].endswith("Z"))
            self.assertEqual(window["to"], sched_time)
            self.assertEqual(window["timezone"], "Europe/Moscow")

            # manage_url and telegraph_url
            self.assertIsNone(data["telegraph_url"])
            self.assertEqual(data["manage_url"], "https://t.me/test_bot?start=email")

            # Items validation
            items = data["items"]
            self.assertEqual(len(items), 2)
            for item in items:
                self.assertIsInstance(item["post_id"], (int, str))
                self.assertIsInstance(item["published_at"], str)
                self.assertTrue(item["published_at"].endswith("Z"))
                self.assertEqual(item["channel"]["username"], "tech_news")
                self.assertEqual(item["channel"]["title"], "Tech News")
                self.assertIn(item["tier"], ("card", "brief"))
                self.assertIsInstance(item["title"], str)
                self.assertIsInstance(item["summary"], str)
                self.assertTrue(item["url"].startswith("https://") or item["url"].startswith("http://"))
                self.assertIsInstance(item["metrics"]["views"], int)
                self.assertIsInstance(item["metrics"]["forwards"], int)

    def test_tg1_tier_partitioning_up_to_3_cards(self):
        """TG1 tier partitioning produces up to 3 cards per channel, with remainder as brief."""
        sections = self._sample_sections(5)
        sched_time = "2026-10-18T07:00:00Z"

        payload = build_ghsub_edition(
            sections=sections,
            title_plain="Газета",
            user_id=42,
            scheduled_at=sched_time,
        )

        items = payload["items"]
        self.assertEqual(len(items), 5)

        cards = [it for it in items if it["tier"] == "card"]
        briefs = [it for it in items if it["tier"] == "brief"]

        self.assertEqual(len(cards), 3)
        self.assertEqual(len(briefs), 2)

    def test_deterministic_edition_id_on_repeat(self):
        """Repeating the same delivery produces identical edition_id and identical file."""
        sections = self._sample_sections(2)
        sched_time = "2026-10-18T07:00:00Z"
        env = {
            "EMAIL_DELIVERY": "ghsub",
            "EMAIL_OUTBOX_DIR": self.tmp_dir,
            "BOT_USERNAME": "test_bot",
        }
        with mock.patch.dict(os.environ, env):
            reset_settings()
            path1 = write_ghsub_outbox(
                sections=sections,
                title_plain="Газета",
                user_id=42,
                scheduled_at=sched_time,
            )
            with open(path1, encoding="utf-8") as f:
                data1 = json.load(f)

            # Repeat write
            path2 = write_ghsub_outbox(
                sections=sections,
                title_plain="Газета",
                user_id=42,
                scheduled_at=sched_time,
            )
            with open(path2, encoding="utf-8") as f:
                data2 = json.load(f)

            self.assertEqual(path1, path2)
            self.assertEqual(data1["edition_id"], data2["edition_id"])
            self.assertEqual(data1, data2)

    def test_atomic_write_no_tmp_after_success(self):
        """Writing uses atomic rename; no .tmp files remain after successful write."""
        sections = self._sample_sections(1)
        sched_time = "2026-10-18T07:00:00Z"

        target_path = write_ghsub_outbox(
            sections=sections,
            title_plain="Газета",
            user_id=42,
            scheduled_at=sched_time,
            outbox_dir=self.tmp_dir,
        )

        self.assertTrue(os.path.exists(target_path))
        # Ensure no .tmp files exist
        all_files = os.listdir(self.tmp_dir)
        tmp_files = [f for f in all_files if ".tmp" in f]
        self.assertEqual(tmp_files, [])

    def test_atomic_write_cleans_up_tmp_on_failure(self):
        """When an error occurs during writing, the temporary file is cleaned up."""
        sections = self._sample_sections(1)

        with mock.patch("ghsub_outbox.json.dump", side_effect=OSError("Disk full")):
            with self.assertRaises(OSError):
                write_ghsub_outbox(
                    sections=sections,
                    title_plain="Газета",
                    user_id=42,
                    scheduled_at="2026-10-18T07:00:00Z",
                    outbox_dir=self.tmp_dir,
                )

        all_files = os.listdir(self.tmp_dir)
        tmp_files = [f for f in all_files if ".tmp" in f]
        self.assertEqual(tmp_files, [])

    def test_text_cleaning_tx1_and_html_stripping(self):
        """Texts pass TX1 cleaning, have HTML stripped, and all links are http(s)."""
        sections = [
            {
                "sub_id": 1,
                "user_id": 42,
                "period": "daily",
                "username": "dirty_chan",
                "title": "<b>Канал</b> &amp; Co",
                "posts": [
                    {
                        "id": 1,
                        "text": "⚡️⚡️ ВАЖНО! ⚡️⚡️\nПервая строка. Вторая <b>жирная</b> &amp; важная.",
                        "link": "javascript:alert(1)",
                        "time": "2026-10-18 06:00:00",
                    },
                    {
                        "id": 2,
                        "text": "https://example.com/some/article",
                        "link": "https://t.me/dirty_chan/2",
                        "time": "2026-10-18 06:10:00",
                    },
                ],
            }
        ]

        payload = build_ghsub_edition(
            sections=sections,
            title_plain="<h1>Спецвыпуск</h1>",
            user_id=42,
            scheduled_at="2026-10-18T07:00:00Z",
        )

        # Title of edition must not have HTML
        self.assertNotIn("<h1>", payload["title"])
        self.assertIn("Спецвыпуск", payload["title"])

        # Channel title must not have HTML
        self.assertEqual(payload["items"][0]["channel"]["title"], "Канал & Co")

        # Items text must not have HTML
        for item in payload["items"]:
            self.assertNotIn("<b>", item["title"])
            self.assertNotIn("</b>", item["title"])
            self.assertNotIn("<b>", item["summary"])
            self.assertNotIn("</b>", item["summary"])
            # All URLs must be http(s)
            self.assertTrue(item["url"].startswith("https://") or item["url"].startswith("http://"))
            self.assertNotIn("javascript:", item["url"])

        # First post had javascript: link, should fallback to channel post link
        self.assertEqual(payload["items"][0]["url"], "https://t.me/dirty_chan/1")

    def test_bot_username_missing_omits_manage_url(self):
        """When BOT_USERNAME is unset/empty, manage_url key is omitted from JSON."""
        sections = self._sample_sections(1)
        env = {
            "EMAIL_DELIVERY": "ghsub",
            "EMAIL_OUTBOX_DIR": self.tmp_dir,
            "BOT_USERNAME": "",
        }
        with mock.patch.dict(os.environ, env), mock.patch.object(main, "BOT_USERNAME", ""):
            reset_settings()
            payload = build_ghsub_edition(
                sections=sections,
                title_plain="Газета",
                user_id=42,
                scheduled_at="2026-10-18T07:00:00Z",
            )
            self.assertNotIn("manage_url", payload)

    def test_deliver_digest_moves_cursors_with_ghsub(self):
        """Integration: deliver_digest succeeds via ghsub and does not trigger telegram fallback."""
        sections = self._sample_sections(1)
        env = {
            "EMAIL_DELIVERY": "ghsub",
            "EMAIL_OUTBOX_DIR": self.tmp_dir,
            "BOT_USERNAME": "test_bot",
        }
        with mock.patch.dict(os.environ, env), mock.patch.object(main, "BOT_USERNAME", "test_bot"):
            reset_settings()
            with mock.patch.object(main, "get_email_delivery_periods", return_value=("user@example.com", {"daily"})), \
                 mock.patch.object(main, "send_telegram_digest") as mock_tg:

                ok = asyncio.run(
                    main.deliver_digest(42, "Газета", sections, scheduled_at="2026-10-18T07:00:00Z")
                )
                self.assertTrue(ok)
                # Telegram delivery should NOT be triggered for successful email delivery
                mock_tg.assert_not_called()

                # Outbox file should exist
                files = [f for f in os.listdir(self.tmp_dir) if f.endswith(".json")]
                self.assertEqual(len(files), 1)

    def test_recipient_email_written_only_for_a_clean_single_line_address(self):
        """Адрес с переводом строки/пробелом/двумя @ в выпуск не попадает (защита от инъекции заголовков письма в ghsub)."""
        from ghsub_outbox import build_ghsub_edition

        sections = self._sample_sections(1)
        for bad in ("a@b.c\nBcc: x@y.z", "a b@c.de", "a@@b.cd", "plain", ""):
            data = build_ghsub_edition(sections, "Газета", user_id=7, address=bad, scheduled_at="2026-10-18T07:00:00Z")
            self.assertNotIn("recipient_email", data, repr(bad))
        data = build_ghsub_edition(sections, "Газета", user_id=7, address="ok@example.com", scheduled_at="2026-10-18T07:00:00Z")
        self.assertEqual(data["recipient_email"], "ok@example.com")

    def test_strip_html_helper(self):
        """Verify strip_html helper."""
        self.assertEqual(strip_html("<b>hello</b> &amp; <i>world</i>"), "hello & world")
        self.assertEqual(strip_html("plain text"), "plain text")
        self.assertEqual(strip_html(""), "")
        self.assertEqual(strip_html(None), "")

    def test_format_iso8601_helper(self):
        """Verify format_iso8601 helper formats correctly in UTC with 'Z'."""
        dt_utc = datetime(2026, 10, 18, 7, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(format_iso8601(dt_utc), "2026-10-18T07:00:00Z")

        # String format
        self.assertEqual(format_iso8601("2026-10-18 07:00:00"), "2026-10-18T07:00:00Z")
        self.assertEqual(format_iso8601("2026-10-18T07:00:00Z"), "2026-10-18T07:00:00Z")


if __name__ == "__main__":
    unittest.main()
