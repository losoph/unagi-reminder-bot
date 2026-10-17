"""Тесты задачи DEP2: контроль версии контейнера, проверка живости и откат деплоя.

Проверяет:
1. Метка org.unagi.git_sha в docker-compose.yml и экспорт GIT_SHA в deploy.yml.
2. Откатный тег unagi-reminder-bot-bot:rollback в deploy.yml перед docker compose up.
3. Проверка живости в deploy.yml (блок # --- liveness) во временной среде с фейковым docker.
4. Ветки сверки меток в ops/aiqu/prod_check.sh (сверка container_sha и git HEAD).
"""

import os
import re
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEPLOY_YML = ROOT / ".github" / "workflows" / "deploy.yml"
COMPOSE_YML = ROOT / "docker-compose.yml"
PROD_CHECK = ROOT / "ops" / "aiqu" / "prod_check.sh"


def liveness_block() -> str:
    """Извлекает блок проверки живости контейнера из deploy.yml."""
    text = DEPLOY_YML.read_text(encoding="utf-8")
    match = re.search(r"# --- liveness[^\n]*\n(.*?)# --- end liveness", text, re.S)
    assert match, "в deploy.yml нет блока `# --- liveness … # --- end liveness`"
    return "set -e\n" + "\n".join(
        line.strip()
        for line in match.group(1).splitlines()
        if line.strip() and not line.strip().startswith("#")
    ) + "\n"


class Dep2ConfigTests(unittest.TestCase):
    def test_compose_has_git_sha_label(self):
        content = COMPOSE_YML.read_text(encoding="utf-8")
        self.assertIn("org.unagi.git_sha", content)
        self.assertIn("${GIT_SHA:-unknown}", content)

    def test_deploy_exports_git_sha_before_compose_up(self):
        content = DEPLOY_YML.read_text(encoding="utf-8")
        export_pos = content.find('export GIT_SHA="$(git rev-parse --short HEAD)"')
        compose_up_pos = content.find("docker compose up -d --build")
        self.assertNotEqual(export_pos, -1, "в deploy.yml должен быть экспорт GIT_SHA")
        self.assertNotEqual(compose_up_pos, -1, "в deploy.yml должен быть docker compose up -d --build")
        self.assertLess(export_pos, compose_up_pos, "GIT_SHA должен экспортироваться ДО docker compose up")

    def test_deploy_tags_rollback_image_before_compose_up(self):
        content = DEPLOY_YML.read_text(encoding="utf-8")
        tag_pos = content.find("docker tag unagi-reminder-bot-bot:latest unagi-reminder-bot-bot:rollback")
        compose_up_pos = content.find("docker compose up -d --build")
        self.assertNotEqual(tag_pos, -1, "в deploy.yml должно быть тегирование rollback")
        self.assertLess(tag_pos, compose_up_pos, "тегирование rollback должно происходить ДО сборки нового образа")

    def test_deploy_prune_does_not_use_all_flag(self):
        content = DEPLOY_YML.read_text(encoding="utf-8")
        match = re.search(r"docker image prune\s+([^\n]+)", content)
        self.assertIsNotNone(match)
        flags = match.group(1)
        self.assertNotIn("-a", flags)
        self.assertNotIn("--all", flags)


