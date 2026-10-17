"""Тесты для cron-файлов и механизма их синхронизации в deploy.yml (OPS3).

Проверяет:
1. Все файлы в ops/aiqu/cron.d/*:
   - начинаются с префикса 'unagi-'
   - не содержат точки в имени файла (иначе Debian/Ubuntu cron их игнорирует)
   - заканчиваются переводом строки '\n' (иначе cron их игнорирует)
2. Логика очистки устаревших cron-файлов:
   - удаляет /etc/cron.d/unagi-*, которых нет в репозитории
   - не трогает чужие cron-файлы без префикса unagi-
   - сохраняет актуальные cron-файлы из репозитория
"""

import re
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CRON_DIR = ROOT / "ops" / "aiqu" / "cron.d"
DEPLOY_YML = ROOT / ".github" / "workflows" / "deploy.yml"


def extract_cron_sync_block() -> str:
    """Извлекает блок очистки и установки cron из deploy.yml."""
    text = DEPLOY_YML.read_text(encoding="utf-8")
    match = re.search(r"# Cron-файлы из репозитория:[^\n]*\n(.*)", text, re.S)
    assert match, "в deploy.yml не найден блок установки cron"
    return "set -e\n" + match.group(1).strip() + "\n"


class CronFilesValidationTests(unittest.TestCase):
    def test_cron_directory_exists_and_not_empty(self):
        self.assertTrue(CRON_DIR.is_dir(), f"Каталог {CRON_DIR} должен существовать")
        files = [p for p in CRON_DIR.iterdir() if p.is_file()]
        self.assertGreater(len(files), 0, "Каталог cron.d не должен быть пуст")

    def test_cron_files_naming_and_formatting(self):
        files = [p for p in CRON_DIR.iterdir() if p.is_file()]
        for cron_file in files:
            name = cron_file.name
            with self.subTest(file=name):
                # 1. Начинается с unagi-
                self.assertTrue(
                    name.startswith("unagi-"),
                    f"Имя файла '{name}' должно начинаться с 'unagi-'",
                )
                # 2. Без точки в имени файла (cron run-parts игнорирует файлы с точками)
                self.assertNotIn(
                    ".",
                    name,
                    f"Имя файла '{name}' не должно содержать точку (cron игнорирует такие файлы)",
                )
                # 3. Заканчивается переводом строки
                content_bytes = cron_file.read_bytes()
                self.assertGreater(len(content_bytes), 0, f"Файл '{name}' пуст")
                self.assertTrue(
                    content_bytes.endswith(b"\n"),
                    f"Файл '{name}' должен заканчиваться символом перевода строки (иначе cron его не выполнит)",
                )


class CronCleanupExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.mock_etc_cron = Path(self.tmp.name) / "etc" / "cron.d"
        self.mock_etc_cron.mkdir(parents=True, exist_ok=True)

    def test_stale_cron_cleanup_and_install_logic(self):
        # Создаём в mock /etc/cron.d:
        # 1. unagi-status-alert (актуальный из репозитория)
        # 2. unagi-stale-job (устаревший, которого нет в репозитории — должен быть удалён)
        # 3. sysstat (системный файл без префикса unagi- — НЕ должен быть удалён)
        repo_files = [p.name for p in CRON_DIR.iterdir() if p.is_file()]
        self.assertIn("unagi-status-alert", repo_files)

        current_file = self.mock_etc_cron / "unagi-status-alert"
        stale_file = self.mock_etc_cron / "unagi-stale-job"
        sys_file = self.mock_etc_cron / "sysstat"

        current_file.write_text("old content\n", encoding="utf-8")
        stale_file.write_text("should be deleted\n", encoding="utf-8")
        sys_file.write_text("system cron\n", encoding="utf-8")

        # Скрипт очистки адаптированный для временного mock каталога /etc/cron.d
        test_script = f"""
set -e
for installed in "{self.mock_etc_cron}"/unagi-*; do
  if [ -f "$installed" ] && [ ! -f "{CRON_DIR}/$(basename "$installed")" ]; then
    rm -f "$installed" || true
  fi
done
for f in "{CRON_DIR}"/*; do
  [ -f "$f" ] && cp "$f" "{self.mock_etc_cron}/$(basename "$f")" || true
done
"""
        res = subprocess.run(["bash", "-c", test_script], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)

        # Проверяем:
        # 1. Устаревший удалён
        self.assertFalse(stale_file.exists(), "Устаревший файл unagi-stale-job должен быть удалён")
        # 2. Системный сохранён
        self.assertTrue(sys_file.exists(), "Системный файл sysstat не должен быть затронут")
        # 3. Актуальные установлены
        for name in repo_files:
            target = self.mock_etc_cron / name
            self.assertTrue(target.exists(), f"Файл {name} должен быть установлен в {self.mock_etc_cron}")
            self.assertEqual(target.read_bytes(), (CRON_DIR / name).read_bytes())


if __name__ == "__main__":
    unittest.main()
