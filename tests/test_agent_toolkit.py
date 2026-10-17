"""Регрессионные проверки набора инструментов мониторинга агентов."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Личный файл ключей разработчика не должен менять результат тестов (route.sh читает его, если ключ не задан).
os.environ.setdefault("AGENT_ENV_FILE", os.devnull)
TOOLS = ROOT / "tools" / "agents"


class AgentToolkitTests(unittest.TestCase):
    def run_cmd(self, args, *, env=None, cwd=ROOT, input_text=None):
        return subprocess.run(args, cwd=cwd, env=env, input=input_text, text=True, capture_output=True, check=False)

    def test_oc_pretty_renders_recorded_events_and_ignores_invalid(self):
        data = "\n".join([
            json.dumps({"type": "reasoning", "part": {"text": "Проверяю решение"}}),
            json.dumps({"type": "tool_use", "part": {"tool": "read", "state": {"input": {"path": "src/file.py"}}}}),
            json.dumps({"type": "error", "error": {"message": "ошибка"}}),
            "not-json",
        ]) + "\n"
        proc = self.run_cmd([sys.executable, str(TOOLS / "oc_pretty.py")], input_text=data)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Проверяю решение", proc.stdout)
        self.assertIn("read src/file.py", proc.stdout)
        self.assertIn("ошибка", proc.stdout)
        self.assertIn("not-json", proc.stdout)

    def test_oc_digest_alerts_and_prints_run_end(self):
        events = [
            {"type": "tool_use", "part": {"tool": "bash", "state": {"input": {"command": "git commit -m test"}, "output": "ok"}}},
            {"type": "tool_use", "part": {"tool": "edit", "state": {"input": {"filePath": "src/a.py"}}}},
            {"type": "error", "error": {"message": "failed"}},
            {"type": "run_end", "title": "run-a", "code": 7},
        ]
        env = os.environ | {"PERIOD": "3600"}
        proc = self.run_cmd([sys.executable, str(TOOLS / "oc_digest.py")], env=env, input_text="\n".join(map(json.dumps, events)) + "\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("OC COMMIT", proc.stdout)
        self.assertIn("OC ERROR", proc.stdout)
        self.assertIn("OC RUN END code=7 title=run-a", proc.stdout)
        self.assertIn("a.py", proc.stdout)

    def test_route_dry_run_forms_and_rejects_unknown_options(self):
        for option in ("--dry-run", "--dry-run=true", "--dry-run=1"):
            with self.subTest(option=option), tempfile.TemporaryDirectory() as tmp:
                log = Path(tmp) / "routes.ndjson"
                env = os.environ | {"AGENT_ROUTE_LOG": str(log), "AGENT_ROUTER": "static"}
                proc = self.run_cmd([str(TOOLS / "route.sh"), "agy", "docs", option], env=env)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertFalse(log.exists())

        proc = self.run_cmd([str(TOOLS / "route.sh"), "agy", "docs", "--unknown"])
        self.assertEqual(proc.returncode, 2)

    def test_route_missing_key_is_logged_as_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "routes.ndjson"
            env = os.environ | {
                "AGENT_ROUTE_LOG": str(log),
                "AGENT_ROUTER": "jev",
                "OPENROUTER_API_KEY": "",
                "TYPESAFE_API_KEY": "",
            }
            proc = self.run_cmd([str(TOOLS / "route.sh"), "agy", "docs"], env=env)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            row = json.loads(log.read_text(encoding="utf-8"))
            self.assertEqual(row["source"], "fallback")
            self.assertEqual(row["fallback_reason"], "нет ключа OpenRouter")

    def test_route_uses_policy_and_appends_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "routes.ndjson"
            env = os.environ | {"AGENT_ROUTE_LOG": str(log), "AGENT_ROUTER": "static"}
            proc = self.run_cmd([str(TOOLS / "route.sh"), "agy", "docs", "Write docs"], env=env)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            # Ожидание берётся из самой политики: тест проверяет механизм, а не конкретную модель.
            expected = json.loads((TOOLS / "routing.json").read_text(encoding="utf-8"))["tasks"]["docs"]["agy"]
            self.assertIn(f"model={expected['model']} effort={expected['effort']}", proc.stdout)
            row = json.loads(log.read_text(encoding="utf-8"))
            self.assertEqual(row["task_kind"], "docs")
            self.assertEqual(row["model"], expected["model"])
            self.assertEqual(row["source"], "fallback")

    def test_route_rejects_external_model_and_falls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "routes.ndjson"
            env = os.environ | {
                "AGENT_ROUTE_LOG": str(log),
                "AGENT_ROUTER_CMD": "python3 -c 'import json; print(json.dumps({\"model\":\"not-allowed\",\"effort\":\"high\",\"reason\":\"external\"}))'",
            }
            proc = self.run_cmd([str(TOOLS / "route.sh"), "opencode", "review"], env=env)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("model=Tokenn/gpt-6.1-sol effort=high", proc.stdout)
            row = json.loads(log.read_text(encoding="utf-8"))
            self.assertEqual(row["source"], "fallback")

    def test_route_external_router_receives_task_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "routes.ndjson"
            router = Path(tmp) / "router.py"
            router.write_text(
                "import json, sys\n"
                "task = json.load(sys.stdin)\n"
                "print(json.dumps({'model': 'Tokenn/gpt-5.6-sol', 'effort': 'low', 'reason': task['task_kind']}))\n",
                encoding="utf-8",
            )
            env = os.environ | {
                "AGENT_ROUTE_LOG": str(log),
                "AGENT_ROUTER_CMD": f"{sys.executable} {router}",
            }
            proc = self.run_cmd([str(TOOLS / "route.sh"), "opencode", "docs", "brief"], env=env)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            row = json.loads(log.read_text(encoding="utf-8"))
            self.assertEqual(row["source"], "external")
            self.assertEqual(row["reason"], "docs")

    def test_usage_reads_agy_opencode_and_route_fixtures(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            agy = d / "agy.ndjson"
            agy.write_text(json.dumps({"event": "result", "result": {"title": "task-x", "model": "gemini-x", "usage": {"input_tokens": 10, "output_tokens": 4, "total_tokens": 14}}}) + "\n", encoding="utf-8")
            oc = d / "oc.ndjson"
            oc.write_text(json.dumps({"type": "step_finish", "title": "task-x", "model": "oc-x", "part": {"tokens": {"input": 20, "output": 5, "total": 25}, "cost": 0.1}}) + "\n", encoding="utf-8")
            routes = d / "routes.ndjson"
            routes.write_text(json.dumps({"title": "task-x", "model": "chosen-model"}) + "\n", encoding="utf-8")
            env = os.environ | {"AGY_LOG_DIR": str(d), "OC_LOG": str(oc), "AGENT_ROUTE_LOG": str(routes)}
            shutil.copyfile(agy, d / "session.ndjson")
            proc = self.run_cmd([str(TOOLS / "usage.sh")], env=env)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("agent=agy title=task-x selected_model=chosen-model", proc.stdout)
            self.assertIn("total=14", proc.stdout)
            self.assertIn("agent=opencode title=task-x selected_model=chosen-model", proc.stdout)
            self.assertIn("cost=0.1", proc.stdout)

    def test_compatibility_wrappers_delegate_to_new_scripts(self):
        for name in ("agy_watch.sh", "agy_supervise.sh"):
            wrapper = ROOT / "scripts" / name
            target = TOOLS / name
            proc = self.run_cmd([str(wrapper), "invalid"])
            self.assertNotEqual(proc.returncode, 0)
            self.assertTrue(target.is_file())
            self.assertIn(f'../tools/agents/{name}', wrapper.read_text(encoding="utf-8"))

    def test_shell_syntax(self):
        files = [*TOOLS.glob("*.sh"), *(ROOT / "scripts").glob("agy_*.sh")]
        for file in files:
            proc = self.run_cmd(["bash", "-n", str(file)])
            self.assertEqual(proc.returncode, 0, f"{file}: {proc.stderr}")


class AgyProfileTests(unittest.TestCase):
    """AGY_HOME: отдельный профиль второго аккаунта agy2 (вход и разговоры не смешиваются с основным)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        (self.dir / "bin").mkdir()
        self.args_file = self.dir / "zsh-args.txt"
        fake = self.dir / "bin" / "zsh"
        fake.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$FAKE_ZSH_ARGS"\n', encoding="utf-8")
        fake.chmod(0o755)
        (self.dir / "brief.md").write_text("задача", encoding="utf-8")
        self.env = {
            **os.environ,
            "PATH": f"{self.dir / 'bin'}:{os.environ['PATH']}",
            "FAKE_ZSH_ARGS": str(self.args_file),
            "AGY_LOG_DIR": str(self.dir / "log"),
            "AGY_MODEL": "model-x",
        }
        self.env.pop("AGY_HOME", None)

    def run_watch(self, **extra):
        env = {**self.env, **extra}
        return subprocess.run([str(TOOLS / "agy_watch.sh"), "run", str(self.dir / "brief.md")], env=env, text=True, capture_output=True, check=False)

    def test_without_profile_home_is_not_overridden(self):
        self.run_watch()
        self.assertTrue(self.args_file.exists())
        self.assertNotIn("HOME=", self.args_file.read_text(encoding="utf-8"))

    def test_profile_sets_home_for_agy_only(self):
        home = self.dir / "agy2"
        home.mkdir()
        self.run_watch(AGY_HOME=str(home))
        command = self.args_file.read_text(encoding="utf-8")
        self.assertIn('HOME="$AGY_HOME" agy', command)
        self.assertIn("--model", command)

    def test_missing_profile_dir_stops_before_starting_agy(self):
        proc = self.run_watch(AGY_HOME=str(self.dir / "absent"))
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("профиль", proc.stdout)
        self.assertFalse(self.args_file.exists())


if __name__ == "__main__":
    unittest.main()
