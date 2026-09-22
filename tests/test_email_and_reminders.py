import asyncio
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from unittest import mock

os.environ.setdefault("BOT_TOKEN", "123456:TEST_TOKEN_FOR_UNIT_TESTS")

import email_digest  # noqa: E402
import main  # noqa: E402
from data import database  # noqa: E402

SECTIONS = [
    {
        "title": "Канал <Тест> & Co",
        "posts": [
            {"text": "Первое предложение. Второе <b>не тег</b>.", "link": "https://t.me/example/1"},
            {"text": "Без ссылки", "link": "javascript:alert(1)"},
        ],
    },
    {"title": "Второй", "posts": [{"text": "Пост", "link": "https://t.me/second/7"}]},
]


class TagBalanceChecker(HTMLParser):
    VOID = {"meta", "hr", "br", "img"}

    def __init__(self):
        super().__init__()
        self.stack: list[str] = []
        self.errors: list[str] = []
        self.hrefs: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.hrefs.append(dict(attrs).get("href"))
        if tag not in self.VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack.pop() != tag:
            self.errors.append(tag)


class DigestEmailRenderingTests(unittest.TestCase):
    def render(self, **kwargs):
        return email_digest.build_digest_email("Газета <1>", SECTIONS, **kwargs)

    def test_html_is_well_formed(self):
        html_body, _ = self.render(telegraph_url="https://telegra.ph/x", manage_url="https://t.me/bot?start=em_off")
        checker = TagBalanceChecker()
        checker.feed(html_body)
        checker.close()
        self.assertEqual(checker.errors, [])
        self.assertEqual(checker.stack, [])
        self.assertTrue(html_body.startswith("<!doctype html>"))

    def test_mirrors_telegraph_layout(self):
        html_body, _ = self.render()
        # Summary line, per-channel heading, bold first sentence and «Читать» link — as on Telegraph.
        self.assertIn("2 канала · 3 поста", html_body)
        self.assertIn("<h4", html_body)
        self.assertIn("<b>Первое предложение.</b>", html_body)
        self.assertIn('href="https://t.me/example/1"', html_body)
        self.assertIn(">Читать</a>", html_body)

    def test_escapes_text_and_drops_unsafe_links(self):
        html_body, _ = self.render()
        self.assertIn("Канал &lt;Тест&gt; &amp; Co", html_body)
        self.assertIn("&lt;b&gt;не тег&lt;/b&gt;", html_body)
        self.assertIn("Газета &lt;1&gt;", html_body)
        self.assertNotIn("javascript:", html_body)

    def test_telegraph_and_manage_links(self):
        html_body, text_body = self.render(telegraph_url="https://telegra.ph/x", manage_url="https://t.me/b?start=em_off")
        checker = TagBalanceChecker()
        checker.feed(html_body)
        self.assertIn("https://telegra.ph/x", checker.hrefs)
        self.assertIn("https://t.me/b?start=em_off", checker.hrefs)
        self.assertIn("Открыть в Telegraph: https://telegra.ph/x", text_body)
        self.assertIn("- Пост\n  https://t.me/second/7", text_body)


class PluralTests(unittest.TestCase):
    def test_russian_plural_forms(self):
        from telegraph_publisher import describe_counts

        self.assertEqual(describe_counts(1, 21), "1 канал · 21 пост")
        self.assertEqual(describe_counts(3, 12), "3 канала · 12 постов")
        self.assertEqual(describe_counts(5, 104), "5 каналов · 104 поста")