class Dep2LivenessExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bin_dir = Path(self.tmp.name) / "bin"
        self.bin_dir.mkdir(parents=True, exist_ok=True)
        self.log_file = Path(self.tmp.name) / "docker.log"

        # Создаём фейковый docker в PATH
        fake_docker = self.bin_dir / "docker"
        docker_script = f"""#!/usr/bin/env bash
cmd="$1"
shift || true
echo "docker $cmd $*" >> "{self.log_file}"
if [ "$cmd" = "inspect" ]; then
  if [ "${{FAKE_DOCKER_FAIL:-0}}" = "1" ]; then
    exit 1
  fi
  case "$*" in
    *State.Status*) echo "${{FAKE_STATUS:-running}}" ;;
    *State.RestartCount*) echo "${{FAKE_RESTARTS:-0}}" ;;
    *) echo "unknown inspect format" >&2; exit 1 ;;
  esac
elif [ "$cmd" = "logs" ]; then
  echo "fake container log tail"
  exit 0
else
  exit 0
fi
"""
        fake_docker.write_text(docker_script, encoding="utf-8")
        fake_docker.chmod(fake_docker.stat().st_mode | stat.S_IEXEC)

        # Фейковый sleep для мгновенного выполнения в тестах
        fake_sleep = self.bin_dir / "sleep"
        fake_sleep.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        fake_sleep.chmod(fake_sleep.stat().st_mode | stat.S_IEXEC)

        self.env = os.environ | {
            "PATH": f"{self.bin_dir}:{os.environ.get('PATH', '')}",
            "DEPLOY_WAIT_SECS": "0",
        }

    def run_liveness(self, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
        env = self.env | (extra_env or {})
        return subprocess.run(
            ["bash", "-c", liveness_block()],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_liveness_succeeds_when_container_running_and_no_restarts(self):
        result = self.run_liveness({"FAKE_STATUS": "running", "FAKE_RESTARTS": "0"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        log_content = self.log_file.read_text(encoding="utf-8")
        self.assertNotIn("docker logs", log_content)

    def test_liveness_fails_when_container_exited(self):
        result = self.run_liveness({"FAKE_STATUS": "exited", "FAKE_RESTARTS": "0"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("деплой остановлен", result.stdout + result.stderr)
        log_content = self.log_file.read_text(encoding="utf-8")
        self.assertIn("docker logs --tail 30 unagi-bot", log_content)

    def test_liveness_fails_when_container_is_restarting(self):
        result = self.run_liveness({"FAKE_STATUS": "restarting", "FAKE_RESTARTS": "1"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("деплой остановлен", result.stdout + result.stderr)
        log_content = self.log_file.read_text(encoding="utf-8")
        self.assertIn("docker logs --tail 30 unagi-bot", log_content)

    def test_liveness_fails_when_restart_count_is_positive(self):
        result = self.run_liveness({"FAKE_STATUS": "running", "FAKE_RESTARTS": "2"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("деплой остановлен", result.stdout + result.stderr)
        log_content = self.log_file.read_text(encoding="utf-8")
        self.assertIn("docker logs --tail 30 unagi-bot", log_content)

    def test_liveness_fails_when_docker_inspect_errors(self):
        result = self.run_liveness({"FAKE_DOCKER_FAIL": "1"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("деплой остановлен", result.stdout + result.stderr)


class Dep2ProdCheckTests(unittest.TestCase):
    def test_prod_check_inspects_git_sha_label(self):
        content = PROD_CHECK.read_text(encoding="utf-8")
        self.assertIn('docker inspect -f \'{{ index .Config.Labels "org.unagi.git_sha" }}\'', content)

    def _sample_remote_output(self, rev: str, container_sha: str) -> str:
        return (
            f"rev={rev}\n"
            "dirty=0\n"
            "container=running restarting=false started=2026-10-17\n"
            f"container_sha={container_sha}\n"
            "log_errors=0\n"
            "db_check=ok\n"
            "db_tables=10\n"
            "disk_pct=50\n"
            "backup_age_h=1\n"
            "mem=50MiB\n"
            "backups_raw=2\n"
            "backups_gz=12\n"
            "backups_mb=500\n"
            "db_mb=100\n"
            "compose_dirty=0\n"
            "unmerged=0\n"
            "conflict_markers=0\n"
            "proc=Uid: 10001 ;CapEff: 0000000000000000;NoNewPrivs: 1;\n"
            "email=True\n"
        )

    def run_prod_check(self, fake_out: str, expect: str = "abcdef1") -> subprocess.CompletedProcess:
        env = os.environ | {
            "PROD_CHECK_OUT": fake_out,
        }
        return subprocess.run(
            ["bash", str(PROD_CHECK), "--expect", expect],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_matching_sha_is_ok(self):
        fake_out = self._sample_remote_output(rev="abcdef1", container_sha="abcdef1")
        result = self.run_prod_check(fake_out, expect="abcdef1")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("OK контейнер: код abcdef1", result.stdout)
        self.assertNotIn("ALERT", result.stdout)

    def test_mismatched_sha_triggers_alert(self):
        fake_out = self._sample_remote_output(rev="abcdef1", container_sha="old1234")
        result = self.run_prod_check(fake_out, expect="abcdef1")
        self.assertEqual(result.returncode, 1, "При расхождении версий код выхода должен быть 1")
        self.assertIn("ALERT контейнер на старом коде: метка old1234, HEAD abcdef1", result.stdout)

    def test_unknown_sha_triggers_warn(self):
        fake_out = self._sample_remote_output(rev="abcdef1", container_sha="unknown")
        result = self.run_prod_check(fake_out, expect="abcdef1")
        self.assertEqual(result.returncode, 0, "WARN не должен ронять код выхода скрипта")
        self.assertIn("WARN контейнер: метка версии unknown/пустая (первый деплой после внедрения)", result.stdout)
        self.assertNotIn("ALERT", result.stdout)

    def test_empty_sha_triggers_warn(self):
        fake_out = self._sample_remote_output(rev="abcdef1", container_sha="")
        result = self.run_prod_check(fake_out, expect="abcdef1")
        self.assertEqual(result.returncode, 0)
        self.assertIn("WARN контейнер: метка версии unknown/пустая (первый деплой после внедрения)", result.stdout)
        self.assertNotIn("ALERT", result.stdout)

    def test_no_value_sha_triggers_warn(self):
        fake_out = self._sample_remote_output(rev="abcdef1", container_sha="<no value>")
        result = self.run_prod_check(fake_out, expect="abcdef1")
        self.assertEqual(result.returncode, 0)
        self.assertIn("WARN контейнер: метка версии unknown/пустая (первый деплой после внедрения)", result.stdout)
        self.assertNotIn("ALERT", result.stdout)

    def test_script_parse_verification(self):
        """Разбор скрипта prod_check.sh: извлекает логику обработки меток и проверяет условия."""
        content = PROD_CHECK.read_text(encoding="utf-8")
        self.assertIn('csha="$(get container_sha)"', content)
        self.assertIn('ALERT "контейнер на старом коде: метка $csha, HEAD $rev"', content)
        self.assertIn('WARN "контейнер: метка версии unknown/пустая (первый деплой после внедрения)"', content)


if __name__ == "__main__":
    unittest.main()
