"""Конфигурация оркестра агентов (tools/agents/orchestra.json) и команда `agents.sh launch`."""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools" / "agents"


def load_orchestra():
    spec = importlib.util.spec_from_file_location("orchestra", TOOLS / "orchestra.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_progress_index():
    spec = importlib.util.spec_from_file_location("progress_index", TOOLS / "progress_index.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class OrchestraConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.original = os.environ.get("AGENT_ORCHESTRA")
        self.addCleanup(self.restore)
        self.orchestra = load_orchestra()

    def restore(self):
        if self.original is None:
            os.environ.pop("AGENT_ORCHESTRA", None)
        else:
            os.environ["AGENT_ORCHESTRA"] = self.original

    def mutated(self, change):
        data = json.loads((TOOLS / "orchestra.json").read_text(encoding="utf-8"))
        change(data)
        path = self.dir / "orchestra.json"
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.environ["AGENT_ORCHESTRA"] = str(path)
        return self.orchestra.validate()

    def test_shipped_config_is_valid(self):
        os.environ.pop("AGENT_ORCHESTRA", None)
        self.assertEqual(self.orchestra.validate(), [])

    def test_free_agents_come_before_paid_and_opencode_is_last(self):
        os.environ.pop("AGENT_ORCHESTRA", None)
        for kind in self.orchestra.KINDS:
            steps = [step for step in self.orchestra.ladder(kind) if step not in self.orchestra.manual_agents()]
            self.assertEqual(steps[:2], ["agy", "dsh"], kind)
            if "opencode" in steps:
                self.assertEqual(steps[-1], "opencode", kind)

    def test_paid_agent_before_free_one_is_rejected(self):
        problems = self.mutated(lambda data: data["ladder"].update({"executor-complex": ["dsh", "agy"]}))
        self.assertTrue(any("приоритет стоимости" in line for line in problems), problems)

    def test_unknown_agent_and_missing_limits_are_reported(self):
        problems = self.mutated(lambda data: (data["ladder"].update({"docs": ["agy", "ghost"]}), data["limits"].pop("monitor_digest_sec")))
        self.assertTrue(any("ghost" in line for line in problems), problems)
        self.assertTrue(any("monitor_digest_sec" in line for line in problems), problems)

    def test_missing_file_is_reported_and_policy_falls_back_to_builtin_order(self):
        os.environ["AGENT_ORCHESTRA"] = str(self.dir / "absent.json")
        self.assertTrue(self.orchestra.validate())
        spec = importlib.util.spec_from_file_location("executor_policy_fb", TOOLS / "executor_policy.py")
        policy = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(policy)
        self.assertEqual(policy.executor_order("executor-complex"), ["agy", "dsh"])
        self.assertFalse(policy.opencode_allowed("executor-complex"))
        self.assertTrue(policy.opencode_allowed("short-check"))


@unittest.skipUnless(shutil.which("git"), "в образе CI (python:3.12-slim) нет git: launch проверяет историю репозитория")
class LaunchTests(unittest.TestCase):
    """agents.sh launch: проверки перед запуском трека и готовые команды."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.repo = self.dir / "repo"
        (self.repo / "docs").mkdir(parents=True)
        self.env = os.environ | {
            "AGENT_ROUTER": "static",
            "AGENT_ENV_FILE": os.devnull,
            "AGENT_ROUTE_LOG": str(self.dir / "routes.ndjson"),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
        }
        self.git("init", "-q")
        (self.repo / "docs" / "brief.md").write_text("задача\n", encoding="utf-8")
        self.git("add", "docs/brief.md")
        self.git("commit", "-q", "-m", "docs: бриф задачи")

    def git(self, *args, check=True):
        return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.test", *args], cwd=self.repo, env=self.env, text=True, capture_output=True, check=check)

    def launch(self, *args):
        return subprocess.run([str(TOOLS / "agents.sh"), "launch", *args], env=self.env, text=True, capture_output=True, check=False)

    def test_agy_prints_supervisor_and_monitor_commands(self):
        proc = self.launch("agy", str(self.repo), "docs/brief.md", "(T1)", "executor-complex")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(f"cd {self.repo} && AGY_LOG_DIR=.agy AGY_MODEL=", proc.stdout)
        self.assertIn("tools/agents/agy_supervise.sh docs/brief.md '(T1)' 6", proc.stdout)
        self.assertIn("tools/agents/agy_watch.sh digest 240", proc.stdout)
        self.assertIn("вкладок терминала не больше 6", proc.stdout)

    def test_dsh_prints_run_and_watch_commands(self):
        proc = self.launch("dsh", str(self.repo), "docs/brief.md", "T2", "executor-complex")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("agents.sh run dsh executor-complex", proc.stdout)
        self.assertIn(".agents/T2.ndjson T2", proc.stdout)
        self.assertIn("agents.sh watch dsh .agents/T2.ndjson", proc.stdout)

    def test_untracked_or_modified_brief_is_refused(self):
        (self.repo / "docs" / "new.md").write_text("x\n", encoding="utf-8")
        proc = self.launch("agy", str(self.repo), "docs/new.md", "(T3)")
        self.assertEqual(proc.returncode, 3)
        self.assertIn("не закоммичен", proc.stderr)
        (self.repo / "docs" / "brief.md").write_text("правка\n", encoding="utf-8")
        proc = self.launch("agy", str(self.repo), "docs/brief.md", "(T3)")
        self.assertEqual(proc.returncode, 3)
        self.assertIn("изменён после коммита", proc.stderr)

    def test_marker_already_in_history_is_refused_with_the_matching_commit(self):
        # 17.10: «(Q4D-fix)» попал в сообщение коммита с брифом — супервизор счёл работу сделанной и agy не запустился.
        (self.repo / "docs" / "brief2.md").write_text("задача 2\n", encoding="utf-8")
        self.git("add", "docs/brief2.md")
        self.git("commit", "-q", "-m", "docs: бриф правок (T4)")
        proc = self.launch("agy", str(self.repo), "docs/brief2.md", "(T4)")
        self.assertEqual(proc.returncode, 3)
        self.assertIn("уже есть в истории", proc.stderr)
        self.assertIn("docs: бриф правок (T4)", proc.stderr)
        self.assertIn("git commit --amend", proc.stderr)

    def test_unsupported_agent_is_rejected(self):
        proc = self.launch("opencode", str(self.repo), "docs/brief.md", "(T5)")
        self.assertEqual(proc.returncode, 2)

    def test_brief_demanding_append_to_progress_md_warns_without_error(self):
        (self.repo / "docs" / "legacy.md").write_text("допиши в docs/sprint/progress.md блок T6\n", encoding="utf-8")
        self.git("add", "docs/legacy.md")
        self.git("commit", "-q", "-m", "docs: бриф со старым протоколом")
        proc = self.launch("agy", str(self.repo), "docs/legacy.md", "(T6)")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("docs/sprint/progress.md", proc.stderr)
        self.assertIn("docs/sprint/progress/<МЕТКА>.md", proc.stderr)


class ProgressIndexTests(unittest.TestCase):
    """Сборка индекса журнала решений (tools/agents/progress_index.py)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.progress_dir = self.dir / "docs" / "sprint" / "progress"
        self.progress_dir.mkdir(parents=True)
        self.mod = load_progress_index()

    def test_synthetic_directory_index_collection_and_formatting(self):
        (self.progress_dir / "README.md").write_text("# Игнорируемый индекс\n", encoding="utf-8")
        (self.progress_dir / "T10.md").write_text(
            "## T10 Управление алертами\n- Решение: inline-кнопки\n- Компромисс: нет\n",
            encoding="utf-8",
        )
        (self.progress_dir / "PRG1.md").write_text(
            "# PRG1 Убрать конфликты — (PRG1)\n- Решение: per-task файлы\n",
            encoding="utf-8",
        )

        entries = self.mod.collect_progress_entries(self.progress_dir)
        labels = [e["label"] for e in entries]
        self.assertIn("T10", labels)
        self.assertIn("PRG1", labels)
        self.assertNotIn("README", labels)

        t10_entry = next(e for e in entries if e["label"] == "T10")
        self.assertEqual(t10_entry["title"], "T10 Управление алертами")
        self.assertEqual(t10_entry["filename"], "T10.md")
        self.assertTrue(t10_entry["commit_date"])

        md = self.mod.format_index_markdown(entries)
        self.assertIn("| Метка | Дата коммита | Заголовок | Файл |", md)
        self.assertIn("PRG1", md)
        self.assertIn("T10", md)
        self.assertIn("T10.md", md)

    def test_cli_execution_on_synthetic_dir(self):
        (self.progress_dir / "SEC1.md").write_text("## SEC1 Безопасность\n- Решение: правки\n", encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, str(TOOLS / "progress_index.py"), str(self.progress_dir)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("SEC1", proc.stdout)
        self.assertIn("Безопасность", proc.stdout)


if __name__ == "__main__":
    unittest.main()

