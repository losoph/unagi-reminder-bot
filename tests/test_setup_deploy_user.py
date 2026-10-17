"""Тесты задачи OPS2: проверка скрипта настройки пользователя деплоя (ops/aiqu/setup_deploy_user.sh).

Проверяет:
1. Синтаксис скрипта через bash -n.
2. Флаг исполняемости скрипта.
3. Текстовые проверки (grep): создание deploy, docker, no password, no sudo, authorized_keys, chown проекта.
4. Поведение и идемпотентность скрипта в изолированной среде:
   - Проверка обязательности аргумента-файла с ключом;
   - Идемпотентность добавления ключей (повторный запуск не дублирует строки в authorized_keys).
"""

import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SETUP_SCRIPT = ROOT / "ops" / "aiqu" / "setup_deploy_user.sh"
README_MD = ROOT / "ops" / "aiqu" / "README.md"
DEPLOY_YML = ROOT / ".github" / "workflows" / "deploy.yml"


class SetupDeployUserTextTests(unittest.TestCase):
    """Синтаксис и текстовые (grep) проверки скрипта setup_deploy_user.sh и документации."""

    def setUp(self):
        self.assertTrue(SETUP_SCRIPT.is_file(), f"Файл {SETUP_SCRIPT} должен существовать")
        self.script_content = SETUP_SCRIPT.read_text(encoding="utf-8")

    def test_bash_syntax_n(self):
        """Проверка синтаксиса через bash -n."""
        res = subprocess.run(["bash", "-n", str(SETUP_SCRIPT)], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"Ошибка синтаксиса bash -n: {res.stderr}")

    def test_script_is_executable(self):
        """Скрипт должен иметь права на исполнение."""
        mode = SETUP_SCRIPT.stat().st_mode
        self.assertTrue(bool(mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)))

    def test_checks_root_execution(self):
        """Скрипт должен проверять запуск от root (id -u)."""
        self.assertRegex(self.script_content, r'id\s+-u')
        self.assertIn("ОШИБКА", self.script_content)

    def test_creates_deploy_user_and_locks_password(self):
        """Пользователь deploy создаётся без пароля."""
        self.assertIn('DEPLOY_USER="deploy"', self.script_content)
        self.assertTrue(
            'useradd -m -s /bin/bash "$DEPLOY_USER"' in self.script_content
            or 'useradd -m -s /bin/bash deploy' in self.script_content
        )
        # Блокировка пароля
        self.assertTrue(
            "passwd -l" in self.script_content or "usermod -p '*'" in self.script_content,
            "Пароль должен быть заблокирован (passwd -l или usermod -p '*')",
        )

    def test_assigns_docker_group(self):
        """Пользователь должен добавляться в группу docker."""
        self.assertIn("groupadd docker", self.script_content)
        self.assertIn('usermod -aG docker "$DEPLOY_USER"', self.script_content)

    def test_enforces_no_sudo(self):
        """Пользователь deploy должен быть без sudo."""
        self.assertIn("gpasswd -d", self.script_content)
        self.assertIn("/etc/sudoers.d", self.script_content)

    def test_idempotent_authorized_keys_handling(self):
        """Добавление ключей в authorized_keys должно быть идемпотентным (проверка наличия перед записью)."""
        self.assertIn("grep -Fxq", self.script_content, "Скрипт должен проверять точное совпадение ключа через grep -Fxq")
        self.assertIn("authorized_keys", self.script_content)
        self.assertIn("0700", self.script_content)
        self.assertIn("0600", self.script_content)

    def test_chowns_project_directory(self):
        """Владелец каталога проекта передаётся пользователю deploy."""
        self.assertIn("PROJECT_DIR", self.script_content)
        self.assertTrue(
            'chown -R "$DEPLOY_USER:$DEPLOY_USER" "$PROJECT_DIR"' in self.script_content
            or 'chown -R deploy:deploy "$PROJECT_DIR"' in self.script_content
        )

    def test_readme_documents_deploy_user_migration(self):
        """README.md должен содержать инструкции по секретам, root-шагам (cron) и откату."""
        readme = README_MD.read_text(encoding="utf-8")
        self.assertIn("setup_deploy_user.sh", readme)
        self.assertIn("USERNAME", readme)
        self.assertIn("/etc/cron.d", readme)
        self.assertIn("sudoers", readme)
        self.assertIn("откат", readme.lower())

    def test_deploy_yml_user_not_switched_yet(self):
        """В deploy.yml пользователь деплоя НЕ должен быть переключен (только подготовка)."""
        workflow = DEPLOY_YML.read_text(encoding="utf-8")
        self.assertIn("username: ${{ secrets.USERNAME }}", workflow)
        self.assertNotIn("username: deploy", workflow)

    def test_deploy_yml_has_checkout_pinned(self):
        """actions/checkout должен быть закреплен по коммиту."""
        workflow = DEPLOY_YML.read_text(encoding="utf-8")
        self.assertIn("actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1", workflow)

    def test_deploy_yml_has_host_tests_step(self):
        """deploy.yml должен содержать шаг прогона тестов на раннере для пропущенных в образе модулей."""
        workflow = DEPLOY_YML.read_text(encoding="utf-8")
        self.assertIn("python3 -m unittest", workflow)
        self.assertIn("tests/test_deploy_sync.py", workflow)
        self.assertIn("tests/test_orchestra.py", workflow)
        self.assertIn("tests/test_dep2_deploy_verify.py", workflow)


