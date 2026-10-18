import os
import sqlite3
import tempfile
import unittest

from data import database as db


class Q4T20SchemaTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_schema.db")
        self.original_db_path = db.DB_PATH
        db.DB_PATH = self.db_path
        db.init_db()

    def tearDown(self):
        db.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def _get_columns(self, table_name: str) -> dict[str, dict[str, str | None]]:
        with db.get_connection() as conn:
            cursor = conn.execute(f"PRAGMA table_info({table_name})")
            # cid, name, type, notnull, dflt_value, pk
            return {
                row[1]: {
                    "type": (row[2] or "").upper(),
                    "notnull": row[3],
                    "default": row[4],
                    "pk": row[5],
                }
                for row in cursor.fetchall()
            }

    def _get_indexes(self, table_name: str) -> dict[str, dict]:
        with db.get_connection() as conn:
            cursor = conn.execute(f"PRAGMA index_list({table_name})")
            # seq, name, unique, origin, partial
            return {
                row[1]: {
                    "unique": bool(row[2]),
                    "partial": bool(row[4]),
                }
                for row in cursor.fetchall()
            }

    def test_tables_created(self):
        """Таблицы folders, digest_editions, digest_edition_items должны быть созданы."""
        with db.get_connection() as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        self.assertIn("folders", tables)
        self.assertIn("digest_editions", tables)
        self.assertIn("digest_edition_items", tables)

    def test_folders_columns(self):
        """Проверка колонок и дефолтов таблицы folders (§3.1)."""
        cols = self._get_columns("folders")
        expected_cols = [
            "id", "user_id", "name", "is_default", "period", "times",
            "send_hour", "send_minute", "weekdays", "month_day", "monthly_mode",
            "delivery", "is_enabled", "next_send_at", "last_edition_id",
            "created_at", "updated_at",
        ]
        for col in expected_cols:
            self.assertIn(col, cols, f"Колонка {col} отсутствует в folders")

        self.assertEqual(cols["id"]["pk"], 1)
        self.assertEqual(cols["user_id"]["notnull"], 1)
        self.assertEqual(cols["name"]["notnull"], 1)
        self.assertEqual(cols["period"]["notnull"], 1)
        self.assertEqual(cols["delivery"]["notnull"], 1)
        self.assertEqual(cols["is_enabled"]["notnull"], 1)
        self.assertEqual(cols["created_at"]["notnull"], 1)
        self.assertEqual(cols["updated_at"]["notnull"], 1)

    def test_digest_editions_columns(self):
        """Проверка колонок таблицы digest_editions (§3.1)."""
        cols = self._get_columns("digest_editions")
        expected_cols = [
            "id", "folder_id", "user_id", "scheduled_at", "status",
            "delivery", "delivery_target", "posts_count", "channels_count",
            "channels_failed", "channels_partial", "error_message",
            "retry_count", "telegraph_url", "created_at", "started_at", "delivered_at",
        ]
        for col in expected_cols:
            self.assertIn(col, cols, f"Колонка {col} отсутствует в digest_editions")

        self.assertEqual(cols["folder_id"]["notnull"], 1)
        self.assertEqual(cols["user_id"]["notnull"], 1)
        self.assertEqual(cols["scheduled_at"]["notnull"], 1)
        self.assertEqual(cols["status"]["notnull"], 1)
        self.assertEqual(cols["delivery"]["notnull"], 1)
        self.assertEqual(cols["created_at"]["notnull"], 1)

    def test_digest_edition_items_columns(self):
        """Проверка колонок таблицы digest_edition_items (§3.1)."""
        cols = self._get_columns("digest_edition_items")
        expected_cols = [
            "id", "edition_id", "subscription_id", "channel_username",
            "post_id", "post_time", "post_title", "post_text", "post_link",
            "score", "rank", "created_at",
        ]
        for col in expected_cols:
            self.assertIn(col, cols, f"Колонка {col} отсутствует в digest_edition_items")

        self.assertEqual(cols["edition_id"]["notnull"], 1)
        self.assertEqual(cols["subscription_id"]["notnull"], 1)
        self.assertEqual(cols["channel_username"]["notnull"], 1)
        self.assertEqual(cols["post_id"]["notnull"], 1)
        self.assertEqual(cols["post_time"]["notnull"], 1)
        self.assertEqual(cols["post_link"]["notnull"], 1)
        self.assertEqual(cols["created_at"]["notnull"], 1)

    def test_altered_columns_in_existing_tables(self):
        """Проверка добавленных колонок (§3.2): subscriptions.folder_id, user_prefs.digest_scheduler_version и т.д."""
        sub_cols = self._get_columns("subscriptions")
        self.assertIn("folder_id", sub_cols)

        prefs_cols = self._get_columns("user_prefs")
        self.assertIn("digest_scheduler_version", prefs_cols)
        self.assertEqual(prefs_cols["digest_scheduler_version"]["default"], "'v1'")

        exec_cols = self._get_columns("digest_executions")
        self.assertIn("folder_id", exec_cols)

        alert_cols = self._get_columns("digest_alerts")
        self.assertIn("folder_id", alert_cols)

    def test_indexes_created(self):
        """Проверка индексов (§3.1, §3.2)."""
        folder_indexes = self._get_indexes("folders")
        self.assertIn("idx_folders_due", folder_indexes)
        self.assertTrue(folder_indexes["idx_folders_due"]["partial"])
        self.assertIn("idx_folders_user", folder_indexes)

        editions_indexes = self._get_indexes("digest_editions")
        self.assertIn("idx_digest_editions_user_status", editions_indexes)
        self.assertIn("idx_digest_editions_sched_status", editions_indexes)

        items_indexes = self._get_indexes("digest_edition_items")
        self.assertIn("idx_edition_items_edition", items_indexes)
        self.assertIn("idx_edition_items_sub", items_indexes)

        subs_indexes = self._get_indexes("subscriptions")
        self.assertIn("idx_subscriptions_folder", subs_indexes)

        exec_indexes = self._get_indexes("digest_executions")
        self.assertIn("uq_digest_executions_folder_slot", exec_indexes)
        self.assertTrue(exec_indexes["uq_digest_executions_folder_slot"]["unique"])
        self.assertTrue(exec_indexes["uq_digest_executions_folder_slot"]["partial"])

        alert_indexes = self._get_indexes("digest_alerts")
        self.assertIn("uq_digest_alerts_folder_slot", alert_indexes)
        self.assertTrue(alert_indexes["uq_digest_alerts_folder_slot"]["unique"])
        self.assertTrue(alert_indexes["uq_digest_alerts_folder_slot"]["partial"])

    def test_init_db_idempotency(self):
        """Повторный запуск init_db() проходит без ошибок и не повреждает структуру."""
        db.init_db()
        db.init_db()

        # Проверяем, что таблицы и колонки на месте
        cols = self._get_columns("folders")
        self.assertIn("id", cols)
        sub_cols = self._get_columns("subscriptions")
        self.assertIn("folder_id", sub_cols)

    def test_existing_data_preserved_across_init_db(self):
        """Существующие данные в subscriptions и user_prefs сохраняются при повторном init_db."""
        with db.get_connection() as conn:
            conn.execute(
                """
                INSERT INTO user_prefs (user_id, timezone, morning_hour)
                VALUES (42, 'Europe/Moscow', 8)
                """
            )
            conn.execute(
                """
                INSERT INTO subscriptions (user_id, channel_username, channel_title, period, tag, is_disabled)
                VALUES (42, 'mychan', 'My Channel', 'daily', 'news', 0)
                """
            )
            conn.commit()

        # Повторная инициализация
        db.init_db()

        with db.get_connection() as conn:
            prefs_row = conn.execute(
                "SELECT timezone, morning_hour, digest_scheduler_version FROM user_prefs WHERE user_id = 42"
            ).fetchone()
            self.assertIsNotNone(prefs_row)
            self.assertEqual(prefs_row["timezone"], "Europe/Moscow")
            self.assertEqual(prefs_row["morning_hour"], 8)
            self.assertEqual(prefs_row["digest_scheduler_version"], "v1")

            sub_row = conn.execute(
                "SELECT channel_username, tag, period FROM subscriptions WHERE user_id = 42"
            ).fetchone()
            self.assertIsNotNone(sub_row)
            self.assertEqual(sub_row["channel_username"], "mychan")
            self.assertEqual(sub_row["tag"], "news")

    def test_folders_check_constraints(self):
        """Проверка CHECK-ограничений в таблице folders."""
        with db.get_connection() as conn:
            # 1. Недопустимый period
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    """
                    INSERT INTO folders (user_id, name, period, created_at, updated_at)
                    VALUES (1, 'BadPeriod', 'hourly', '2026-10-18 00:00:00', '2026-10-18 00:00:00')
                    """
                )

            # 2. Недопустимый send_hour (> 23)
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    """
                    INSERT INTO folders (user_id, name, period, send_hour, created_at, updated_at)
                    VALUES (1, 'BadHour', 'daily', 24, '2026-10-18 00:00:00', '2026-10-18 00:00:00')
                    """
                )

            # 3. Недопустимый send_minute (> 59)
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    """
                    INSERT INTO folders (user_id, name, period, send_minute, created_at, updated_at)
                    VALUES (1, 'BadMin', 'daily', 60, '2026-10-18 00:00:00', '2026-10-18 00:00:00')
                    """
                )

            # 4. Недопустимый monthly_mode
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    """
                    INSERT INTO folders (user_id, name, period, monthly_mode, created_at, updated_at)
                    VALUES (1, 'BadMode', 'monthly', 'yearly', '2026-10-18 00:00:00', '2026-10-18 00:00:00')
                    """
                )

            # 5. Недопустимый delivery
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    """
                    INSERT INTO folders (user_id, name, period, delivery, created_at, updated_at)
                    VALUES (1, 'BadDelivery', 'daily', 'sms', '2026-10-18 00:00:00', '2026-10-18 00:00:00')
                    """
                )

    def test_foreign_keys_not_enabled_globally(self):
        """PRAGMA foreign_keys в get_connection() НЕ включены глобально (решение T20)."""
        with db.get_connection() as conn:
            fk_state = conn.execute("PRAGMA foreign_keys").fetchone()[0]
            self.assertEqual(fk_state, 0, "foreign_keys не должны быть включены глобально")

    def test_foreign_keys_cascade_and_restrict_when_enabled(self):
        """При явном включении FK: CASCADE на editions/items и RESTRICT на subscriptions."""
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("PRAGMA foreign_keys = ON")

            # Создаём папку
            conn.execute(
                """
                INSERT INTO folders (user_id, name, period, created_at, updated_at)
                VALUES (1, 'TestFK', 'daily', '2026-10-18 00:00:00', '2026-10-18 00:00:00')
                """
            )
            folder_id = conn.execute("SELECT id FROM folders WHERE name = 'TestFK'").fetchone()[0]

            # Создаём подписку с folder_id
            conn.execute(
                """
                INSERT INTO subscriptions (user_id, channel_username, period, folder_id, is_disabled)
                VALUES (1, 'fk_chan', 'daily', ?, 0)
                """,
                (folder_id,),
            )
            sub_id = conn.execute("SELECT id FROM subscriptions WHERE channel_username = 'fk_chan'").fetchone()[0]

            # Создаём выпуск (digest_editions)
            conn.execute(
                """
                INSERT INTO digest_editions (folder_id, user_id, scheduled_at, status, created_at)
                VALUES (?, 1, '2026-10-18 08:00:00', 'ready', '2026-10-18 08:00:00')
                """,
                (folder_id,),
            )
            edition_id = conn.execute("SELECT id FROM digest_editions WHERE folder_id = ?", (folder_id,)).fetchone()[0]

            # Создаём элемент выпуска (digest_edition_items)
            conn.execute(
                """
                INSERT INTO digest_edition_items (edition_id, subscription_id, channel_username, post_id, post_time, post_link, created_at)
                VALUES (?, ?, 'fk_chan', 101, '2026-10-18 07:00:00', 'https://t.me/fk_chan/101', '2026-10-18 08:00:00')
                """,
                (edition_id, sub_id),
            )
            conn.commit()

            # 1. Попытка удалить folder при наличии subscriptions с RESTRICT должна упасть с IntegrityError
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("DELETE FROM folders WHERE id = ?", (folder_id,))

            # 2. Если отвязать subscription, удаление folder каскадно удаляет edition и edition_item
            conn.execute("UPDATE subscriptions SET folder_id = NULL WHERE id = ?", (sub_id,))
            conn.execute("DELETE FROM folders WHERE id = ?", (folder_id,))
            conn.commit()

            remaining_editions = conn.execute("SELECT COUNT(*) FROM digest_editions WHERE folder_id = ?", (folder_id,)).fetchone()[0]
            remaining_items = conn.execute("SELECT COUNT(*) FROM digest_edition_items WHERE edition_id = ?", (edition_id,)).fetchone()[0]
            self.assertEqual(remaining_editions, 0, "Каскадное удаление edition не сработало")
            self.assertEqual(remaining_items, 0, "Каскадное удаление item не сработало")
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
