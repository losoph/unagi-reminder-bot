"""Тесты задачи DEP2: контроль версии контейнера, проверка живости и откат деплоя.

Проверяет:
1. Метка org.unagi.git_sha в docker-compose.yml и экспорт GIT_SHA в deploy.yml.
2. Откатный тег unagi-reminder-bot-bot:rollback в deploy.yml из образа работающего контейнера.
3. Проверка живости в deploy.yml (блок # --- liveness) с проверкой .RestartCount и org.unagi.git_sha.
4. Ветки сверки меток в ops/aiqu/prod_check.sh через фейковый ssh (без обхода через PROD_CHECK_OUT).
5. Политика очистки rollback-образа при заполнении диска >= 85%.
6. Инструкции отката и восстановления БД в ops/aiqu/README.md.
"""

import os
import re
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEPLOY_YML = ROOT / ".github" / "workflows" / "deploy.yml"
COMPOSE_YML = ROOT / "docker-compose.yml"
PROD_CHECK = ROOT / "ops" / "aiqu" / "prod_check.sh"
README_MD = ROOT / "ops" / "aiqu" / "README.md"


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


def rollback_block() -> str:
    """Извлекает блок автоотката из deploy.yml."""
    text = DEPLOY_YML.read_text(encoding="utf-8")
    match = re.search(r"# --- rollback[^\n]*\n(.*?)# --- end rollback", text, re.S)
    assert match, "в deploy.yml нет блока `# --- rollback … # --- end rollback`"
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

    def test_deploy_tags_rollback_image_from_running_container_before_compose_up(self):
        content = DEPLOY_YML.read_text(encoding="utf-8")
        self.assertNotIn(
            "docker tag unagi-reminder-bot-bot:latest unagi-reminder-bot-bot:rollback",
            content,
            "Тег rollback не должен браться из latest (latest — последний собранный, а не обязательно рабочий)",
        )
        inspect_pos = content.find("docker inspect -f '{{.Image}}' unagi-bot")
        tag_pos = content.find('docker tag "$current_image" unagi-reminder-bot-bot:rollback')
        compose_up_pos = content.find("docker compose up -d --build")
        self.assertNotEqual(inspect_pos, -1, "в deploy.yml должен определяться образ работающего контейнера unagi-bot")
        self.assertNotEqual(tag_pos, -1, "в deploy.yml должно быть тегирование rollback из current_image")
        self.assertLess(inspect_pos, compose_up_pos, "тегирование rollback должно происходить ДО сборки нового образа")
        self.assertLess(tag_pos, compose_up_pos)

    def test_deploy_liveness_uses_root_restart_count_field(self):
        content = DEPLOY_YML.read_text(encoding="utf-8")
        self.assertNotIn(
            "State.RestartCount",
            content,
            "Поле RestartCount находится в корне inspect контейнера, а не в .State",
        )
        self.assertIn(
            "docker inspect -f '{{.RestartCount}}' unagi-bot",
            content,
            "Liveness должен запрашивать {{.RestartCount}}",
        )

    def test_deploy_liveness_checks_git_sha_label(self):
        content = DEPLOY_YML.read_text(encoding="utf-8")
        self.assertIn("org.unagi.git_sha", content)
        self.assertIn("container_sha", content)
        self.assertIn('"$container_sha" != "$GIT_SHA"', content)

    def test_deploy_prune_does_not_use_all_flag(self):
        content = DEPLOY_YML.read_text(encoding="utf-8")
        match = re.search(r"docker image prune\s+([^\n]+)", content)
        self.assertIsNotNone(match)
        flags = match.group(1)
        self.assertNotIn("-a", flags)
        self.assertNotIn("--all", flags)

    def test_deploy_has_rollback_disk_cleanup_policy(self):
        content = DEPLOY_YML.read_text(encoding="utf-8")
        self.assertIn("df --output=pcent", content, "deploy.yml должен проверять процент занятого места на диске")
        self.assertIn("85", content, "Порог очистки должен соответствовать порогу ALERT (85%)")
        self.assertIn("docker rmi unagi-reminder-bot-bot:rollback", content)

    def test_readme_rollback_instructions_and_db_warning(self):
        content = README_MD.read_text(encoding="utf-8")
        # П. 5: git checkout и export GIT_SHA ДО docker compose up
        co_pos = content.find("git checkout <sha>")
        export_pos = content.find('export GIT_SHA="$(git rev-parse --short HEAD)"')
        up_pos = content.find("docker compose up -d\n")
        self.assertNotEqual(co_pos, -1)
        self.assertNotEqual(export_pos, -1)
        self.assertNotEqual(up_pos, -1)
        self.assertLess(co_pos, up_pos, "git checkout должен быть ДО docker compose up")
        self.assertLess(export_pos, up_pos, "export GIT_SHA должен быть ДО docker compose up")
        self.assertIn("--expect <sha>", content, "README должен требовать --expect <sha> для prod_check при откате")
        # П. 8: предупреждение о БД и миграциях
        self.assertIn("predeploy.db", content, "README должен содержать инструкцию по восстановлению снимка БД при откате")
        self.assertIn("миграци", content, "README должен предупреждать о миграциях БД при откате")

    def test_deploy_tags_rollback_only_when_container_healthy(self):
        content = DEPLOY_YML.read_text(encoding="utf-8")
        self.assertIn("current_status", content)
        self.assertIn("current_restarts", content)
        self.assertIn('"$current_status" = "running"', content)
        self.assertIn('"$current_restarts" = "0"', content)

    def test_deploy_cleans_disk_before_build(self):
        content = DEPLOY_YML.read_text(encoding="utf-8")
        pre_disk_pos = content.find("disk_pre=")
        compose_up_pos = content.find("docker compose up -d --build")
        self.assertNotEqual(pre_disk_pos, -1, "deploy.yml должен проверять диск до сборки")
        self.assertLess(pre_disk_pos, compose_up_pos, "Проверка диска должна быть ДО сборки")
        self.assertIn("docker builder prune -f", content[:compose_up_pos])
        self.assertIn("docker image prune -f", content[:compose_up_pos])

    def test_deploy_runs_backup_as_non_root(self):
        content = DEPLOY_YML.read_text(encoding="utf-8")
        self.assertIn("setpriv --reuid=10001 --regid=10001 --clear-groups python3 ops/aiqu/backup_db.py", content)
        self.assertIn("chown -R 10001:10001 storage", content)

    def test_prod_check_sqlite_runs_as_non_root(self):
        content = PROD_CHECK.read_text(encoding="utf-8")
        self.assertIn("setpriv --reuid=10001 --regid=10001 --clear-groups sqlite3 -readonly", content)


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
    *unagi-reminder-bot-bot:rollback*)
      if [ "${{FAKE_ROLLBACK_EXISTS:-1}}" = "1" ]; then
        echo "sha256:rollback123"
        exit 0
      else
        exit 1
      fi
      ;;
    *State.Status*) echo "${{FAKE_STATUS:-running}}" ;;
    *State.RestartCount*)
      # В реальном Docker в .State нет поля RestartCount: Go template выводит <no value>
      echo "<no value>"
      ;;
    *RestartCount*) echo "${{FAKE_RESTARTS:-0}}" ;;
    *org.unagi.git_sha*) echo "${{FAKE_CONTAINER_SHA-${{GIT_SHA:-abcdef1}}}}" ;;
    *Image*) echo "${{FAKE_IMAGE:-sha256:current123}}" ;;
    *) echo "unknown inspect format: $*" >&2; exit 1 ;;
  esac
