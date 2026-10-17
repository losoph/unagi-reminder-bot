"""Шаг синхронизации деплоя (.github/workflows/deploy.yml, блок `# --- sync`) на воспроизведении сбоя 17.10.2026.

17.10 на проде в docker-compose.yml вручную стояло `cpus: 1.0`, а OPS1 заменил строку на `${BOT_CPUS:-1.5}`: `git pull --autostash` слил обновление,
вернул stash с конфликтом (код выхода 0!), в compose остались маркеры, и `docker compose up` упал на YAML. Блок синхронизации обязан это лечить.
"""

import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def sync_block() -> str:
    text = (ROOT / ".github" / "workflows" / "deploy.yml").read_text(encoding="utf-8")
    match = re.search(r"# --- sync[^\n]*\n(.*?)# --- end sync", text, re.S)
    assert match, "в deploy.yml нет блока `# --- sync … # --- end sync`"
    return "set -e\n" + "\n".join(line.strip() for line in match.group(1).splitlines() if line.strip() and not line.strip().startswith("#")) + "\n"


@unittest.skipUnless(shutil.which("git"), "в образе CI (python:3.12-slim) нет git")
class DeploySyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.env = os.environ | {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.test",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.test",
        }
        self.origin = self.dir / "origin.git"
        self.prod = self.dir / "prod"
        self.dev = self.dir / "dev"
        self.git(self.dir, "init", "-q", "--bare", "-b", "main", str(self.origin))
        self.git(self.dir, "clone", "-q", str(self.origin), str(self.dev))
        (self.dev / "docker-compose.yml").write_text("services:\n  bot:\n    mem_limit: 512m\n    cpus: 1.5\n    restart: always\n", encoding="utf-8")
        (self.dev / "README.md").write_text("v1\n", encoding="utf-8")
        self.git(self.dev, "add", "-A")
        self.git(self.dev, "commit", "-q", "-m", "init")
        self.git(self.dev, "push", "-q", "origin", "HEAD:main")
        self.git(self.dir, "clone", "-q", str(self.origin), str(self.prod))

    def git(self, cwd, *args, check=True):
        return subprocess.run(["git", *args], cwd=cwd, env=self.env, text=True, capture_output=True, check=check)

    def upstream_changes_cpus_line(self):
        path = self.dev / "docker-compose.yml"
        path.write_text(path.read_text(encoding="utf-8").replace("cpus: 1.5", "cpus: ${BOT_CPUS:-1.5}"), encoding="utf-8")
        self.git(self.dev, "commit", "-qam", "cpus из .env")
        self.git(self.dev, "push", "-q", "origin", "HEAD:main")

    def run_sync(self):
        return subprocess.run(["bash", "-c", sync_block()], cwd=self.prod, env=self.env, text=True, capture_output=True, check=False)

    def head(self, cwd):
        return self.git(cwd, "rev-parse", "HEAD").stdout.strip()

    def test_recovers_from_the_failed_autostash_of_17_october(self):
        compose = self.prod / "docker-compose.yml"
        compose.write_text(compose.read_text(encoding="utf-8").replace("cpus: 1.5", "cpus: 1.0"), encoding="utf-8")  # ручная правка на сервере
        self.upstream_changes_cpus_line()
        old = self.git(self.prod, "pull", "--autostash", "origin", "main", check=False)  # прежний деплой
        self.assertEqual(old.returncode, 0, "autostash вернул успех, несмотря на конфликт — так и было на проде")
        self.assertIn("<<<<<<<", compose.read_text(encoding="utf-8"))
        self.assertTrue(self.git(self.prod, "ls-files", "-u").stdout.strip())

        result = self.run_sync()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.git(self.prod, "ls-files", "-u").stdout.strip(), "")
        self.assertNotIn("<<<<<<<", compose.read_text(encoding="utf-8"))
        self.assertIn("${BOT_CPUS:-1.5}", compose.read_text(encoding="utf-8"))
        self.assertEqual(self.head(self.prod), self.head(self.dev))

    def test_manual_compose_edit_is_dropped_before_the_pull(self):
        compose = self.prod / "docker-compose.yml"
        compose.write_text(compose.read_text(encoding="utf-8").replace("restart: always", "restart: no"), encoding="utf-8")
        self.upstream_changes_cpus_line()
        result = self.run_sync()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("restart: always", compose.read_text(encoding="utf-8"))
        self.assertEqual(self.head(self.prod), self.head(self.dev))

    def test_clean_checkout_just_fast_forwards(self):
        self.upstream_changes_cpus_line()
        result = self.run_sync()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.head(self.prod), self.head(self.dev))

    def test_diverged_history_stops_the_deploy(self):
        (self.prod / "README.md").write_text("локальный коммит на сервере\n", encoding="utf-8")
        self.git(self.prod, "commit", "-qam", "server local")
        (self.dev / "README.md").write_text("v2\n", encoding="utf-8")
        self.git(self.dev, "commit", "-qam", "v2")
        self.git(self.dev, "push", "-q", "origin", "HEAD:main")
        result = self.run_sync()
        self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
