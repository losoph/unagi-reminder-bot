"""Ужесточение контейнера (ревью безопасности, п.7): точка входа, Dockerfile, compose и смоук-проверка в CI согласованы."""

import os
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = ROOT / "ops" / "docker" / "entrypoint.sh"


def read(*parts: str) -> str:
    return ROOT.joinpath(*parts).read_text(encoding="utf-8")


def compose_list(key: str) -> list[str]:
    """Элементы блока `key:` сервиса bot в docker-compose.yml (простые списки `- значение`)."""
    match = re.search(rf"^    {key}:\n((?:      - .+\n)+)", read("docker-compose.yml"), re.MULTILINE)
    return [line.strip()[2:].strip() for line in match.group(1).splitlines()] if match else []


class EntrypointTests(unittest.TestCase):
    def test_syntax(self):
        subprocess.run(["sh", "-n", str(ENTRYPOINT)], check=True)

    @unittest.skipIf(os.getuid() == 0, "от root точка входа меняет владельца storage и сбрасывает права")
    def test_runs_command_as_is_without_root(self):
        result = subprocess.run([str(ENTRYPOINT), "sh", "-c", 'printf "%s|%s" "$1" "$2"', "sh", "a b", "c"], capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout, "a b|c")

    def test_uid_matches_dockerfile(self):
        uid = re.search(r"^BOT_UID=(\d+)$", ENTRYPOINT.read_text(encoding="utf-8"), re.MULTILINE).group(1)
        gid = re.search(r"^BOT_GID=(\d+)$", ENTRYPOINT.read_text(encoding="utf-8"), re.MULTILINE).group(1)
        dockerfile = read("Dockerfile")
        self.assertIn(f"--gid {gid} bot", dockerfile)
        self.assertIn(f"--uid {uid} --gid {gid}", dockerfile)
        self.assertIn('ENTRYPOINT ["docker-entrypoint.sh"]', dockerfile)

    def test_root_stage_ends_with_dropping_privileges(self):
        script = ENTRYPOINT.read_text(encoding="utf-8")
        self.assertRegex(script, r'exec setpriv --reuid="\$BOT_UID" --regid="\$BOT_GID" --clear-groups --no-new-privs "\$@"')


class ComposeTests(unittest.TestCase):
    def test_container_is_hardened(self):
        compose = read("docker-compose.yml")
        self.assertEqual(compose_list("cap_drop"), ["ALL"])
        self.assertEqual(compose_list("security_opt"), ["no-new-privileges:true"])
        self.assertIn("    read_only: true\n", compose)
        self.assertIn("MSMTPRC_PATH: /run/unagi/msmtprc", compose)
        self.assertTrue(any(item.startswith("/run/unagi:") for item in compose_list("tmpfs")))
        self.assertTrue(any(item.startswith("/tmp:") for item in compose_list("tmpfs")))

    def test_cpu_limit_default_fits_a_single_vcpu_host(self):
        # 17.10: деплой упал на проде — Docker отвергает cpus выше числа ядер (хост 1 vCPU: «range of CPUs is from 0.01 to 1.00»).
        match = re.search(r"cpus: \$\{BOT_CPUS:-([0-9.]+)\}", read("docker-compose.yml"))
        self.assertIsNotNone(match)
        self.assertLessEqual(float(match.group(1)), 1.0)

    def test_ci_smoke_uses_compose_capabilities(self):
        workflow = read(".github", "workflows", "deploy.yml")
        in_ci = set(re.findall(r"--cap-add (\w+)", workflow))
        self.assertEqual(in_ci, set(compose_list("cap_add")))
        self.assertIn("--cap-drop ALL", workflow)
        self.assertIn("--security-opt no-new-privileges:true", workflow)
        self.assertIn("--read-only", workflow)
        self.assertIn("ops/docker/smoke.py", workflow)


if __name__ == "__main__":
    unittest.main()