elif [ "$cmd" = "exec" ]; then
  if [ "${{FAKE_READY:-1}}" = "1" ]; then
    exit 0
  else
    exit 1
  fi
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
            "GIT_SHA": "abcdef1",
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

    def test_liveness_fails_when_container_sha_mismatched(self):
        result = self.run_liveness({
            "FAKE_STATUS": "running",
            "FAKE_RESTARTS": "0",
            "FAKE_CONTAINER_SHA": "old1234",
            "GIT_SHA": "abcdef1",
        })
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("метка версии не совпадает", result.stdout + result.stderr)
        log_content = self.log_file.read_text(encoding="utf-8")
        self.assertIn("docker logs --tail 30 unagi-bot", log_content)

    def test_liveness_fails_when_container_sha_missing(self):
        result = self.run_liveness({
            "FAKE_STATUS": "running",
            "FAKE_RESTARTS": "0",
            "FAKE_CONTAINER_SHA": "",
            "GIT_SHA": "abcdef1",
        })
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("метка версии не совпадает", result.stdout + result.stderr)

    def test_liveness_fails_when_docker_inspect_errors(self):
        result = self.run_liveness({"FAKE_DOCKER_FAIL": "1"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("деплой остановлен", result.stdout + result.stderr)

    def test_liveness_fails_when_ready_marker_missing(self):
        result = self.run_liveness({"FAKE_READY": "0"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("деплой остановлен", result.stdout + result.stderr)
        log_content = self.log_file.read_text(encoding="utf-8")
        self.assertIn("docker logs --tail 30 unagi-bot", log_content)

    def test_liveness_allows_single_restart_if_stable(self):
        result = self.run_liveness({"FAKE_STATUS": "running", "FAKE_RESTARTS": "1", "FAKE_READY": "1"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_liveness_triggers_auto_rollback_on_failure(self):
        result = self.run_liveness({"FAKE_STATUS": "exited", "FAKE_ROLLBACK_EXISTS": "1"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("откат: запуск контейнера из unagi-reminder-bot-bot:rollback", result.stdout + result.stderr)
        self.assertIn("откат выполнен", result.stdout + result.stderr)
        log_content = self.log_file.read_text(encoding="utf-8")
        self.assertIn("docker tag unagi-reminder-bot-bot:rollback unagi-reminder-bot-bot:latest", log_content)
        self.assertIn("docker compose up -d", log_content)

    def test_liveness_skips_rollback_when_no_rollback_image(self):
        result = self.run_liveness({"FAKE_STATUS": "exited", "FAKE_ROLLBACK_EXISTS": "0"})
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("откат выполнен", result.stdout + result.stderr)
        log_content = self.log_file.read_text(encoding="utf-8")
        self.assertNotIn("docker tag unagi-reminder-bot-bot:rollback", log_content)

    def test_rollback_block_isolated_execution(self):
        res = subprocess.run(
            ["bash", "-c", rollback_block()],
            env=self.env | {"FAKE_ROLLBACK_EXISTS": "1"},
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("откат: запуск контейнера из unagi-reminder-bot-bot:rollback", res.stdout)
        self.assertIn("откат выполнен", res.stdout)
        log_content = self.log_file.read_text(encoding="utf-8")
        self.assertIn("docker tag unagi-reminder-bot-bot:rollback unagi-reminder-bot-bot:latest", log_content)
        self.assertIn("docker compose up -d", log_content)

    def test_fake_docker_rejects_state_restart_count_simulation(self):
        inspect_res = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.RestartCount}}", "unagi-bot"],
            env=self.env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(inspect_res.stdout.strip(), "<no value>")


class Dep2ProdCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bin_dir = Path(self.tmp.name) / "bin"
        self.bin_dir.mkdir(parents=True, exist_ok=True)
        self.ssh_log = Path(self.tmp.name) / "ssh.log"

        # Создаём фейковый ssh в PATH, убирая бэкдор PROD_CHECK_OUT
        fake_ssh = self.bin_dir / "ssh"
        ssh_script = f"""#!/usr/bin/env bash
echo "ssh $*" >> "{self.ssh_log}"
if [ "${{FAKE_SSH_FAIL:-0}}" = "1" ]; then
  echo "ssh: connect to host failed: Connection refused" >&2
  exit 255
fi
if [ -n "${{FAKE_SSH_OUT:-}}" ]; then
  printf '%s\\n' "${{FAKE_SSH_OUT}}"
  exit 0
else
  echo "no ssh output" >&2
  exit 1
fi
"""
        fake_ssh.write_text(ssh_script, encoding="utf-8")
        fake_ssh.chmod(fake_ssh.stat().st_mode | stat.S_IEXEC)

        self.env = os.environ | {
            "PATH": f"{self.bin_dir}:{os.environ.get('PATH', '')}",
        }

    def test_prod_check_inspects_git_sha_label(self):
        content = PROD_CHECK.read_text(encoding="utf-8")
        self.assertIn('docker inspect -f \'{{ index .Config.Labels "org.unagi.git_sha" }}\'', content)

    def test_prod_check_does_not_contain_prod_check_out(self):
        content = PROD_CHECK.read_text(encoding="utf-8")
        self.assertNotIn(
            "PROD_CHECK_OUT",
            content,
            "prod_check.sh не должен содержать бэкдор PROD_CHECK_OUT в обход ssh",
        )

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

    def run_prod_check(self, fake_out: str, expect: str = "abcdef1", ssh_fail: bool = False) -> subprocess.CompletedProcess:
        env = self.env | {
            "FAKE_SSH_OUT": fake_out,
            "FAKE_SSH_FAIL": "1" if ssh_fail else "0",
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
        # Проверяем, что ssh действительно вызывался
        self.assertIn("ssh", self.ssh_log.read_text(encoding="utf-8"))

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

    def test_prod_check_fails_on_ssh_error(self):
        result = self.run_prod_check("", ssh_fail=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("ALERT ssh: нет связи", result.stdout + result.stderr)

    def test_script_parse_verification(self):
        """Разбор скрипта prod_check.sh: извлекает логику обработки меток и проверяет условия."""
        content = PROD_CHECK.read_text(encoding="utf-8")
        self.assertIn('csha="$(get container_sha)"', content)
        self.assertIn('ALERT "контейнер на старом коде: метка $csha, HEAD $rev"', content)
        self.assertIn('WARN "контейнер: метка версии unknown/пустая (первый деплой после внедрения)"', content)


def is_docker_daemon_available() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        res = subprocess.run(["docker", "info"], capture_output=True, timeout=2)
        return res.returncode == 0
    except Exception:
        return False


@unittest.skipUnless(is_docker_daemon_available(), "требуется запущенный Docker daemon")
class Dep2RealDockerInspectTests(unittest.TestCase):
    def test_real_docker_inspect_restart_count(self):
        """Проверка на реальном Docker, что .RestartCount валиден, а .State.RestartCount не существует."""
        run_res = subprocess.run(
            ["docker", "run", "-d", "--name", "test-dep2-probe", "alpine", "sleep", "10"],
            capture_output=True,
            text=True,
        )
        if run_res.returncode != 0:
            self.skipTest(f"не удалось запустить контейнер: {run_res.stderr}")
        try:
            inspect_correct = subprocess.run(
                ["docker", "inspect", "-f", "{{.RestartCount}}", "test-dep2-probe"],
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertEqual(inspect_correct.stdout.strip(), "0")

            inspect_wrong = subprocess.run(
                ["docker", "inspect", "-f", "{{.State.RestartCount}}", "test-dep2-probe"],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(inspect_wrong.stdout.strip(), "0")
        finally:
            subprocess.run(["docker", "rm", "-f", "test-dep2-probe"], capture_output=True)


if __name__ == "__main__":
    unittest.main()
