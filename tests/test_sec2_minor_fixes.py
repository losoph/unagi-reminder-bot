"""SEC2 — регрессионные тесты по минорным пунктам 11–14 ревью безопасности.

Проверяют уже закрытые дефекты из docs/reviews/2026-10-16-security-dsh.md:
импорт произвольного saved_at, сброс попыток кода почты, пересоздание
уникального индекса подписок и чтение всего backlog постов.
"""

import os

os.environ.setdefault("BOT_TOKEN", "123456:TEST_TOKEN_FOR_UNIT_TESTS")

import asyncio  # noqa: E402
import sqlite3  # noqa: E402
import tempfile  # noqa: E402
import unittest  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402
from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

import data.database as db  # noqa: E402
import main  # noqa: E402

UTC = timezone.utc
INDEX_NAME = "uq_subscriptions_user_channel_period_active"


def _now_str() -> str:
    return db.serialize_datetime(db.utc_now())


class Sec2TestBase(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp_dir.name, "sec2.db")
        self.orig_db_path = db.DB_PATH
        db.DB_PATH = self.db_path
        db.init_db()

    def tearDown(self):
        db.DB_PATH = self.orig_db_path
        self.tmp_dir.cleanup()

    def _add_sub(
        self,
        user_id,
        username="chan_one",
        period="daily",
        last_scraped=None,
        next_send=None,
    ):
        return db.add_subscription(
            user_id,
            username,
            f"Канал {username}",
            period,
            last_scraped or _now_str(),
            next_send or _now_str(),
        )


class TestP11SavedAtImport(Sec2TestBase):
    """П.11 — импорт не сохраняет произвольный saved_at, карточка открывается."""

    def test_invalid_saved_at_is_replaced_and_card_opens(self):
        user = 11001
        before = db.utc_now()
        report = main._import_user_payload(
            user,
            {
                "bookmarks": [
                    {
                        "full_text": "текст закладки",
                        "source_name": "Импорт",
                        "tag": "тег",
                        "saved_at": "not-a-date",
                    }
                ]
            },
        )
        after = db.utc_now()

        self.assertIn("Закладки: +1", report)
        rows = db.get_saved_messages(user)
        self.assertEqual(len(rows), 1)
        parsed = db.parse_db_datetime(rows[0]["saved_at"])
        self.assertGreaterEqual(parsed, before - timedelta(seconds=1))
        self.assertLessEqual(parsed, after + timedelta(seconds=1))

        # Именно открытие карточки падало на display_db_datetime(saved_at).
        target = MagicMock()
        target.answer = AsyncMock()
        state = AsyncMock()
        asyncio.run(main.render_kb_detail_screen(user, rows[0]["id"], target, state))

        target.answer.assert_awaited_once()
        text = target.answer.call_args.args[0]
        self.assertIn("Сохранено", text)
        self.assertIn("текст закладки", text)

    def test_valid_saved_at_is_preserved(self):
        user = 11002
        main._import_user_payload(
            user,
            {
                "bookmarks": [
                    {
                        "full_text": "старая закладка",
                        "saved_at": "2020-01-02 03:04:05",
                    }
                ]
            },
        )
        rows = db.get_saved_messages(user)
        self.assertEqual(rows[0]["saved_at"], "2020-01-02 03:04:05")


class TestP12EmailAttempts(Sec2TestBase):
    """П.12 — переотправка кода не открывает новый лимит попыток."""

    @staticmethod
    def _start(user_id, code, now):
        db.start_email_verification(
            user_id,
            "a@example.com",
            code,
            db.serialize_datetime(now),
            db.serialize_datetime(now + timedelta(minutes=15)),
        )

    @staticmethod
    def _confirm(user_id, code, now):
        return db.confirm_email_code(user_id, code, db.serialize_datetime(now))

    def test_resend_does_not_reset_attempts_or_unlock_correct_code(self):
        user = 12001
        now = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
        self._start(user, "111111", now)

        for _ in range(db.EMAIL_CODE_MAX_ATTEMPTS):
            self.assertEqual(self._confirm(user, "000000", now), "invalid")
        self.assertEqual(db.get_user_email(user)["code_attempts"], db.EMAIL_CODE_MAX_ATTEMPTS)

        # Даже верный старый код после исчерпания лимита не принимается.
        self.assertEqual(self._confirm(user, "111111", now + timedelta(minutes=1)), "too_many")

        # Переотправка меняет код, но не счётчик: правильный новый код тоже
        # заблокирован и до, и после истечения срока действия кода.
        self._start(user, "222222", now + timedelta(minutes=2))
        row = db.get_user_email(user)
        self.assertEqual(row["verify_code"], "222222")
        self.assertEqual(row["code_attempts"], db.EMAIL_CODE_MAX_ATTEMPTS)
        self.assertEqual(self._confirm(user, "222222", now + timedelta(minutes=3)), "too_many")
        self.assertEqual(self._confirm(user, "222222", now + timedelta(minutes=30)), "too_many")

    def test_successful_confirmation_is_the_only_attempt_reset(self):
        user = 12002
        now = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
        self._start(user, "111111", now)
        self.assertEqual(self._confirm(user, "000000", now), "invalid")
        self.assertEqual(db.get_user_email(user)["code_attempts"], 1)

        self._start(user, "222222", now + timedelta(minutes=1))
        self.assertEqual(db.get_user_email(user)["code_attempts"], 1)
        self.assertEqual(self._confirm(user, "222222", now + timedelta(minutes=2)), "ok")
        self.assertEqual(db.get_user_email(user)["code_attempts"], 0)

        self._start(user, "333333", now + timedelta(minutes=3))
        self.assertEqual(db.get_user_email(user)["code_attempts"], 0)


