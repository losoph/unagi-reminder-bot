import hashlib
import io
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import data.database as db_mod
from ops.migrate_schedules_dryrun import (
    analyze_migration,
    main,
    open_readonly_db,
)


class MigrateSchedulesDryrunTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "test_bot_data.db"
        with patch.object(db_mod, "DB_PATH", str(self.db_path)):
            db_mod.init_db()

    def tearDown(self):
        self.tmp_dir.cleanup()

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
            # Пользователь 101: 2 канала в теге 'news', 1 канал без тега (Основное)
            conn.execute(
                """
                INSERT INTO subscriptions (user_id, channel_username, channel_title, period, tag, is_disabled)
                VALUES (101, 'tech_news', 'Tech News', 'daily', 'news', 0),
                       (101, 'world_news', 'World News', 'daily', 'news', 0),
                       (101, 'my_channel', 'My Channel', 'daily', NULL, 0)
                """
            )
            # Пользователь 102: 1 канал в 'crypto' (weekly, email)
            conn.execute(
                """
                INSERT INTO subscriptions (user_id, channel_username, channel_title, period, tag, is_disabled)
                VALUES (102, 'crypto_chan', 'Crypto', 'weekly', 'crypto', 0)
                """
            )
            # Настройки digest_settings
            conn.execute(
                """
                INSERT INTO digest_settings (user_id, period, send_hour, send_minute)
                VALUES (101, 'daily', 8, 30),
                       (102, 'weekly', 10, 0)
                """
            )
            # Почта пользователя 102 (подтверждена)
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

    def test_clean_data_dryrun_reports_no_conflicts_exit_0(self):
        """Чистые данные: отчёт верен, расчёт папок/расписаний точен, код возврата 0."""
        self._seed_clean_data()

        stdout_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf):
            exit_code = main([str(self.db_path)])

        output = stdout_buf.getvalue()
        self.assertEqual(exit_code, 0)
        self.assertIn("ВЕРДИКТ: ЧИСТО, КОНФЛИКТОВ НЕ ОБНАРУЖЕНО (КОД ВЫХОДА 0)", output)
        self.assertIn("Активных пользователей: 2", output)
        self.assertIn("Активных подписок:      4", output)
        # Пользователь 101: папка 'news' (daily) + 'Основное' (daily) = 2 папки, 2 расписания
        # Пользователь 102: папка 'crypto' (weekly) = 1 папка, 1 расписание
        self.assertIn("Будет создано папок:    3", output)
        self.assertIn("Будет создано расписаний: 3", output)
        self.assertIn("Telegram-расписаний:   2", output)
        self.assertIn("Email-расписаний:      1", output)

    def test_conflict_channel_in_multiple_tags_detected(self):
        """Конфликт: один и тот же канал у пользователя в разных тегах."""
        self._seed_clean_data()
        # Добавляем для user 101 подписку на тот же канал 'tech_news', но с другим тегом 'work'
        self._execute_write(
            """
            INSERT INTO subscriptions (user_id, channel_username, channel_title, period, tag, is_disabled)
            VALUES (101, 'tech_news', 'Tech News Duplicate', 'weekly', 'work', 0)
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
        self.assertIn("ВЕРДИКТ: ОБНАРУЖЕНЫ КОНФЛИКТЫ (КОД ВЫХОДА 3)", output)
        self.assertIn("[1] Канал в нескольких тегах: 1", output)
        self.assertIn("channel=@tech_news", output)

    def test_conflict_unconfirmed_email_detected(self):
        """Конфликт: указаны email_periods, но email пуст или не подтверждён."""
        self._seed_clean_data()
        self._execute_write(
            """
            INSERT INTO user_emails (user_id, email, email_periods, pending_email)
            VALUES (101, NULL, 'daily', 'pending101@example.com')
            """
        )

        stdout_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf):
            exit_code = main([str(self.db_path)])

        output = stdout_buf.getvalue()
        self.assertEqual(exit_code, 3)
        self.assertIn("[4] Почта без подтверждения: 1", output)
        self.assertIn("user=101", output)

    def test_conflict_schedule_without_time_detected(self):
        """Конфликт: в digest_settings отсутствуют или некорректны часы/минуты."""
        self._seed_clean_data()
        # Повреждаем время для пользователя 101
        self._execute_write(
            "UPDATE digest_settings SET send_hour = NULL WHERE user_id = 101 AND period = 'daily'"
        )

        stdout_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf):
            exit_code = main([str(self.db_path)])

        output = stdout_buf.getvalue()
        self.assertEqual(exit_code, 3)
        self.assertIn("[3] Расписания без явного времени:", output)
        self.assertIn("user=101", output)

    def test_conflict_empty_or_inactive_folders_detected(self):
        """Конфликт: тег существует только у отключённых подписок."""
        self._seed_clean_data()
        self._execute_write(
            """
            INSERT INTO subscriptions (user_id, channel_username, channel_title, period, tag, is_disabled)
            VALUES (101, 'dead_channel', 'Dead', 'daily', 'archive_tag', 1)
            """
        )

        stdout_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf):
            exit_code = main([str(self.db_path)])

        output = stdout_buf.getvalue()
        self.assertEqual(exit_code, 3)
        self.assertIn("[2] Пустые / неактивные теги: 1", output)
        self.assertIn("archive_tag", output)

    def test_db_remains_strictly_unchanged_and_readonly(self):
        """Проверка неизменности БД: mtime и sha256 совпадают до и после запуска, запись запрещена."""
        self._seed_clean_data()

        # Фиксируем состояние файла до анализа
        mtime_before = self.db_path.stat().st_mtime_ns
        hash_before = hashlib.sha256(self.db_path.read_bytes()).hexdigest()

        # Проверяем, что соединение открывается строго read-only
        ro_conn = open_readonly_db(self.db_path)
        with self.assertRaises(sqlite3.OperationalError) as ctx:
            ro_conn.execute("INSERT INTO subscriptions (user_id) VALUES (1)")
        self.assertIn("readonly", str(ctx.exception).lower())
        ro_conn.close()

        # Выполняем dry-run
        stdout_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf):
            code = main([str(self.db_path)])
        self.assertEqual(code, 0)

        # Проверяем состояние файла после
        mtime_after = self.db_path.stat().st_mtime_ns
        hash_after = hashlib.sha256(self.db_path.read_bytes()).hexdigest()

        self.assertEqual(hash_before, hash_after, "Хеш базы данных изменился во время dry-run!")
        self.assertEqual(mtime_before, mtime_after, "Время модификации mtime изменилось!")

    def test_repeated_run_gives_identical_output(self):
        """Повторный запуск скрипта детерминирован и даёт идентичный отчёт."""
        self._seed_clean_data()

        stdout_1 = io.StringIO()
        with patch("sys.stdout", stdout_1):
            code_1 = main([str(self.db_path)])

        stdout_2 = io.StringIO()
        with patch("sys.stdout", stdout_2):
            code_2 = main([str(self.db_path)])

        self.assertEqual(code_1, code_2)
        self.assertEqual(stdout_1.getvalue(), stdout_2.getvalue())

    def test_legacy_counters_are_counted(self):
        """Проверка подсчёта legacy-записей (digest_executions, digest_alerts)."""
        self._seed_clean_data()
        self._execute_write(
            """
            INSERT INTO digest_executions (user_id, period, scheduled_at, status, created_at)
            VALUES (101, 'daily', '2026-10-17 08:30:00', 'delivered', '2026-10-17 08:31:00')
            """
        )
        self._execute_write(
            """
            INSERT INTO digest_alerts (user_id, period, scheduled_at, alert_type, sent_at, created_at)
            VALUES (101, 'daily', '2026-10-17 08:30:00', 'missed', '2026-10-17 09:30:00', '2026-10-17 09:30:00')
            """
        )

        ro_conn = open_readonly_db(self.db_path)
        try:
            report = analyze_migration(ro_conn)
        finally:
            ro_conn.close()

        self.assertEqual(report["legacy"]["digest_executions_count"], 1)
        self.assertEqual(report["legacy"]["digest_alerts_count"], 1)


if __name__ == "__main__":
    unittest.main()
