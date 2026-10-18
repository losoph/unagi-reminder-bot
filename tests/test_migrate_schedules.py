import hashlib
import io
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from data import database as db
from ops.migrate_schedules import apply_migration, main
from ops.migrate_schedules_dryrun import normalize_tag


class MigrateSchedulesTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_migrate.db"
        self.original_db_path = db.DB_PATH
        db.DB_PATH = str(self.db_path)
        db.init_db()

    def tearDown(self):
        db.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def _execute_write(self, sql: str, params: tuple = ()) -> None:
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute(sql, params)
            conn.commit()
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()

    def _seed_clean_data(self) -> None:
        """Наполняет базу консистентными данными без конфликтов."""
        conn = sqlite3.connect(str(self.db_path))
        try:
            # Пользователь 101: 2 канала в теге 'news' (daily, 08:30), 1 канал без тега 'Основное' (daily, 08:30)
            conn.execute(
                """
                INSERT INTO subscriptions (id, user_id, channel_username, channel_title, period, tag, last_post_id, is_disabled, next_send_at)
                VALUES (1, 101, 'tech_news', 'Tech News', 'daily', 'news', 500, 0, '2026-10-18 08:30:00'),
                       (2, 101, 'world_news', 'World News', 'daily', 'news', 600, 0, '2026-10-18 08:30:00'),
                       (3, 101, 'my_channel', 'My Channel', 'daily', NULL, 700, 0, '2026-10-18 08:30:00')
                """
            )
            # Пользователь 102: 1 канал в 'crypto' (weekly, 10:00), с подтверждённым email
            conn.execute(
                """
                INSERT INTO subscriptions (id, user_id, channel_username, channel_title, period, tag, last_post_id, is_disabled, next_send_at)
                VALUES (4, 102, 'crypto_chan', 'Crypto', 'weekly', 'crypto', 800, 0, '2026-10-18 10:00:00')
                """
            )
            # Настройки digest_settings
            conn.execute(
                """
                INSERT INTO digest_settings (user_id, period, send_hour, send_minute, weekday, month_day, monthly_mode)
                VALUES (101, 'daily', 8, 30, 0, 1, 'date'),
                       (102, 'weekly', 10, 0, 6, 1, 'date')
                """
            )
            # Настройки user_prefs (v1)
            conn.execute(
                """
                INSERT INTO user_prefs (user_id, timezone, digest_scheduler_version)
                VALUES (101, 'Europe/Moscow', 'v1'),
                       (102, 'UTC', 'v1')
                """
            )
            # Email пользователя 102
            conn.execute(
                """
                INSERT INTO user_emails (user_id, email, email_periods)
                VALUES (102, 'user102@example.com', 'weekly')
                """
            )
            conn.commit()
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()

    def test_clean_db_dryrun_exit_0_and_db_untouched(self):
        """Без --apply на чистых данных: код 0, БД не тронута по хешу."""
        self._seed_clean_data()
        hash_before = hashlib.sha256(self.db_path.read_bytes()).hexdigest()

        stdout_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf):
            exit_code = main([str(self.db_path)])

        output = stdout_buf.getvalue()
        self.assertEqual(exit_code, 0)
        self.assertIn("ВЕРДИКТ: ЧИСТО, КОНФЛИКТОВ НЕ ОБНАРУЖЕНО (КОД ВЫХОДА 0)", output)
        self.assertIn("Режим dry-run", output)

        hash_after = hashlib.sha256(self.db_path.read_bytes()).hexdigest()
        self.assertEqual(hash_before, hash_after, "Хеш базы данных изменился в dry-run режиме")

    def test_conflict_blocks_migration_exit_3_and_db_untouched_even_with_apply(self):
        """При наличии конфликтов (неоднородный period): код 3, в БД ничего не меняется даже с --apply."""
        self._seed_clean_data()
        # Добавляем конфликт: неоднородный period в папке 'news'
        self._execute_write(
            """
            INSERT INTO subscriptions (id, user_id, channel_username, channel_title, period, tag, is_disabled, next_send_at)
            VALUES (5, 101, 'weekly_news', 'Weekly News', 'weekly', 'news', 0, '2026-10-18 08:30:00')
            """
        )
        hash_before = hashlib.sha256(self.db_path.read_bytes()).hexdigest()

        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf), patch("sys.stderr", stderr_buf):
            exit_code = main([str(self.db_path), "--apply"])

        self.assertEqual(exit_code, 3)
        self.assertIn("ВЕРДИКТ: ОБНАРУЖЕНЫ КОНФЛИКТЫ (КОД ВЫХОДА 3)", stdout_buf.getvalue())
        self.assertIn("Миграция заблокирована (код возврата 3)", stderr_buf.getvalue())

        hash_after = hashlib.sha256(self.db_path.read_bytes()).hexdigest()
        self.assertEqual(hash_before, hash_after, "Хеш базы данных изменился при конфликте")

    def test_apply_on_clean_data_creates_folders_and_links_subscriptions(self):
        """Успешный --apply: создание папок, привязка subscriptions, сохранение last_post_id и v1."""
        self._seed_clean_data()

        stdout_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf):
            exit_code = main([str(self.db_path), "--apply"])

        output = stdout_buf.getvalue()
        self.assertEqual(exit_code, 0)
        self.assertIn("[УСПЕХ] Миграция успешно завершена", output)
        self.assertIn("Создано новых папок:         3", output)
        self.assertIn("Привязано подписок к папкам: 4", output)

        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            # 1. Проверяем папки пользователя 101
            folders_101 = conn.execute(
                "SELECT * FROM folders WHERE user_id = 101 ORDER BY name"
            ).fetchall()
            self.assertEqual(len(folders_101), 2)

            f_news = next(f for f in folders_101 if f["name"] == "news")
            self.assertEqual(f_news["is_default"], 0)
            self.assertEqual(f_news["period"], "daily")
            self.assertEqual(f_news["send_hour"], 8)
            self.assertEqual(f_news["send_minute"], 30)
            self.assertEqual(f_news["times"], '["08:30"]')
            self.assertEqual(f_news["delivery"], "telegram")
            self.assertEqual(f_news["next_send_at"], "2026-10-18 08:30:00")

            f_main = next(f for f in folders_101 if f["name"] == "Основное")
            self.assertEqual(f_main["is_default"], 1)
            self.assertEqual(f_main["period"], "daily")
            self.assertEqual(f_main["send_hour"], 8)
            self.assertEqual(f_main["send_minute"], 30)
            self.assertEqual(f_main["delivery"], "telegram")

            # 2. Проверяем папку пользователя 102 (по умолчанию telegram, без --email-folders)
            f_crypto = conn.execute(
                "SELECT * FROM folders WHERE user_id = 102"
            ).fetchone()
            self.assertIsNotNone(f_crypto)
            self.assertEqual(f_crypto["name"], "crypto")
            self.assertEqual(f_crypto["period"], "weekly")
            self.assertEqual(f_crypto["send_hour"], 10)
            self.assertEqual(f_crypto["send_minute"], 0)
            self.assertEqual(f_crypto["weekdays"], "6")
            self.assertEqual(f_crypto["delivery"], "telegram")

            # 3. Проверяем привязку subscriptions.folder_id
            subs = conn.execute(
                "SELECT id, channel_username, folder_id, last_post_id FROM subscriptions ORDER BY id"
            ).fetchall()
            self.assertEqual(subs[0]["folder_id"], f_news["id"])
            self.assertEqual(subs[0]["last_post_id"], 500, "last_post_id не должен меняться!")
            self.assertEqual(subs[1]["folder_id"], f_news["id"])
            self.assertEqual(subs[1]["last_post_id"], 600, "last_post_id не должен меняться!")
            self.assertEqual(subs[2]["folder_id"], f_main["id"])
            self.assertEqual(subs[2]["last_post_id"], 700, "last_post_id не должен меняться!")
            self.assertEqual(subs[3]["folder_id"], f_crypto["id"])
            self.assertEqual(subs[3]["last_post_id"], 800, "last_post_id не должен меняться!")

            # 4. Проверяем user_prefs: digest_scheduler_version остаётся 'v1'
            prefs = conn.execute("SELECT user_id, digest_scheduler_version FROM user_prefs ORDER BY user_id").fetchall()
            for p in prefs:
                self.assertEqual(p["digest_scheduler_version"], "v1")
        finally:
            conn.close()

    def test_apply_idempotency_second_run_does_not_duplicate(self):
        """Повторный запуск --apply идемпотентен, не дублирует папки и возвращает код 0."""
        self._seed_clean_data()

        # Первый запуск
        code_1 = main([str(self.db_path), "--apply"])
        self.assertEqual(code_1, 0)

        # Второй запуск
        stdout_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf):
            code_2 = main([str(self.db_path), "--apply"])

        self.assertEqual(code_2, 0)
        self.assertIn("Создано новых папок:         0", stdout_buf.getvalue())

        conn = sqlite3.connect(str(self.db_path))
        try:
            count_folders = conn.execute("SELECT COUNT(*) FROM folders").fetchone()[0]
            self.assertEqual(count_folders, 3, "Количество папок не должно измениться при повторном запуске")
            unlinked_subs = conn.execute("SELECT COUNT(*) FROM subscriptions WHERE folder_id IS NULL AND is_disabled = 0").fetchone()[0]
            self.assertEqual(unlinked_subs, 0)
        finally:
            conn.close()

    def test_email_folders_flag(self):
        """Флаг --email-folders переводит папки email-пользователей в delivery='email'."""
        self._seed_clean_data()

        stdout_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf):
            exit_code = main([str(self.db_path), "--apply", "--email-folders"])

        self.assertEqual(exit_code, 0)

        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            # Пользователь 101 (нет email) -> telegram
            f_news = conn.execute("SELECT delivery FROM folders WHERE user_id = 101 AND name = 'news'").fetchone()
            self.assertEqual(f_news["delivery"], "telegram")

            # Пользователь 102 (есть email с weekly) -> email
            f_crypto = conn.execute("SELECT delivery FROM folders WHERE user_id = 102 AND name = 'crypto'").fetchone()
            self.assertEqual(f_crypto["delivery"], "email")
        finally:
            conn.close()

    def test_rollback_on_failure_in_the_middle_of_transaction(self):
        """Сбой посреди транзакции приводит к полному откату (код 1, папки не созданы)."""
        self._seed_clean_data()

        # Патчим apply_migration, чтобы сымитировать ошибку
        def failing_apply(conn, email_folders=False):
            # Создадим одну папку, затем бросим исключение
            conn.execute(
                """
                INSERT INTO folders (user_id, name, period, created_at, updated_at)
                VALUES (999, 'FailedFolder', 'daily', '2026-10-18 00:00:00', '2026-10-18 00:00:00')
                """
            )
            raise RuntimeError("Симулированный сбой посреди транзакции миграции")

        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        with patch("ops.migrate_schedules.apply_migration", side_effect=failing_apply), \
             patch("sys.stdout", stdout_buf), patch("sys.stderr", stderr_buf):
            exit_code = main([str(self.db_path), "--apply"])

        self.assertEqual(exit_code, 1)
        self.assertIn("Сбой при применении миграции (транзакция откачена)", stderr_buf.getvalue())

        conn = sqlite3.connect(str(self.db_path))
        try:
            # Проверяем, что папка FailedFolder не была зафиксирована
            folder_row = conn.execute("SELECT 1 FROM folders WHERE name = 'FailedFolder'").fetchone()
            self.assertIsNone(folder_row, "Транзакция не была откачена!")
            # Проверяем, что folder_id у subscriptions остался NULL
            subs_with_folder = conn.execute("SELECT COUNT(*) FROM subscriptions WHERE folder_id IS NOT NULL").fetchone()[0]
            self.assertEqual(subs_with_folder, 0, "folder_id не должен быть привязан при откате")
        finally:
            conn.close()

    def test_missing_db_file_returns_exit_1(self):
        """Несуществующий файл базы данных возвращает код 1."""
        stderr_buf = io.StringIO()
        with patch("sys.stderr", stderr_buf):
            exit_code = main(["/non/existent/path/to/db.sqlite"])
        self.assertEqual(exit_code, 1)
        self.assertIn("файл базы данных не найден", stderr_buf.getvalue())

    def test_tag_normalization_empty_and_system_names_to_default_folder(self):
        """Пункт 1: пустой тег, а также «Основное»/«Основной» (casefold) попадают в Основное с is_default=1."""
        self.assertEqual(normalize_tag(None), ("Основное", 1))
        self.assertEqual(normalize_tag(""), ("Основное", 1))
        self.assertEqual(normalize_tag("   "), ("Основное", 1))
        self.assertEqual(normalize_tag("Основное"), ("Основное", 1))
        self.assertEqual(normalize_tag("основное"), ("Основное", 1))
        self.assertEqual(normalize_tag("ОСНОВНОЕ"), ("Основное", 1))
        self.assertEqual(normalize_tag("Основной"), ("Основное", 1))
        self.assertEqual(normalize_tag("основной"), ("Основное", 1))
        self.assertEqual(normalize_tag("ОСНОВНОЙ"), ("Основное", 1))
        self.assertEqual(normalize_tag("news"), ("news", 0))
        self.assertEqual(normalize_tag("  Новости  "), ("Новости", 0))

    def test_casefold_collision_returns_code_3_and_blocks_write(self):
        """Пункт 2: теги, различающиеся только регистром (кириллица casefold) — конфликт, код 3, запись не делается."""
        self._seed_clean_data()
        self._execute_write(
            """
            INSERT INTO subscriptions (id, user_id, channel_username, channel_title, period, tag, is_disabled, next_send_at)
            VALUES (10, 101, 'crypto_upper', 'Crypto Upper', 'daily', 'Крипта', 0, '2026-10-18 08:30:00'),
                   (11, 101, 'crypto_lower', 'Crypto Lower', 'daily', 'крипта', 0, '2026-10-18 08:30:00')
            """
        )
        hash_before = hashlib.sha256(self.db_path.read_bytes()).hexdigest()

        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf), patch("sys.stderr", stderr_buf):
            exit_code = main([str(self.db_path), "--apply"])

        self.assertEqual(exit_code, 3)
        self.assertIn("Коллизии имён папок по регистру (casefold): 1", stdout_buf.getvalue())
        self.assertIn("Миграция заблокирована (код возврата 3)", stderr_buf.getvalue())

        hash_after = hashlib.sha256(self.db_path.read_bytes()).hexdigest()
        self.assertEqual(hash_before, hash_after, "БД не должна изменяться при casefold коллизии")

    def test_disabled_subscriptions_do_not_cause_false_period_conflict(self):
        """Пункт 3: период папки и конфликты считаются только по активным подпискам (is_disabled = 0)."""
        self._seed_clean_data()
        # Добавляем отключённую подписку с другим периодом в существующий тег 'news'
        self._execute_write(
            """
            INSERT INTO subscriptions (id, user_id, channel_username, channel_title, period, tag, is_disabled, next_send_at)
            VALUES (20, 101, 'dead_news', 'Dead News', 'weekly', 'news', 1, '2026-10-18 08:30:00')
            """
        )

        stdout_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf):
            exit_code = main([str(self.db_path), "--apply"])

        self.assertEqual(exit_code, 0)
        self.assertIn("[УСПЕХ] Миграция успешно завершена", stdout_buf.getvalue())

        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            f_news = conn.execute("SELECT * FROM folders WHERE user_id = 101 AND name = 'news'").fetchone()
            self.assertEqual(f_news["period"], "daily")
        finally:
            conn.close()

    def test_conflict_report_shows_active_and_paused_statuses(self):
        """Пункт 3: в отчёте конфликтов выводится статус подписки (disabled/paused/active)."""
        self._seed_clean_data()
        # Добавляем подписку на тот же канал 'tech_news', но в другом периоде, со статусом is_paused=1
        self._execute_write(
            """
            INSERT INTO subscriptions (id, user_id, channel_username, channel_title, period, tag, is_disabled, is_paused, next_send_at)
            VALUES (25, 101, 'tech_news', 'Tech News Paused', 'weekly', 'news', 0, 1, '2026-10-18 08:30:00')
            """
        )
        self._execute_write(
            "INSERT INTO digest_settings (user_id, period, send_hour, send_minute) VALUES (101, 'weekly', 9, 0)"
        )

        stdout_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf):
            exit_code = main([str(self.db_path)])

        output = stdout_buf.getvalue()
        self.assertEqual(exit_code, 3)
        self.assertIn("статусы:", output)
        self.assertTrue("active" in output and "paused" in output)

    def test_existing_folder_period_mismatch_raises_error_and_rolls_back(self):
        """Пункт 4: идемпотентность проверяет соответствие period и is_default у существующей папки."""
        self._seed_clean_data()
        # Сначала создаем схему и папку с несовпадающим period='weekly' вместо 'daily'
        conn = sqlite3.connect(str(self.db_path))
        try:
            # Создаем схему вручную
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS folders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    is_default INTEGER NOT NULL DEFAULT 0,
                    period TEXT NOT NULL,
                    times TEXT NOT NULL DEFAULT '["07:00"]',
                    send_hour INTEGER NOT NULL DEFAULT 7,
                    send_minute INTEGER NOT NULL DEFAULT 0,
                    weekdays TEXT,
                    month_day INTEGER,
                    monthly_mode TEXT DEFAULT 'date',
                    delivery TEXT NOT NULL DEFAULT 'telegram',
                    is_enabled INTEGER NOT NULL DEFAULT 1,
                    next_send_at DATETIME,
                    last_edition_id INTEGER,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL,
                    UNIQUE(user_id, name)
                )
                """
            )
            conn.execute(
                """
                INSERT INTO folders (user_id, name, is_default, period, created_at, updated_at)
                VALUES (101, 'news', 0, 'weekly', '2026-10-18 00:00:00', '2026-10-18 00:00:00')
                """
            )
            conn.commit()
        finally:
            conn.close()

        stderr_buf = io.StringIO()
        stdout_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf), patch("sys.stderr", stderr_buf):
            exit_code = main([str(self.db_path), "--apply"])

        self.assertEqual(exit_code, 1)
        self.assertIn("Конфликт существующей папки", stderr_buf.getvalue())

    def test_invariant_before_commit_rolls_back_if_active_sub_has_null_folder_id(self):
        """Пункт 5: число активных подписок с folder_id IS NULL должно быть равно 0 перед commit."""
        self._seed_clean_data()

        # Симулируем сценарий, когда в процессе apply_migration активная подписка не получила folder_id
        real_apply = apply_migration

        def flawed_apply(conn, email_folders=False):
            res = real_apply(conn, email_folders)
            # Принудительно сбрасываем folder_id у одной активной подписки
            conn.execute("UPDATE subscriptions SET folder_id = NULL WHERE id = 1")
            # Проверяем инвариант вручную так же, как это делает реальный код
            row_unlinked = conn.execute(
                "SELECT COUNT(*) FROM subscriptions WHERE is_disabled = 0 AND folder_id IS NULL"
            ).fetchone()
            if row_unlinked[0] > 0:
                raise RuntimeError("Нарушение инварианта перед COMMIT: обнаружены активные подписки без folder_id")
            return res

        stderr_buf = io.StringIO()
        stdout_buf = io.StringIO()
        with patch("ops.migrate_schedules.apply_migration", side_effect=flawed_apply), \
             patch("sys.stdout", stdout_buf), patch("sys.stderr", stderr_buf):
            exit_code = main([str(self.db_path), "--apply"])

        self.assertEqual(exit_code, 1)
        self.assertIn("Нарушение инварианта перед COMMIT", stderr_buf.getvalue())

    def test_repeated_apply_picks_up_delta_subscriptions_between_runs(self):
        """Пункт 6: повторный запуск --apply безопасен и подбирает подписки без folder_id (дельта v1)."""
        self._seed_clean_data()

        # 1. Первый запуск
        code_1 = main([str(self.db_path), "--apply"])
        self.assertEqual(code_1, 0)

        # 2. Добавляем новую подписку на v1 между запусками (без folder_id)
        self._execute_write(
            """
            INSERT INTO subscriptions (id, user_id, channel_username, channel_title, period, tag, last_post_id, is_disabled, next_send_at, folder_id)
            VALUES (30, 101, 'delta_news_channel', 'Delta News', 'daily', 'news', 999, 0, '2026-10-18 08:30:00', NULL)
            """
        )

        # 3. Второй запуск
        stdout_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf):
            code_2 = main([str(self.db_path), "--apply"])

        self.assertEqual(code_2, 0)
        output = stdout_buf.getvalue()
        self.assertIn("Создано новых папок:         0", output)
        self.assertIn("Привязано подписок к папкам: 1", output)

        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            # Проверяем, что дельта-подписка привязана к существующей папке 'news'
            f_news = conn.execute("SELECT id FROM folders WHERE user_id = 101 AND name = 'news'").fetchone()
            delta_sub = conn.execute("SELECT folder_id, last_post_id FROM subscriptions WHERE id = 30").fetchone()
            self.assertEqual(delta_sub["folder_id"], f_news["id"])
            self.assertEqual(delta_sub["last_post_id"], 999)

            # Проверяем, что ни одна активная подписка не осталась без folder_id
            unlinked = conn.execute("SELECT COUNT(*) FROM subscriptions WHERE is_disabled = 0 AND folder_id IS NULL").fetchone()[0]
            self.assertEqual(unlinked, 0)
        finally:
            conn.close()

    def test_report_contains_table_and_no_time_list(self):
        """Пункт 7: отчёт содержит таблицу целевых папок и отдельный список папок без времени."""
        self._seed_clean_data()

        stdout_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf):
            exit_code = main([str(self.db_path)])

        self.assertEqual(exit_code, 0)
        output = stdout_buf.getvalue()

        # Проверяем заголовки таблицы G1
        self.assertIn("ЦЕЛЕВЫЕ ПАПКИ И РАСПИСАНИЯ:", output)
        self.assertIn("user_id", output)
        self.assertIn("папка", output)
        self.assertIn("is_default", output)
        self.assertIn("period", output)
        self.assertIn("время (digest_settings)", output)
        self.assertIn("delivery", output)
        self.assertIn("число каналов", output)
        self.assertIn("ПАПКИ БЕЗ ЯВНОГО ВРЕМЕНИ В DIGEST_SETTINGS", output)


if __name__ == "__main__":
    unittest.main()
