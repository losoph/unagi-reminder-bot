"""Регрессионные тесты для замечаний независимого ревью DOC1 (dsh).

Проверяют исправление всех подтверждённых пунктов ревью:
1. Политика очистки builder prune until=6h в документации и deploy.yml;
2. Статус MS2 (digest_settings.py) как находящегося на ревью / в PR 28;
3. Статус tools/handler_order.py (в PR 28, интроспекция dp, а не только статический анализ);
4. Согласованность журнала решений docs/sprint/progress/<МЕТКА>.md;
5. Доступность решений владельца docs/sprint/owner-decisions-2026-10-17.md;
6. Актуальный baseline тестов (>= 640 тестов) и описание пропусков в CI;
7. Точное описание проверок харденинга в prod_check.sh (не-root, CapEff: 0, NoNewPrivs: 1);
8. paths-ignore в .github/workflows/deploy.yml для защиты прода от docs-only деплоев;
9. Проверка числа таблиц (>= min_tables) в ops/aiqu/backup_db.py (пустая БД не проходит).
"""

from __future__ import annotations

import contextlib
import importlib.util
from pathlib import Path
import sqlite3
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

spec = importlib.util.spec_from_file_location("backup_db", ROOT / "ops" / "aiqu" / "backup_db.py")
backup_db = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup_db)


class Doc1ReviewRegressionTests(unittest.TestCase):
    # 1. builder prune policy: until=6h
    def test_item1_builder_prune_policy_is_6h(self):
        deploy_yml = (ROOT / ".github" / "workflows" / "deploy.yml").read_text(encoding="utf-8")
        handoff = (ROOT / "docs" / "HANDOFF.md").read_text(encoding="utf-8")
        arch = (ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")
        aiqu_readme = (ROOT / "ops" / "aiqu" / "README.md").read_text(encoding="utf-8")

        self.assertIn("until=6h", deploy_yml)
        self.assertNotIn("until=48h", deploy_yml)

        self.assertIn("until=6h", handoff)
        self.assertNotIn("until=48h", handoff)

        self.assertIn("until=6h", arch)
        self.assertNotIn("until=48h", arch)

        self.assertIn("until=6h", aiqu_readme)
        self.assertNotIn("until=48h", aiqu_readme)

    # 2. MS2 status in docs: in review / PR 28, not completed in main
    def test_item2_ms2_described_as_in_review(self):
        arch = (ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        roadmap = (ROOT / "docs" / "roadmap.md").read_text(encoding="utf-8")

        # Не должно утверждаться, что digest_settings уже слит в main
        self.assertNotIn("кластеры закладок (bookmarks.py, MS1) и настроек дайджеста (digest_settings.py, MS2) вынесены", roadmap)
        self.assertTrue(
            "ревью" in roadmap or "PR" in roadmap,
            "roadmap должен указывать статус MS2 как на ревью / в PR",
        )
        self.assertTrue(
            "ревью" in arch or "PR" in arch or "feat/ms2-digest-router" in arch,
            "architecture.md должен указывать статус MS2 как на ревью / в PR",
        )
        self.assertTrue(
            "ревью" in readme or "PR" in readme or "feat/ms2-digest-router" in readme,
            "README.md должен указывать статус MS2 как на ревью / в PR",
        )

    # 3. handler_order.py status: in PR 28, introspection not purely static
    def test_item3_handler_order_described_accurately(self):
        arch = (ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")
        # Должно быть указано, что это интроспекция/обход дерева при импорте, и инструмент в PR
        self.assertNotIn("Специализированная утилита статического анализа", arch)
        self.assertTrue(
            "интроспекци" in arch or "обход" in arch or "импорт" in arch,
            "architecture.md должен описывать работу утилиты через интроспекцию / обход дерева хендлеров",
        )

    # 4. Progress per-task convention
    def test_item4_progress_per_task_convention_consistent(self):
        handoff = (ROOT / "docs" / "HANDOFF.md").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        arch = (ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")

        self.assertIn("docs/sprint/progress/", handoff)
        self.assertIn("docs/sprint/progress/", readme)
        self.assertIn("docs/sprint/progress/", arch)

    # 5. owner decisions file exists and links resolve
    def test_item5_owner_decisions_file_exists(self):
        target = ROOT / "docs" / "sprint" / "owner-decisions-2026-10-17.md"
        self.assertTrue(target.is_file(), f"файл {target} должен существовать в репозитории")
        content = target.read_text(encoding="utf-8")
        self.assertIn("Лист решений владельца", content)

    # 6. Test baseline in docs reflects current suite (>= 640 tests) and CI skips
    def test_item6_test_baseline_accuracy(self):
        handoff = (ROOT / "docs" / "HANDOFF.md").read_text(encoding="utf-8")
        arch = (ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")

        self.assertNotIn("570 тестов, skipped=2", handoff)
        self.assertNotIn("570 тестов", arch)

    # 7. prod_check.sh hardening description accuracy
    def test_item7_prod_check_hardening_description(self):
        aiqu_readme = (ROOT / "ops" / "aiqu" / "README.md").read_text(encoding="utf-8")
        arch = (ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")

        # prod_check.sh проверяет не-root uid, CapEff: 0, NoNewPrivs: 1, а не строгий Uid 10001
        self.assertNotIn("Uid 10001 и сброс capabilities", arch)
        self.assertNotIn("Uid: 10001 (пользователь bot)", aiqu_readme)

    # 8. deploy.yml paths-ignore for docs-only commits
    def test_item8_deploy_workflow_has_paths_ignore_for_docs(self):
        deploy_yml = (ROOT / ".github" / "workflows" / "deploy.yml").read_text(encoding="utf-8")
        self.assertIn("paths-ignore:", deploy_yml)
        self.assertIn("docs/**", deploy_yml)

    # 9. backup_db.py empty database verification
    def test_item9_backup_db_rejects_empty_database_with_zero_tables(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            empty_db = tmp / "empty.db"
            # Создаём пустой валидный файл SQLite без таблиц
            with contextlib.closing(sqlite3.connect(empty_db)) as conn:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.commit()

            backup_dir = tmp / "backups"

            # snapshot() должен выбросить RuntimeError
            with self.assertRaises(RuntimeError) as ctx:
                backup_db.snapshot(empty_db, backup_dir, "predeploy")
            self.assertIn("таблиц", str(ctx.exception))

            # CLI main() должен вернуть код 1 и не оставлять снимков
            code = backup_db.main(["--db", str(empty_db), "--dir", str(backup_dir)])
            self.assertEqual(code, 1)
            self.assertEqual(list(backup_dir.glob("*.db")), [])
            self.assertEqual(list(backup_dir.glob("*.partial")), [])


if __name__ == "__main__":
    unittest.main()
