"""Проверки decide.sh (простые решения через Jev) и загрузчика ключей без внешней сети."""

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DECIDE = ROOT / "tools" / "agents" / "decide.sh"
ROUTE = ROOT / "tools" / "agents" / "route.sh"
OPTIONS = ["merge=ревью чистое", "fix=есть дефекты", "hold=не хватает данных"]


class AskHandler(BaseHTTPRequestHandler):
    response = {}
    requests = []

    def do_POST(self):
        self.requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(json.dumps(self.response).encode())

    def log_message(self, *_args):
        return


def answer(choice="merge", confidence=0.9, escalate=0.1):
    probabilities = {"merge": 0.1, "fix": 0.1, "hold": 0.1}
    probabilities[choice] = confidence
    return {
        "model": "typesafe/jev-test",
        "answers": {
            "decision": {"type": "choice", "choice": choice, "probabilities": probabilities, "confidence": confidence},
            "escalate": {"type": "noul", "noul": escalate},
        },
        "usage": {"cost": 0.00002},
    }


class DecideTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), AskHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.thread.join()

    def setUp(self):
        AskHandler.response = answer()
        AskHandler.requests = []
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log = Path(self.tmp.name) / "decisions.ndjson"

    def run_decide(self, *args, key="test-secret", extra_env=None):
        env = os.environ | {
            "JEV_URL": self.url,
            "JEV_TIMEOUT": "2",
            "AGENT_DECISION_LOG": str(self.log),
            "AGENT_ENV_FILE": os.devnull,
            "PYTHON": sys.executable,
        }
        env.pop("OPENROUTER_API_KEY", None)
        env.pop("TYPESAFE_API_KEY", None)
        if key is not None:
            env["OPENROUTER_API_KEY"] = key
        env.update(extra_env or {})
        return subprocess.run([str(DECIDE), "Влить ветку?", *OPTIONS, *args], text=True, capture_output=True, env=env, check=False)

    def test_confident_choice_is_returned_and_logged(self):
        proc = self.run_decide("--title", "T24c")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("choice=merge source=jev confidence=0.9", proc.stdout)
        row = json.loads(self.log.read_text(encoding="utf-8"))
        self.assertEqual((row["choice"], row["source"], row["title"]), ("merge", "jev", "T24c"))
        self.assertEqual(row["meta"]["cost"], 0.00002)
        request = AskHandler.requests[0]
        self.assertEqual(set(request["questions"]["decision"]["criteria"]), {"merge", "fix", "hold"})

    def test_low_confidence_uses_default_or_asks_human(self):
        AskHandler.response = answer("fix", 0.5)
        proc = self.run_decide("--default", "hold")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("choice=hold source=default", proc.stdout)
        proc = self.run_decide()
        self.assertEqual(proc.returncode, 10)
        self.assertIn("choice=ASK_HUMAN source=human", proc.stdout)

    def test_escalation_goes_to_human_even_with_default(self):
        AskHandler.response = answer("merge", 0.95, escalate=0.6)
        proc = self.run_decide("--default", "hold")
        self.assertEqual(proc.returncode, 10)
        self.assertIn("choice=ASK_HUMAN", proc.stdout)

    def test_missing_key_never_touches_network(self):
        proc = self.run_decide(key=None)
        self.assertEqual(proc.returncode, 10)
        self.assertIn("нет ключа OpenRouter", proc.stdout)
        self.assertEqual(AskHandler.requests, [])

    def test_choice_outside_options_is_rejected(self):
        AskHandler.response = answer()
        AskHandler.response["answers"]["decision"]["choice"] = "ship-it"
        proc = self.run_decide("--default", "hold")
        self.assertIn("choice=hold source=default", proc.stdout)

    def test_dry_run_writes_no_log_and_bad_usage_exits_2(self):
        proc = self.run_decide("--dry-run")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(self.log.exists())
        proc = self.run_decide("--default", "nope")
        self.assertEqual(proc.returncode, 2)
        env = os.environ | {"AGENT_ENV_FILE": os.devnull}
        proc = subprocess.run([str(DECIDE), "вопрос", "один=вариант"], text=True, capture_output=True, env=env, check=False)
        self.assertEqual(proc.returncode, 2)

    def test_secrets_in_question_context_and_options_are_redacted(self):
        secret = "sk-abcdef1234567890"
        context = Path(self.tmp.name) / "ctx.txt"
        context.write_text(f"лог: OPENROUTER_API_KEY={secret} и Bearer abcdefghijkl12345", encoding="utf-8")
        proc = self.run_decide("--context-file", str(context))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        sent = json.dumps(AskHandler.requests[0], ensure_ascii=False)
        self.assertNotIn(secret, sent)
        self.assertNotIn("abcdefghijkl12345", sent)
        self.assertIn("[REDACTED]", sent)

    def test_huge_context_is_trimmed_below_body_limit(self):
        context = Path(self.tmp.name) / "big.txt"
        context.write_text("я" * 200_000, encoding="utf-8")
        proc = self.run_decide("--context-file", str(context))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        body = json.dumps(AskHandler.requests[0], ensure_ascii=False, separators=(",", ":")).encode()
        self.assertLessEqual(len(body), 32 * 1024)

    def test_key_file_fills_only_unset_variables(self):
        env_file = Path(self.tmp.name) / "env"
        env_file.write_text("# комментарий\nexport OPENROUTER_API_KEY='from-file'\nTYPESAFE_API_KEY=other\n$(touch pwned)\n", encoding="utf-8")
        script = f'. "{ROOT}/tools/agents/agent_env.sh"; agent_env_load OPENROUTER_API_KEY TYPESAFE_API_KEY; printf "%s|%s" "${{OPENROUTER_API_KEY-unset}}" "${{TYPESAFE_API_KEY-unset}}"'
        base = {k: v for k, v in os.environ.items() if k not in {"OPENROUTER_API_KEY", "TYPESAFE_API_KEY"}}
        proc = subprocess.run(["bash", "-c", script], text=True, capture_output=True, env=base | {"AGENT_ENV_FILE": str(env_file)}, cwd=self.tmp.name, check=False)
        self.assertEqual(proc.stdout, "from-file|other", proc.stderr)
        self.assertFalse((Path(self.tmp.name) / "pwned").exists())
        proc = subprocess.run(["bash", "-c", script], text=True, capture_output=True, env=base | {"AGENT_ENV_FILE": str(env_file), "OPENROUTER_API_KEY": ""}, cwd=self.tmp.name, check=False)
        self.assertEqual(proc.stdout, "|other", proc.stderr)

    def test_route_picks_key_from_file_when_unset(self):
        env_file = Path(self.tmp.name) / "env"
        env_file.write_text("OPENROUTER_API_KEY=from-file\n", encoding="utf-8")
        AskHandler.response = {
            "model": "typesafe/jev-test",
            "answers": {
                "model": {"type": "choice", "choice": "gemini-3.8-flash-medium", "probabilities": {"gemini-3.8-flash-medium": 0.9}, "confidence": 0.9},
                "effort": {"type": "choice", "choice": "low"},
                "escalate": {"type": "noul", "noul": 0.0},
            },
        }
        env = {k: v for k, v in os.environ.items() if k not in {"OPENROUTER_API_KEY", "TYPESAFE_API_KEY", "AGENT_ROUTER"}}
        env |= {"AGENT_ENV_FILE": str(env_file), "JEV_URL": self.url, "PYTHON": sys.executable}
        proc = subprocess.run([str(ROUTE), "agy", "docs", "--dry-run"], text=True, capture_output=True, env=env, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("model=gemini-3.8-flash-medium effort=low", proc.stdout)
        self.assertEqual(AskHandler.requests[0]["state"]["agent"], "agy")

    def test_usage_reports_decision_count_and_cost(self):
        rows = [
            {"choice": "merge", "meta": {"cost": 0.25}},
            {"choice": "ASK_HUMAN", "meta": {}},
        ]
        self.log.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        env = os.environ | {"AGENT_DECISION_LOG": str(self.log), "AGY_LOG_DIR": self.tmp.name, "AGENT_ROUTE_LOG": os.devnull, "OC_LOG": ""}
        proc = subprocess.run([str(ROOT / "tools" / "agents" / "usage.sh")], text=True, capture_output=True, env=env, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("decide count=2 human=1 cost=0.25000000", proc.stdout)


if __name__ == "__main__":
    unittest.main()