class SmtpConfigTests(unittest.TestCase):
    def load(self, rc_text=None, **env):
        with tempfile.NamedTemporaryFile("w", suffix="rc", delete=False) as fh:
            fh.write(rc_text or "")
        clean = {k: "" for k in ("SMTP_HOST", "SMTP_PORT", "SMTP_SECURITY", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM")}
        clean.update(env)
        try:
            with mock.patch.dict(os.environ, clean), mock.patch.object(
                email_digest, "MSMTPRC_PATH", fh.name if rc_text else "/nonexistent"
            ):
                return email_digest.load_config()
        finally:
            os.unlink(fh.name)

    def test_msmtprc_default_account(self):
        config = self.load(
            "defaults\nauth on\ntls on\n\naccount other\nhost other.example\n\n"
            "account mail\nhost smtp.example.com\nport 465\ntls_starttls off\nuser bot@example.com\n"
            "password secret\nfrom alerts@example.com\n\naccount default : mail\n"
        )
        self.assertEqual(
            (config.host, config.port, config.security, config.user, config.password, config.sender),
            ("smtp.example.com", 465, "ssl", "bot@example.com", "secret", "alerts@example.com"),
        )

    def test_env_overrides_msmtprc(self):
        config = self.load("account a\nhost smtp.example.com\ntls on\nfrom a@example.com\n", SMTP_FROM="b@example.com")
        self.assertEqual((config.security, config.port, config.sender), ("starttls", 587, "b@example.com"))

    def test_env_only_never_defaults_to_plaintext(self):
        config = self.load(SMTP_HOST="smtp.example.com", SMTP_USER="u@example.com", SMTP_PASSWORD="p")
        self.assertEqual((config.security, config.port, config.sender), ("starttls", 587, "u@example.com"))
        config = self.load(SMTP_HOST="smtp.example.com", SMTP_PORT="465", SMTP_FROM="u@example.com")
        self.assertEqual(config.security, "ssl")

    def test_disabled_without_host(self):
        self.assertIsNone(self.load())

    def test_send_uses_starttls_and_login(self):
        config = email_digest.SmtpConfig("smtp.example.com", 587, "starttls", "u", "p", "bot@example.com")
        with mock.patch.object(email_digest, "_CONFIG", config), mock.patch("email_digest.smtplib.SMTP") as smtp:
            ok = asyncio.run(email_digest.send_email("to@example.com", "Тема", "text", "<p>html</p>"))
        self.assertTrue(ok)
        server = smtp.return_value
        server.starttls.assert_called_once()
        server.login.assert_called_once_with("u", "p")
        message = server.send_message.call_args.args[0]
        self.assertEqual(message["To"], "to@example.com")
        self.assertEqual([part.get_content_type() for part in message.iter_parts()], ["text/plain", "text/html"])

    def test_send_failure_returns_false(self):
        config = email_digest.SmtpConfig("smtp.example.com", 465, "ssl", "", "", "bot@example.com")
        with mock.patch.object(email_digest, "_CONFIG", config), mock.patch(
            "email_digest.smtplib.SMTP_SSL", side_effect=OSError("down")
        ):
            self.assertFalse(asyncio.run(email_digest.send_email("to@example.com", "s", "t")))

    def test_normalize_email(self):
        self.assertEqual(email_digest.normalize_email(" Name.Tag+x@Example.COM "), "Name.Tag+x@example.com")
        for bad in ["", "name", "a@b", "a b@example.com", "a@example.com\nBcc: x@y.z"]:
            self.assertIsNone(email_digest.normalize_email(bad), bad)


class DatabaseTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = os.path.join(self.temp_dir.name, "test.db")
        database.init_db()

    def tearDown(self):
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()


class EmailVerificationTests(DatabaseTestCase):
    NOW = datetime(2026, 9, 22, 12, tzinfo=timezone.utc)

    def start(self, email="a@example.com", code="123456"):
        database.start_email_verification(
            1,
            email,
            code,
            database.serialize_datetime(self.NOW),
            database.serialize_datetime(self.NOW + timedelta(minutes=15)),
        )

    def confirm(self, code, minutes=1):
        return database.confirm_email_code(1, code, database.serialize_datetime(self.NOW + timedelta(minutes=minutes)))

    def test_happy_path_confirms_address(self):
        self.start()
        self.assertEqual(database.get_email_delivery(1), (None, set()))
        self.assertEqual(self.confirm("000000"), "invalid")
        self.assertEqual(self.confirm(" 123456 "), "ok")
        # Confirming does not move any digest away from Telegram by itself.
        self.assertEqual(database.get_email_delivery(1), ("a@example.com", set()))
        self.assertEqual(self.confirm("123456"), "none")

    def test_expired_and_attempt_limit(self):
        self.start()
        self.assertEqual(self.confirm("123456", minutes=16), "expired")
        for _ in range(database.EMAIL_CODE_MAX_ATTEMPTS):
            self.assertEqual(self.confirm("999999"), "invalid")
        self.assertEqual(self.confirm("123456"), "too_many")

    def test_changing_address_keeps_old_and_periods_until_confirmed(self):
        self.start()
        self.confirm("123456")
        database.set_email_period(1, "daily", True)
        self.start(email="b@example.com", code="654321")
        self.assertEqual(database.get_email_delivery(1), ("a@example.com", {"daily"}))
        self.assertEqual(self.confirm("654321"), "ok")
        self.assertEqual(database.get_email_delivery(1), ("b@example.com", {"daily"}))

    def test_period_toggles_clear_and_delete(self):
        self.assertFalse(database.set_email_period(1, "daily", True))  # nothing confirmed yet
        self.start()
        self.confirm("123456")
        database.set_email_period(1, "daily", True)
        database.set_email_period(1, "monthly", True)
        database.set_email_period(1, "daily", False)
        self.assertEqual(database.get_email_delivery(1), ("a@example.com", {"monthly"}))
        self.assertTrue(database.clear_email_periods(1))
        self.assertFalse(database.clear_email_periods(1))
        self.assertEqual(database.get_email_delivery(1), ("a@example.com", set()))
        database.delete_user_email(1)
        self.assertIsNone(database.get_user_email(1))

    def test_legacy_table_gets_period_column(self):
        with database.get_connection() as conn:
            conn.execute("DROP TABLE user_emails")
            conn.execute("CREATE TABLE user_emails (user_id INTEGER PRIMARY KEY, email TEXT, is_enabled INTEGER)")
            conn.execute("INSERT INTO user_emails (user_id, email, is_enabled) VALUES (1, 'a@example.com', 1)")
            conn.commit()
        database.init_db()
        self.assertEqual(database.get_email_delivery(1), ("a@example.com", set()))


class DigestRoutingTests(unittest.TestCase):
    """Each period goes either to email or to Telegram; a failed email falls back to Telegram."""

    SECTIONS = [
        {"period": "daily", "title": "D", "posts": [{"text": "d", "link": "https://t.me/d/1"}]},
        {"period": "weekly", "title": "W", "posts": [{"text": "w", "link": "https://t.me/w/1"}]},
        {"period": "monthly", "title": "M", "posts": []},
    ]

    def run_delivery(self, email_periods, email_ok=True):
        email = mock.AsyncMock(return_value=email_ok)
        telegram = mock.AsyncMock()
        with mock.patch.object(main, "get_email_delivery_periods", return_value=("a@example.com", email_periods)), \
                mock.patch.object(main, "send_digest_email", email), \
                mock.patch.object(main, "send_telegram_digest", telegram):
            delivered = asyncio.run(main.deliver_digest(1, "Газета", [dict(s) for s in self.SECTIONS]))
        return delivered, email, telegram

    @staticmethod
    def titles(sections):
        return [s["title"] for s in sections]

    def test_no_email_everything_to_telegram(self):
        delivered, email, telegram = self.run_delivery(set())
        self.assertTrue(delivered)
        email.assert_not_called()
        self.assertEqual(self.titles(telegram.call_args.args[2]), ["D", "W"])

    def test_email_period_is_not_sent_to_telegram(self):
        _, email, telegram = self.run_delivery({"daily"})
        self.assertEqual(self.titles(email.call_args.args[2]), ["D"])
        self.assertEqual(self.titles(telegram.call_args.args[2]), ["W"])

    def test_all_email_sends_nothing_to_telegram(self):
        _, email, telegram = self.run_delivery({"daily", "weekly"})
        self.assertEqual(self.titles(email.call_args.args[2]), ["D", "W"])
        telegram.assert_not_called()

    def test_failed_email_falls_back_to_telegram_with_note(self):
        _, _, telegram = self.run_delivery({"daily"}, email_ok=False)
        self.assertEqual(self.titles(telegram.call_args.args[2]), ["W", "D"])
        self.assertIn("почту", telegram.call_args.args[3])

    def test_empty_digest_is_not_delivered(self):
        with mock.patch.object(main, "get_email_delivery_periods") as periods:
            self.assertFalse(asyncio.run(main.deliver_digest(1, "Газета", [self.SECTIONS[2]])))
        periods.assert_not_called()


class RescheduleKeepsTagTests(DatabaseTestCase):
    def test_delivered_reminder_keeps_its_folder(self):
        database.add_message(1, None, "2026-09-22 10:00:00", "текст", "источник", "Работа")
        with database.get_connection() as conn:
            row_id = conn.execute("SELECT id FROM scheduled_messages").fetchone()[0]
        database.mark_as_sent(row_id, delivered_message_id=500)
        database.replace_sent_reminder_with_pending(1, row_id, None, "2026-09-23 06:00:00", "текст", "источник")
        with database.get_connection() as conn:
            rows = conn.execute("SELECT tag, is_sent, send_at FROM scheduled_messages").fetchall()
        self.assertEqual([tuple(r) for r in rows], [("Работа", 0, "2026-09-23 06:00:00")])

    def test_missing_row_inserts_nothing(self):
        with self.assertRaises(ValueError):
            database.replace_sent_reminder_with_pending(1, 999, None, "2026-09-23 06:00:00")
        with database.get_connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM scheduled_messages").fetchone()[0], 0)


class QuickTimeTests(unittest.TestCase):
    def at(self, day, hour, minute=0):
        return datetime(2026, 9, day, hour, minute, tzinfo=main.TZ)

    def test_day_parts_pick_nearest_moment(self):
        early = self.at(22, 2)
        self.assertEqual(main.get_quick_scheduled_time("morning", early)[0], self.at(22, 9))
        late = self.at(22, 21, 30)
        self.assertEqual(main.get_quick_scheduled_time("morning", late), (self.at(23, 9), "на завтра в 09:00"))
        self.assertEqual(main.get_quick_scheduled_time("day", late)[0], self.at(23, 14))
        self.assertEqual(main.get_quick_scheduled_time("evening", self.at(22, 19))[0], self.at(22, 20))

    def test_relative_buttons(self):
        now = self.at(22, 10, 15)
        self.assertEqual(main.get_quick_scheduled_time("hour", now), (self.at(22, 11, 15), "через час"))
        self.assertEqual(main.get_quick_scheduled_time("now", now)[0], self.at(22, 13, 15))
        self.assertEqual(main.get_quick_scheduled_time("nope", now), (None, ""))

    def test_hint_line(self):
        self.assertEqual(
            main.describe_quick_times(self.at(22, 15)),
            "🌅 завтра в 09:00 · ☀️ завтра в 14:00 · 🌙 сегодня в 20:00",
        )

    def test_keyboard_callbacks_fit_telegram_limit(self):
        markup = main.build_time_selection_keyboard("senttime_2147483647", back_callback="senttime_2147483647_back")
        for row in markup.inline_keyboard:
            for button in row:
                self.assertLessEqual(len(button.callback_data.encode()), 64)


if __name__ == "__main__":
    unittest.main()