class SetupDeployUserExecutionTests(unittest.TestCase):
    """Изолированное тестирование выполнения setup_deploy_user.sh и проверка идемпотентности."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

        self.bin_dir = self.dir / "bin"
        self.bin_dir.mkdir(parents=True, exist_ok=True)
        self.fake_home = self.dir / "home" / "deploy"
        self.fake_home.mkdir(parents=True, exist_ok=True)
        self.fake_project = self.fake_home / "projects" / "unagi-reminder-bot"
        self.log_file = self.dir / "commands.log"

        # Создаём фейковые утилиты системного администрирования
        fake_id = self.bin_dir / "id"
        fake_id.write_text(f"""#!/bin/sh
echo "id $*" >> "{self.log_file}"
if [ "$1" = "-u" ]; then
    if [ -z "$2" ]; then
        # Проверка текущего пользователя — эмулируем root
        echo "0"
        exit 0
    elif [ "$2" = "deploy" ]; then
        if [ -f "{self.dir}/user_created" ]; then
            echo "1002"
            exit 0
        else
            exit 1
        fi
    fi
elif [ "$1" = "-Gn" ]; then
    echo "deploy docker"
    exit 0
fi
echo "0"
exit 0
""", encoding="utf-8")
        fake_id.chmod(0o755)

        fake_getent = self.bin_dir / "getent"
        fake_getent.write_text(f"""#!/bin/sh
echo "getent $*" >> "{self.log_file}"
if [ "$1" = "passwd" ] && [ "$2" = "deploy" ]; then
    echo "deploy:x:1002:1002:Deploy User:{self.fake_home}:/bin/bash"
    exit 0
elif [ "$1" = "group" ] && [ "$2" = "docker" ]; then
    exit 0
fi
exit 0
""", encoding="utf-8")
        fake_getent.chmod(0o755)

        fake_useradd = self.bin_dir / "useradd"
        fake_useradd.write_text(f"""#!/bin/sh
echo "useradd $*" >> "{self.log_file}"
touch "{self.dir}/user_created"
exit 0
""", encoding="utf-8")
        fake_useradd.chmod(0o755)

        fake_usermod = self.bin_dir / "usermod"
        fake_usermod.write_text(f"""#!/bin/sh
echo "usermod $*" >> "{self.log_file}"
exit 0
""", encoding="utf-8")
        fake_usermod.chmod(0o755)

        fake_passwd = self.bin_dir / "passwd"
        fake_passwd.write_text(f"""#!/bin/sh
echo "passwd $*" >> "{self.log_file}"
exit 0
""", encoding="utf-8")
        fake_passwd.chmod(0o755)

        fake_groupadd = self.bin_dir / "groupadd"
        fake_groupadd.write_text(f"""#!/bin/sh
echo "groupadd $*" >> "{self.log_file}"
exit 0
""", encoding="utf-8")
        fake_groupadd.chmod(0o755)

        fake_gpasswd = self.bin_dir / "gpasswd"
        fake_gpasswd.write_text(f"""#!/bin/sh
echo "gpasswd $*" >> "{self.log_file}"
exit 0
""", encoding="utf-8")
        fake_gpasswd.chmod(0o755)

        fake_chown = self.bin_dir / "chown"
        fake_chown.write_text(f"""#!/bin/sh
echo "chown $*" >> "{self.log_file}"
exit 0
""", encoding="utf-8")
        fake_chown.chmod(0o755)

        self.env = os.environ | {
            "PATH": f"{self.bin_dir}:{os.environ.get('PATH', '')}",
        }

    def test_missing_argument_fails(self):
        """Запуск без аргументов должен завершаться с ошибкой."""
        res = subprocess.run(["bash", str(SETUP_SCRIPT)], env=self.env, capture_output=True, text=True)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("Использование", res.stderr)

    def test_nonexistent_key_file_fails(self):
        """Запуск с несуществующим файлом ключа должен завершаться с ошибкой."""
        res = subprocess.run(
            ["bash", str(SETUP_SCRIPT), str(self.dir / "nonexistent.pub")],
            env=self.env,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("не существует", res.stderr)

    def test_empty_key_file_fails(self):
        """Запуск с пустым файлом ключа должен завершаться с ошибкой."""
        empty_key = self.dir / "empty.pub"
        empty_key.touch()
        res = subprocess.run(
            ["bash", str(SETUP_SCRIPT), str(empty_key)],
            env=self.env,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("пуст", res.stderr)

    def test_idempotent_execution(self):
        """Повторный запуск скрипта не дублирует строки ключей в authorized_keys."""
        key_file = self.dir / "deploy.pub"
        test_key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleDeployKey12345 github-actions-deploy"
        key_file.write_text(f"{test_key}\n", encoding="utf-8")

        # Первый запуск
        res1 = subprocess.run(
            ["bash", str(SETUP_SCRIPT), str(key_file), str(self.fake_project)],
            env=self.env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(res1.returncode, 0, f"STDOUT: {res1.stdout}\nSTDERR: {res1.stderr}")
        self.assertIn("Ключ добавлен в authorized_keys", res1.stdout)

        auth_keys = self.fake_home / ".ssh" / "authorized_keys"
        self.assertTrue(auth_keys.is_file())
        content1 = auth_keys.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(content1, [test_key])

        # Второй запуск (проверка идемпотентности)
        res2 = subprocess.run(
            ["bash", str(SETUP_SCRIPT), str(key_file), str(self.fake_project)],
            env=self.env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(res2.returncode, 0, f"STDOUT: {res2.stdout}\nSTDERR: {res2.stderr}")
        self.assertIn("Ключ уже присутствует в authorized_keys (пропуск)", res2.stdout)

        content2 = auth_keys.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(content2, [test_key], "authorized_keys не должен содержать дубликатов")


if __name__ == "__main__":
    unittest.main()