class TestP13SubscriptionIndex(Sec2TestBase):
    """П.13 — индекс чинится только при расхождении, add_subscription без дублей."""

    def _index_row(self):
        with sqlite3.connect(self.db_path) as conn:
            return conn.execute(
                "SELECT sql, rootpage FROM sqlite_master "
                "WHERE type = 'index' AND name = ?",
                (INDEX_NAME,),
            ).fetchone()

    def test_repeated_init_db_does_not_recreate_index(self):
        before = self._index_row()
        self.assertIsNotNone(before)
        with sqlite3.connect(self.db_path) as conn:
            schema_before = conn.execute("PRAGMA schema_version").fetchone()[0]

        db.init_db()

        after = self._index_row()
        with sqlite3.connect(self.db_path) as conn:
            schema_after = conn.execute("PRAGMA schema_version").fetchone()[0]
        self.assertEqual(after[0], before[0])
        self.assertEqual(after[1], before[1])
        self.assertEqual(schema_after, schema_before)

    def test_wrong_index_definition_is_repaired(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(f"DROP INDEX {INDEX_NAME}")
            conn.execute(
                f"CREATE INDEX {INDEX_NAME} ON subscriptions(user_id)"
            )
            conn.commit()

        db.init_db()

        row = self._index_row()
        sql = " ".join((row[0] or "").split())
        self.assertIn("UNIQUE", sql.upper())
        self.assertIn("channel_username", sql)
        self.assertIn("period", sql)
        self.assertIn("WHERE is_disabled = 0", sql)
        with sqlite3.connect(self.db_path) as conn:
            entries = conn.execute("PRAGMA index_list(subscriptions)").fetchall()
        entry = next(item for item in entries if item[1] == INDEX_NAME)
        self.assertEqual(entry[2], 1)  # unique flag

    def test_sequential_add_subscription_does_not_create_duplicate(self):
        user = 13001
        first = self._add_sub(user, "chan_dup")
        second = self._add_sub(user, "chan_dup")
        self.assertEqual(first, second)
        self.assertEqual(db.count_user_subscriptions(user), 1)
        with sqlite3.connect(self.db_path) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM subscriptions "
                "WHERE user_id = ? AND channel_username = ? AND period = ? AND is_disabled = 0",
                (user, "chan_dup", "daily"),
            ).fetchone()[0]
        self.assertEqual(count, 1)


class TestP14ChannelPostsLimit(Sec2TestBase):
    """П.14 — выборка ограничена батчем, порядок и признак backlog сохранены."""

    def _insert_posts(self, channel, count, now):
        posts = [
            {
                "id": i,
                "text": f"p{i}",
                "link": f"https://t.me/{channel}/{i}",
                "time": now + timedelta(minutes=i),
            }
            for i in range(1, count + 1)
        ]
        db.upsert_channel_posts(channel, posts, "test")
        return posts

    def test_default_limit_returns_first_batch_in_order_for_both_cursors(self):
        now = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
        marker = db.serialize_datetime(now - timedelta(days=1))
        self._insert_posts("limit_chan", 7, now)

        with patch.object(db, "CHANNEL_DELIVERY_BATCH_SIZE", 3):
            by_time = db.get_channel_posts_since("limit_chan", marker)
            by_post_id = db.get_channel_posts_since("limit_chan", marker, last_post_id=0)
            next_page = db.get_channel_posts_since("limit_chan", marker, last_post_id=3)

        self.assertEqual([post["id"] for post in by_time], [1, 2, 3])
        self.assertEqual([post["id"] for post in by_post_id], [1, 2, 3])
        self.assertEqual([post["id"] for post in next_page], [4, 5, 6])

    def test_main_keeps_batch_and_backlog_flag_with_small_batch(self):
        user = 14001
        now = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
        marker = db.serialize_datetime(now - timedelta(days=1))
        posts = self._insert_posts("limit_chan", 7, now)
        sub_id = self._add_sub(user, "limit_chan", last_scraped=marker)
        prefetched = {
            "limit_chan": {"posts": posts, "source": "test", "error": None}
        }

        def fetch(last_scraped, last_post_id):
            sub_tuple = (
                sub_id,
                user,
                "limit_chan",
                "Limit Chan",
                "daily",
                last_scraped,
                0,
                last_post_id,
            )
            return asyncio.run(
                main.fetch_subscription_posts(
                    sub_tuple,
                    session=object(),
                    semaphore=asyncio.Semaphore(1),
                    now_str=_now_str(),
                    prefetched_channels=prefetched,
                )
            )

        with patch.object(main, "CHANNEL_DELIVERY_BATCH_SIZE", 3):
            first = fetch(marker, None)
            self.assertEqual([post["id"] for post in first["posts"]], [1, 2, 3])
            self.assertTrue(first["has_backlog"])
            self.assertTrue(first["is_partial"])
            self.assertEqual(first["last_post_id"], 3)

            second = fetch(first["last_scraped_str"], first["last_post_id"])
            self.assertEqual([post["id"] for post in second["posts"]], [4, 5, 6])
            self.assertTrue(second["has_backlog"])
            self.assertEqual(second["last_post_id"], 6)

            third = fetch(second["last_scraped_str"], second["last_post_id"])
            self.assertEqual([post["id"] for post in third["posts"]], [7])
            self.assertFalse(third["has_backlog"])
            self.assertFalse(third["is_partial"])
            self.assertEqual(third["last_post_id"], 7)


if __name__ == "__main__":
    unittest.main()
