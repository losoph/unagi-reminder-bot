"""Проверки бюджета токенов opencode: предохранитель прогона, журнал спринта, отказ при пустом остатке."""

import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools" / "agents"
BUDGET = TOOLS / "oc_budget.py"
OC_RUN = TOOLS / "oc_run.sh"


def finish(tokens_in, tokens_out=0):
    return json.dumps({"type": "step_finish", "part": {"tokens": {"input": tokens_in, "output": tokens_out, "reasoning": 0}}}) + "\n"


class OcBudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger = Path(self.tmp.name) / "ledger.ndjson"
        self.env = os.environ | {
            "OC_LEDGER": str(self.ledger),
            "OC_SPRINT": "test-sprint",
            "OC_SPRINT_TOKEN_LIMIT": "100000",
            "OC_RUN_TOKEN_LIMIT": "50000",
        }

    def run_budget(self, *args, input_text=None, env=None):
        return subprocess.run([sys.executable, str(BUDGET), *args], input=input_text, text=True, capture_output=True, env=env or self.env, check=False)

    def rows(self):
        return [json.loads(line) for line in self.ledger.read_text(encoding="utf-8").splitlines()] if self.ledger.exists() else []

    def test_guard_passes_lines_through_and_records_total(self):
        stream = "не json\n" + finish(10000, 500) + finish(12000, 300) + '{"type":"text"}\n'
        proc = self.run_budget("guard", "--title", "T1", input_text=stream)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, stream)
        self.assertEqual([(r["title"], r["tokens"], r["counted"]) for r in self.rows()], [("T1", 22800, True)])

    def test_guard_trips_on_run_limit_and_emits_event(self):
        stream = finish(30000) + finish(30000) + finish(30000)
        proc = self.run_budget("guard", "--title", "big", input_text=stream)
        self.assertEqual(proc.returncode, 3)
        lines = proc.stdout.splitlines()
        self.assertEqual(json.loads(lines[-1])["type"], "budget_exceeded")
        self.assertEqual(len(lines), 3)  # третий шаг не прочитан
        self.assertEqual(self.rows()[0]["tokens"], 60000)

    def test_guard_trips_on_sprint_limit_across_runs(self):
        self.run_budget("add", "70000", "earlier")
        proc = self.run_budget("guard", "--title", "next", input_text=finish(20000) + finish(20000))
        self.assertEqual(proc.returncode, 3)
        self.assertIn("лимит спринта", proc.stdout)

    def test_reference_entries_are_recorded_but_not_counted(self):
        self.run_budget("add", "5000000", "before economy", "--reference", "--note", "справочно")
        self.assertIn("used=0", self.run_budget("status").stdout)
        self.assertFalse(self.rows()[0]["counted"])

    def test_check_refuses_when_sprint_budget_is_gone(self):
        self.assertEqual(self.run_budget("check").returncode, 0)
        self.run_budget("add", "95000", "spent")
        proc = self.run_budget("check")
        self.assertEqual(proc.returncode, 3)
        self.assertIn("исчерпан", proc.stderr)

    def test_other_sprints_and_uncounted_rows_do_not_count(self):
        self.ledger.write_text(
            json.dumps({"sprint": "old", "tokens": 9_000_000, "counted": True}) + "\n"
            + json.dumps({"sprint": "test-sprint", "tokens": 9_000_000, "counted": False}) + "\nбитая строка\n",
            encoding="utf-8",
        )
        proc = self.run_budget("status")
        self.assertIn("used=0 limit=100000 remaining=100000", proc.stdout)

    def test_oc_run_stops_a_runaway_agent_that_ignores_sigpipe(self):
        bin_dir = Path(self.tmp.name) / "bin"
        bin_dir.mkdir()
        fake = bin_dir / "opencode"
        fake.write_text(
            "#!/bin/sh\ntrap '' PIPE\n"
            f"for i in 1 2 3 4 5 6; do printf '%s\\n' '{finish(30000).strip()}'; sleep 0.2; done\nwhile :; do sleep 1; done\n",
            encoding="utf-8",
        )
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        worktree = Path(self.tmp.name) / "wt"
        worktree.mkdir()
        brief = Path(self.tmp.name) / "brief.md"
        brief.write_text("x", encoding="utf-8")
        env = self.env | {"PATH": f"{bin_dir}:{os.environ['PATH']}"}
        started = time.monotonic()
        proc = subprocess.run(
            [str(OC_RUN), str(worktree), "Tokenn/test", str(brief), str(Path(self.tmp.name) / "run.ndjson"), "runaway"],
            text=True, capture_output=True, env=env, check=False, timeout=30,
        )
        self.assertEqual(proc.returncode, 3, proc.stderr)
        self.assertLess(time.monotonic() - started, 20)
        log = (Path(self.tmp.name) / "run.ndjson").read_text(encoding="utf-8")
        self.assertIn("budget_exceeded", log)
        self.assertIn('"code": 3', log)
        self.assertEqual(subprocess.run(["pgrep", "-f", str(fake)], capture_output=True).returncode, 1)

    def test_oc_run_refuses_to_start_without_budget(self):
        self.run_budget("add", "99000", "spent")
        worktree = Path(self.tmp.name) / "wt"
        worktree.mkdir()
        brief = Path(self.tmp.name) / "brief.md"
        brief.write_text("x", encoding="utf-8")
        proc = subprocess.run([str(OC_RUN), str(worktree), "m", str(brief), str(Path(self.tmp.name) / "r.ndjson"), "t"], text=True, capture_output=True, env=self.env, check=False)
        self.assertEqual(proc.returncode, 3)
        self.assertIn("исчерпан", proc.stderr)


if __name__ == "__main__":
    unittest.main()
