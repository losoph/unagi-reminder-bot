"""Тесты корректного завершения и сброса WAL SQLite (OPS3).

Проверяет:
1. Вызов PRAGMA wal_checkpoint(TRUNCATE) при завершении работы бота.
2. Сброс и усечение WAL-файла до 0 байт на временной БД.
3. Остановку фоновых задач и удаление маркера готовности.
"""

import asyncio
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

os.environ.setdefault("BOT_TOKEN", "123456:TEST_TOKEN_FOR_UNIT_TESTS")

import data.database as db_mod
import main


class ShutdownCheckpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test_bot.db"
        self.ready_path = Path(self.tmp.name) / "run" / "ready"
        self.orig_db_path = db_mod.DB_PATH
        db_mod.DB_PATH = str(self.db_path)
        main.READY_FILE_PATH = str(self.ready_path)
        main._shutdown_done = False
        main._background_tasks = []

    async def asyncTearDown(self):
        db_mod.DB_PATH = self.orig_db_path
        main._shutdown_done = False
        main._background_tasks = []
        self.tmp.cleanup()

    def test_checkpoint_db_truncates_wal_file(self):
        # Создаём БД в режиме WAL и пишем данные, чтобы наполнить WAL-лог
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, val TEXT)")
        for i in range(100):
            conn.execute("INSERT INTO items (val) VALUES (?)", (f"value_{i}",))
        conn.commit()

        try:
            wal_path = Path(f"{self.db_path}-wal")
            self.assertTrue(wal_path.exists(), "WAL файл должен существовать до чекпоинта")
            self.assertGreater(wal_path.stat().st_size, 0, "WAL файл должен содержать данные")

            # Вызываем checkpoint_db
            res = db_mod.checkpoint_db()
            self.assertEqual(len(res), 3)
            busy, log, checkpointed = res
            self.assertEqual(busy, 0, "Checkpoint не должен быть занят (busy=0)")

            # После TRUNCATE размер WAL-файла должен стать 0 байт
            self.assertEqual(wal_path.stat().st_size, 0, "PRAGMA wal_checkpoint(TRUNCATE) должен усечь WAL файл до 0 байт")
        finally:
            conn.close()

    async def test_perform_shutdown_calls_checkpoint_and_stops_tasks(self):
        # Создаём БД в WAL
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, val TEXT)")
        conn.execute("INSERT INTO items (val) VALUES ('test')")
        conn.commit()
        conn.close()

        # Создаём маркер готовности
        main.write_ready_marker(str(self.ready_path))
        self.assertTrue(self.ready_path.exists(), "Маркер готовности должен существовать")

        # Создаём фиктивную фоновую задачу
        task_cancelled = False

        async def dummy_loop():
            nonlocal task_cancelled
            try:
                while True:
                    await asyncio.sleep(1)
            except asyncio.CancelledError:
                task_cancelled = True
                raise

        task = asyncio.create_task(dummy_loop())
        main._background_tasks = [task]

        with patch.object(main.channel_source, "close", new_callable=AsyncMock) as mock_close, \
             patch.object(main.feed, "cancel_backfill_tasks", new_callable=AsyncMock) as mock_backfill:
            await main.perform_shutdown()

            self.assertTrue(task.cancelled() or task_cancelled, "Фоновая задача должна быть отменена")
            mock_close.assert_awaited_once()
            mock_backfill.assert_awaited_once()
            self.assertFalse(self.ready_path.exists(), "Маркер готовности должен быть удалён при завершении")

        # Проверяем, что WAL усечён
        wal_path = Path(f"{self.db_path}-wal")
        if wal_path.exists():
            self.assertEqual(wal_path.stat().st_size, 0)

    def test_ready_marker_create_and_remove(self):
        marker = Path(self.tmp.name) / "ready_marker"
        self.assertFalse(marker.exists())
        self.assertTrue(main.write_ready_marker(str(marker)))
        self.assertTrue(marker.exists())
        self.assertEqual(marker.read_text(encoding="utf-8").strip(), "ready")
        main.remove_ready_marker(str(marker))
        self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
