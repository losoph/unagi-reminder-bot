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
from unittest import mock

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

    def test_brief_with_per_task_directory_and_negation_does_not_warn(self):
        # 18.10: бриф с новым протоколом не должен получать ложное предупреждение
        brief_text = (
            "Задача T7.\n"
            "блок решений зафиксируй в отдельном файле docs/sprint/progress/<TASK>.md "
            "(в том же коммите, не дописывай в progress.md).\n"
        )
        (self.repo / "docs" / "modern.md").write_text(brief_text, encoding="utf-8")
        self.git("add", "docs/modern.md")
        self.git("commit", "-q", "-m", "docs: бриф с новым протоколом")
        proc = self.launch("agy", str(self.repo), "docs/modern.md", "(T7)")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("⚠ бриф требует дописывать", proc.stderr)


class BriefProgressWarningTests(unittest.TestCase):
    """Проверка брифа на требование дописывать в старый docs/sprint/progress.md.

    Работает в любой среде (в том числе в CI python:3.12-slim без git).
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.mod = load_progress_index()

    def test_old_brief_full_path_triggers_warning(self):
        brief = self.dir / "legacy1.md"
        brief.write_text("допиши в docs/sprint/progress.md блок T6\n", encoding="utf-8")
        warn = self.mod.check_brief_warning(brief)
        self.assertIsNotNone(warn)
        self.assertIn("docs/sprint/progress.md", warn)
        self.assertIn("docs/sprint/progress/<МЕТКА>.md", warn)

    def test_old_brief_short_path_and_block_phrase_triggers_warning(self):
        # Случай из отчёта ревью (docs/sprint/agy-review-fixes.md): нет полного пути и корня «допис»
        brief = self.dir / "legacy2.md"
        brief.write_text("коротким блоком «T14b» в progress.md, затем продолжай\n", encoding="utf-8")
        warn = self.mod.check_brief_warning(brief)
        self.assertIsNotNone(warn)
        self.assertIn("docs/sprint/progress.md", warn)

        brief3 = self.dir / "legacy3.md"
        brief3.write_text("Блок «AI1» в docs/sprint/progress.md\n", encoding="utf-8")
        warn3 = self.mod.check_brief_warning(brief3)
        self.assertIsNotNone(warn3)

    def test_negative_phrases_and_per_task_directory_do_not_trigger_warning(self):
        # Случай из отчёта ревью: ложное срабатывание на fix-шаблоне и agy-brief-prg1
        fix_template = (TOOLS / "templates" / "fix.md").read_text(encoding="utf-8")
        self.assertIsNone(self.mod.check_brief_warning(fix_template))

        review_template = (TOOLS / "templates" / "review.md").read_text(encoding="utf-8")
        self.assertIsNone(self.mod.check_brief_warning(review_template))

        prg1_brief = (ROOT / "docs" / "sprint" / "agy-brief-prg1.md").read_text(encoding="utf-8")
        self.assertIsNone(self.mod.check_brief_warning(prg1_brief))

        synthetic_valid = self.dir / "valid.md"
        synthetic_valid.write_text(
            "блок решений зафиксируй в отдельном файле docs/sprint/progress/<TASK>.md (не дописывай в progress.md)\n",
            encoding="utf-8",
        )
        self.assertIsNone(self.mod.check_brief_warning(synthetic_valid))

    def test_cli_check_brief_stderr_and_exit_code(self):
        legacy = self.dir / "legacy.md"
        legacy.write_text("допиши в docs/sprint/progress.md блок T6\n", encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, str(TOOLS / "progress_index.py"), "--check-brief", str(legacy)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertIn("docs/sprint/progress.md", proc.stderr)
        self.assertIn("docs/sprint/progress/<МЕТКА>.md", proc.stderr)

        valid = self.dir / "valid.md"
        valid.write_text("блок решений в docs/sprint/progress/T1.md\n", encoding="utf-8")
        proc_ok = subprocess.run(
            [sys.executable, str(TOOLS / "progress_index.py"), "--check-brief", str(valid)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc_ok.returncode, 0)
        self.assertEqual(proc_ok.stderr, "")


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
            "## PRG1 Убрать конфликты — 64a710a\n- Решение: per-task файлы\n",
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

    def test_get_commit_date_git_mocked(self):
        file_path = self.progress_dir / "T10.md"
        file_path.write_text("## T10\n", encoding="utf-8")
        with mock.patch("subprocess.run") as m_run:
            m_run.return_value = subprocess.CompletedProcess(
                args=["git", "log", "-1", "--format=%cs", "--", "T10.md"],
                returncode=0,
                stdout="2026-10-17\n",
                stderr="",
            )
            date = self.mod.get_commit_date(file_path)
            self.assertEqual(date, "2026-10-17")
            m_run.assert_called_once()
            args, kwargs = m_run.call_args
            self.assertEqual(args[0], ["git", "log", "-1", "--format=%cs", "--", "T10.md"])
            self.assertEqual(kwargs["cwd"], str(self.progress_dir))

    def test_output_protection_prevents_overwriting_source_progress_file(self):
        source = self.progress_dir / "T10.md"
        original_content = "## T10 Управление алертами\n- Решение: inline-кнопки\n"
        source.write_text(original_content, encoding="utf-8")

        proc = subprocess.run(
            [sys.executable, str(TOOLS / "progress_index.py"), str(self.progress_dir), "-o", str(source)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("защита от перезаписи", proc.stderr)
        self.assertEqual(source.read_text(encoding="utf-8"), original_content)

    def test_format_index_markdown_includes_archive_link(self):
        entries = self.mod.collect_progress_entries(self.progress_dir)
        md = self.mod.format_index_markdown(entries)
        self.assertIn("docs/sprint/progress.md", md)

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


class ProtocolAndDocsComplianceTests(unittest.TestCase):
    """Сверка документации и протокола (docs/HANDOFF.md, PRG1.md, progress.md)."""

    def test_prg1_file_follows_protocol_format(self):
        prg1_path = ROOT / "docs" / "sprint" / "progress" / "PRG1.md"
        self.assertTrue(prg1_path.exists())
        lines = prg1_path.read_text(encoding="utf-8").splitlines()
        first_line = lines[0].strip()
        self.assertTrue(
            first_line.startswith("## PRG1 "),
            f"Заголовок должен быть уровня ## (H2): {first_line}",
        )
        self.assertRegex(
            first_line,
            r"^## PRG1 .+ — [0-9a-f]{7,40}$",
            f"Заголовок должен содержать commit sha по протоколу: {first_line}",
        )

    def test_handoff_references_progress_per_task_directory(self):
        handoff_path = ROOT / "docs" / "HANDOFF.md"
        self.assertTrue(handoff_path.exists())
        content = handoff_path.read_text(encoding="utf-8")
        self.assertIn("docs/sprint/progress/", content)

    def test_progress_md_archive_header_reflects_prg1_transition(self):
        progress_path = ROOT / "docs" / "sprint" / "progress.md"
        self.assertTrue(progress_path.exists())
        content = progress_path.read_text(encoding="utf-8")
        self.assertIn("Архив решений до перехода на PRG1 (18.10)", content)


@unittest.skipUnless(shutil.which("git"), "в образе CI (python:3.12-slim) нет git")
class GitCommitDateRealTests(unittest.TestCase):
    """Проверка извлечения даты коммита через реальный git log."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.repo = self.dir / "repo"
        self.progress_dir = self.repo / "docs" / "sprint" / "progress"
        self.progress_dir.mkdir(parents=True)
        self.env = os.environ | {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_AUTHOR_DATE": "2026-10-10T12:00:00+00:00",
            "GIT_COMMITTER_DATE": "2026-10-10T12:00:00+00:00",
        }
        subprocess.run(["git", "init", "-q"], cwd=self.repo, env=self.env, check=True)
        self.mod = load_progress_index()

    def test_get_commit_date_extracts_git_date_not_mtime(self):
        file_path = self.progress_dir / "T1.md"
        file_path.write_text("## T1\n", encoding="utf-8")
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@test", "add", "docs/sprint/progress/T1.md"],
            cwd=self.repo,
            env=self.env,
            check=True,
        )
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@test", "commit", "-q", "-m", "docs: T1"],
            cwd=self.repo,
            env=self.env,
            check=True,
        )

        # Выставляем mtime файла на намеренно другую дату (например, 2026-10-25)
        future_ts = 1792900000.0  # late October 2026
        os.utime(file_path, (future_ts, future_ts))

        date = self.mod.get_commit_date(file_path)
        self.assertEqual(date, "2026-10-10")


if __name__ == "__main__":
    unittest.main()

