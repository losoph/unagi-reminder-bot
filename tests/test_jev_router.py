"""Проверки интеграции Jev без внешней сети."""

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
# Личный файл ключей разработчика не должен менять результат тестов (route.sh читает его, если ключ не задан).
os.environ.setdefault("AGENT_ENV_FILE", os.devnull)
ROUTER = ROOT / "tools" / "agents" / "routers" / "jev_decide.py"
ROUTE = ROOT / "tools" / "agents" / "route.sh"


class JevHandler(BaseHTTPRequestHandler):
    response = {}
    statuses = []
    requests = []
    delay = 0

    def do_POST(self):
        import time

        self.requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
        if self.delay:
            time.sleep(self.delay)
        status = self.statuses.pop(0) if self.statuses else 200
        self.send_response(status)
        self.end_headers()
        if status == 200:
            try:
                self.wfile.write(json.dumps(self.response).encode())
            except BrokenPipeError:
                pass

    def log_message(self, *_args):
        return

    def handle_error(self, _request, _client_address):
        return


class JevRouterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), JevHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.thread.join()

    def setUp(self):
        JevHandler.response = {
            "model": "typesafe/jev-test",
            "answers": {
                "model": {"type": "choice", "choice": "gemini-3.8-flash-medium", "probabilities": {"gemini-3.8-flash-medium": 0.8, "gemini-3.8-flash-low": 0.2}, "confidence": 0.8},
                "effort": {"type": "choice", "choice": "medium"},
                "escalate": {"type": "noul", "noul": 0.1},
            },
            "usage": {"cost": 0.00003},
        }
        JevHandler.statuses = []
        JevHandler.requests = []
        JevHandler.delay = 0

    def invoke(self, *, key="test-secret", description="обычная задача", timeout="2"):
        task = {"agent": "agy", "task_kind": "docs", "title": "Задача", "description": description}
        env = os.environ | {"JEV_URL": self.url, "JEV_TIMEOUT": timeout}
        env.pop("OPENROUTER_API_KEY", None)
        env.pop("TYPESAFE_API_KEY", None)
        if key is not None:
            env["OPENROUTER_API_KEY"] = key
        return subprocess.run([sys.executable, str(ROUTER)], input=json.dumps(task), text=True, capture_output=True, env=env, check=False)

    def test_correct_choice_and_size_limit(self):
        proc = self.invoke(description="я" * 100_000)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("model=gemini-3.8-flash-medium effort=medium", proc.stdout)
        self.assertLessEqual(len(json.dumps(JevHandler.requests[0], ensure_ascii=False, separators=(",", ":")).encode()), 32 * 1024)

    def test_choice_outside_candidates_is_invalid(self):
        JevHandler.response["answers"]["model"]["choice"] = "not-allowed"
        proc = self.invoke()
        self.assertEqual(proc.returncode, 4)

    def test_low_confidence_and_escalation(self):
        JevHandler.response["answers"]["model"]["confidence"] = 0.2
        self.assertEqual(self.invoke().returncode, 6)
        self.setUp()
        JevHandler.response["answers"]["escalate"]["noul"] = 0.5
        self.assertEqual(self.invoke().returncode, 6)

    def test_retries_429_then_succeeds(self):
        JevHandler.statuses = [429, 200]
        proc = self.invoke()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(JevHandler.requests), 2)

    def test_timeout_is_network_failure(self):
        JevHandler.delay = 1.0
        proc = self.invoke(timeout="0.05")
        self.assertEqual(proc.returncode, 5)

    def test_timeout_covers_retries_and_backoff(self):
        JevHandler.statuses = [429, 200]
        JevHandler.delay = 0.15
        proc = self.invoke(timeout="0.3")
        self.assertEqual(proc.returncode, 5)
        self.assertLess(len(JevHandler.requests), 2)

    def test_missing_key_skips_network(self):
        proc = self.invoke(key=None)
        self.assertEqual(proc.returncode, 3)
        self.assertEqual(JevHandler.requests, [])

    def test_empty_key_is_missing(self):
        proc = self.invoke(key="")
        self.assertEqual(proc.returncode, 3)

    def test_route_cleans_temp_files_after_jev(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "routes.ndjson"
            env = os.environ | {
                "OPENROUTER_API_KEY": "test-secret",
                "JEV_URL": self.url,
                "AGENT_ROUTE_LOG": str(log),
                "PYTHON": sys.executable,
                "TMPDIR": tmp,
            }
            proc = subprocess.run([str(ROUTE), "agy", "docs", "--dry-run=1"], text=True, capture_output=True, env=env, check=False)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(list(Path(tmp).glob("jev-route-*")), [])

    def test_secret_never_reaches_output_or_route_log(self):
        secret = "fictional-secret-do-not-print"
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ | {"OPENROUTER_API_KEY": secret, "JEV_URL": self.url, "AGENT_ROUTE_LOG": str(Path(tmp) / "routes.ndjson"), "PYTHON": sys.executable}
            proc = subprocess.run([str(ROUTE), "agy", "docs", "Документация"], text=True, capture_output=True, env=env, check=False)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            logged = (Path(tmp) / "routes.ndjson").read_text(encoding="utf-8")
            self.assertNotIn(secret, proc.stdout + proc.stderr + logged)


if __name__ == "__main__":
    unittest.main()
