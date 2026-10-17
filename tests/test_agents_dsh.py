"""Набор агентов: dsh_run.sh, лестница исполнителей, agents.sh, мониторы dsh и отчёт Jev — без сети и без настоящего dsh."""
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools" / "agents"


def load(name):
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(TOOLS))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(TOOLS))
    return module


def step_end(fresh, out, cached=0):
    return json.dumps({"type": "status", "phase": "step_end", "turn": 1, "step": 1,
                       "usage": {"inputTokens": fresh, "outputTokens": out, "cacheReadTokens": cached, "cacheWriteTokens": 0}}) + "\n"


class DshBase(unittest.TestCase):
    FAKE = """#!/bin/sh
printf '%s\\n' "$@" > "$DSH_FAKE_ARGS"
while [ $# -gt 0 ]; do if [ "$1" = --patch ]; then cp "$2" "$DSH_FAKE_OVERLAY"; fi; shift; done
cat > "$DSH_FAKE_TASK"
@@BODY@@
"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        (self.dir / "bin").mkdir()
        self.fake = self.dir / "bin" / "dsh"  # в пути есть «bin/dsh» — по нему предохранитель находит процесс
        self.set_fake("""printf '%s\\n' '{"type":"session","sessionId":"session-test","cwd":"x"}' '""" + step_end(1000, 50, 4000).strip()
                      + """' '{"type":"final","text":"ok"}'""")
        self.worktree = self.dir / "wt"
        self.worktree.mkdir()
        self.brief = self.dir / "brief.md"
        self.brief.write_text("сделай дело", encoding="utf-8")
        self.ledger = self.dir / "ledger.ndjson"
        self.env = os.environ | {
            "DSH_BIN": str(self.fake), "OC_LEDGER": str(self.ledger), "OC_SPRINT": "test-sprint",
            "DSH_SPRINT_TOKEN_LIMIT": "1000000", "DSH_RUN_TOKEN_LIMIT": "200000",
            "DSH_FAKE_ARGS": str(self.dir / "args.txt"), "DSH_FAKE_OVERLAY": str(self.dir / "overlay.yml"), "DSH_FAKE_TASK": str(self.dir / "task.txt"),
            "AGENT_ENV_FILE": os.devnull, "OPENROUTER_API_KEY": "", "TYPESAFE_API_KEY": "", "AGENT_ROUTER": "static",
            "AGENT_ROUTE_LOG": str(self.dir / "routes.ndjson"), "PYTHON": sys.executable, "AGENT_DECISION_LOG": str(self.dir / "decisions.ndjson"),
        }

    def set_fake(self, body):
        self.fake.write_text(self.FAKE.replace("@@BODY@@", body), encoding="utf-8")
        self.fake.chmod(self.fake.stat().st_mode | stat.S_IEXEC)

    def run_script(self, script, *args, env=None, timeout=60):
        return subprocess.run([str(TOOLS / script), *map(str, args)], text=True, capture_output=True, env=env or self.env, check=False, timeout=timeout)

    def ledger_rows(self):
        return [json.loads(line) for line in self.ledger.read_text(encoding="utf-8").splitlines()] if self.ledger.exists() else []


class DshRunTests(DshBase):
    def test_run_passes_overlay_before_json_logs_run_end_and_counts_tokens(self):
        log = self.dir / "run.ndjson"
        proc = self.run_script("dsh_run.sh", self.worktree, self.brief, log, "T1", "--model", "deepseek-v4-pro", "--effort", "low")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        args = (self.dir / "args.txt").read_text(encoding="utf-8").split("\n")
        self.assertLess(args.index("--patch"), args.index("--json"))  # --patch — флаг лаунчера: после --json dsh его не знает
        overlay = (self.dir / "overlay.yml").read_text(encoding="utf-8")
        self.assertIn("model: deepseek-v4-pro", overlay)
        self.assertIn('reasoningEffort: "low"', overlay)
        self.assertEqual((self.dir / "task.txt").read_text(encoding="utf-8"), "сделай дело")
        last = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual((last["type"], last["code"], last["model"], last["effort"]), ("run_end", 0, "deepseek-v4-pro", "low"))
        row = self.ledger_rows()[0]
        self.assertEqual((row["tool"], row["title"], row["tokens"], row["cache_read"]), ("dsh", "T1", 1050, 4000))
        self.assertIn("шагов 1, новых входных 1000, выходных 50, из кеша 4000", proc.stdout)

    def test_without_model_and_effort_profile_defaults_are_used_and_review_prefix_added(self):
        proc = self.run_script("dsh_run.sh", self.worktree, self.brief, self.dir / "r.ndjson", "T2", "--review")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("--patch", (self.dir / "args.txt").read_text(encoding="utf-8"))
        self.assertTrue((self.dir / "task.txt").read_text(encoding="utf-8").startswith("ТОЛЬКО ЧТЕНИЕ"))

    def test_bad_arguments_missing_dsh_and_exhausted_budget(self):
        self.assertEqual(self.run_script("dsh_run.sh", self.worktree, self.brief, self.dir / "r.ndjson", "T", "--model", "gpt").returncode, 2)
        self.assertEqual(self.run_script("dsh_run.sh", self.worktree, self.brief, self.dir / "r.ndjson", "T", "--effort", "turbo").returncode, 2)
        self.assertEqual(self.run_script("dsh_run.sh", self.dir / "nope", self.brief, self.dir / "r.ndjson", "T").returncode, 2)
        missing = self.env | {"DSH_BIN": str(self.dir / "no-such-dsh")}
        self.assertEqual(self.run_script("dsh_run.sh", self.worktree, self.brief, self.dir / "r.ndjson", "T", env=missing).returncode, 4)
        spent = self.env | {"DSH_SPRINT_TOKEN_LIMIT": "1000"}
        proc = self.run_script("dsh_run.sh", self.worktree, self.brief, self.dir / "r.ndjson", "T", env=spent)
        self.assertEqual(proc.returncode, 3)
        self.assertIn("исчерпан", proc.stderr)
        self.assertFalse((self.dir / "args.txt").exists())  # dsh не запускался

    def test_runaway_dsh_is_stopped_by_the_run_token_limit(self):
        events = " ".join(f"'{step_end(150000, 100).strip()}'" for _ in range(4))
        self.set_fake(f"echo $$ > '{self.dir}/agent.pid'\ntrap '' PIPE\nfor e in {events}; do printf '%s\\n' \"$e\"; sleep 0.2; done\nwhile :; do sleep 1; done")
        started = time.monotonic()
        proc = self.run_script("dsh_run.sh", self.worktree, self.brief, self.dir / "run.ndjson", "runaway", timeout=40)
        self.assertEqual(proc.returncode, 3, proc.stderr)
        self.assertLess(time.monotonic() - started, 25)
        log = (self.dir / "run.ndjson").read_text(encoding="utf-8")
        self.assertIn("budget_exceeded", log)
        with self.assertRaises(ProcessLookupError):
            os.kill(int((self.dir / "agent.pid").read_text(encoding="utf-8")), 0)


class AgentsShTests(DshBase):
    def test_run_dsh_takes_model_and_effort_from_route_output(self):
        proc = self.run_script("agents.sh", "run", "dsh", "docs", self.worktree, self.brief, self.dir / "r.ndjson", "Doc")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        overlay = (self.dir / "overlay.yml").read_text(encoding="utf-8")
        self.assertIn("model: deepseek-flash", overlay)  # статическая политика docs/dsh: effort off
        self.assertIn('reasoningEffort: "off"', overlay)
        proc = self.run_script("agents.sh", "run", "dsh", "review", self.worktree, self.brief, self.dir / "r.ndjson", "Rev")
        self.assertIn('reasoningEffort: "high"', (self.dir / "overlay.yml").read_text(encoding="utf-8"))

    def test_opencode_is_refused_without_a_jev_decision_and_never_started(self):
        fake_oc = self.dir / "bin" / "opencode"
        fake_oc.write_text(f"#!/bin/sh\ntouch '{self.dir}/opencode-started'\n", encoding="utf-8")
        fake_oc.chmod(fake_oc.stat().st_mode | stat.S_IEXEC)
        env = self.env | {"PATH": f"{self.dir / 'bin'}:{os.environ['PATH']}"}
        proc = self.run_script("agents.sh", "run", "opencode", "review", self.worktree, self.brief, self.dir / "o.ndjson", "OC", env=env)
        self.assertEqual(proc.returncode, 4)
        self.assertIn("только с решением Jev", proc.stderr)
        self.assertFalse((self.dir / "opencode-started").exists())

    def serve_jev(self, model_choice, effort_choice):
        """Фейковый Jev: возвращает заданные модель и эффорт и запоминает запрос."""
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                answer = {"model": "typesafe/jev-test", "answers": {
                    "model": {"type": "choice", "choice": model_choice, "probabilities": {model_choice: 0.9}, "confidence": 0.9},
                    "effort": {"type": "choice", "choice": effort_choice}, "escalate": {"type": "noul", "noul": 0.0}}}
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps(answer).encode())

            def log_message(self, *_args):
                return

        server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        env = self.env | {"OPENROUTER_API_KEY": "test-key", "JEV_URL": f"http://127.0.0.1:{server.server_port}"}
        env.pop("AGENT_ROUTER")
        return env, requests

    def test_jev_chooses_dsh_model_and_all_four_effort_levels(self):
        for model, effort in (("deepseek-v4-pro", "max"), ("deepseek-flash", "off"), ("deepseek-flash", "low"), ("deepseek-v4-pro", "high")):
            with self.subTest(model=model, effort=effort):
                env, requests = self.serve_jev(model, effort)
                proc = self.run_script("agents.sh", "run", "dsh", "review", self.worktree, self.brief, self.dir / "j.ndjson", "J", env=env)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                overlay = (self.dir / "overlay.yml").read_text(encoding="utf-8")
                self.assertIn(f"model: {model}", overlay)
                self.assertIn(f'reasoningEffort: "{effort}"', overlay)
                question = requests[0]["questions"]
                self.assertEqual(list(question["effort"]["criteria"]), ["off", "low", "high", "max"])
                self.assertEqual(list(question["model"]["criteria"]), ["deepseek-flash", "deepseek-v4-pro"])
                self.assertIn("V4.1 Flash", json.dumps(question["model"]["criteria"], ensure_ascii=False))

    def test_effort_level_foreign_to_the_agent_is_rejected_and_policy_fallback_used(self):
        env, _ = self.serve_jev("deepseek-flash", "medium")  # у dsh нет уровня medium
        proc = self.run_script("agents.sh", "run", "dsh", "docs", self.worktree, self.brief, self.dir / "j.ndjson", "J", env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('reasoningEffort: "off"', (self.dir / "overlay.yml").read_text(encoding="utf-8"))  # статика docs/dsh
        self.assertIn("fallback=", proc.stderr)

    def test_opencode_runs_with_the_model_jev_chose(self):
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                answer = {"model": "typesafe/jev-test", "answers": {
                    "model": {"type": "choice", "choice": "Tokenn/codex-5.3", "probabilities": {"Tokenn/codex-5.3": 0.9}, "confidence": 0.9},
                    "effort": {"type": "choice", "choice": "low"}, "escalate": {"type": "noul", "noul": 0.0}}}
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps(answer).encode())

            def log_message(self, *_args):
                return

        server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        fake_oc = self.dir / "bin" / "opencode"
        fake_oc.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$@\" > '{self.dir}/oc-args.txt'\n", encoding="utf-8")
        fake_oc.chmod(fake_oc.stat().st_mode | stat.S_IEXEC)
        config = self.dir / "opencode.json"
        config.write_text(json.dumps({"provider": {"Tokenn": {"models": {"codex-5.3": {"variants": {"low": {"reasoningEffort": "low"}}}}}}}), encoding="utf-8")
        env = self.env | {"PATH": f"{self.dir / 'bin'}:{os.environ['PATH']}", "OPENROUTER_API_KEY": "test-key", "JEV_URL": f"http://127.0.0.1:{server.server_port}",
                          "OC_STATS_CMD": "exit 1", "OC_SPRINT_TOKEN_LIMIT": "100000", "OC_CONFIG_PATH": str(config)}
        env.pop("AGENT_ROUTER")
        proc = self.run_script("agents.sh", "run", "opencode", "short-check", self.worktree, self.brief, self.dir / "o.ndjson", "OC", env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        args = (self.dir / "oc-args.txt").read_text(encoding="utf-8").split("\n")
        self.assertEqual(args[args.index("-m") + 1], "Tokenn/codex-5.3")  # уровень мышления задан панелью Tokenn (Min): эффорт не передаётся

    def test_opencode_refuses_effort_without_a_defined_variant(self):
        fake_oc = self.dir / "bin" / "opencode"
        fake_oc.write_text(f"#!/bin/sh\ntouch '{self.dir}/opencode-started'\n", encoding="utf-8")
        fake_oc.chmod(fake_oc.stat().st_mode | stat.S_IEXEC)
        config = self.dir / "opencode.json"
        config.write_text(json.dumps({"provider": {"Tokenn": {"models": {"codex-5.3": {}}}}}), encoding="utf-8")
        env = self.env | {"PATH": f"{self.dir / 'bin'}:{os.environ['PATH']}", "OC_CONFIG_PATH": str(config), "OC_SPRINT_TOKEN_LIMIT": "100000"}
        proc = self.run_script("oc_run.sh", self.worktree, "Tokenn/codex-5.3", self.brief, self.dir / "o.ndjson", "OC", "--effort", "high", env=env)
        self.assertEqual(proc.returncode, 5)
        self.assertIn("oc_variants.py apply", proc.stderr)
        self.assertFalse((self.dir / "opencode-started").exists())

    def _oc_args_after_run(self, *flags):
        fake_oc = self.dir / "bin" / "opencode"
        fake_oc.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$@\" > '{self.dir}/oc-args.txt'\n", encoding="utf-8")
        fake_oc.chmod(fake_oc.stat().st_mode | stat.S_IEXEC)
        env = self.env | {"PATH": f"{self.dir / 'bin'}:{os.environ['PATH']}", "OC_BUDGET": "off"}
        proc = self.run_script("oc_run.sh", self.worktree, "Tokenn/gpt-6.1-sol", self.brief, self.dir / "o.ndjson", "OC", *flags, env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return (self.dir / "oc-args.txt").read_text(encoding="utf-8")

    def test_oc_run_attaches_the_brief_by_default(self):
        text = self._oc_args_after_run("--review")
        self.assertIn("\n-f\n", f"\n{text}")
        self.assertIn(str(self.brief), text)
        self.assertNotIn("сделай дело", text)

    def test_oc_run_inline_sends_the_brief_text_in_the_message_without_attachment(self):
        # gpt-6.1-sol не получает содержимое вложения: короткие сверки без инструментов идут текстом в сообщении.
        text = self._oc_args_after_run("--review", "--inline")
        self.assertNotIn("\n-f\n", f"\n{text}")
        self.assertIn("сделай дело", text)
        self.assertIn("Do not modify any files or create commits.", text)

    def test_oc_run_stops_a_hung_agent_by_wall_clock(self):
        # Зависший шлюз (ни одного события) не должен держать наблюдателя: OC_RUN_TIMEOUT прерывает прогон кодом 142.
        fake_oc = self.dir / "bin" / "opencode"
        fake_oc.write_text("#!/bin/sh\nexec sleep 30\n", encoding="utf-8")
        fake_oc.chmod(fake_oc.stat().st_mode | stat.S_IEXEC)
        env = self.env | {"PATH": f"{self.dir / 'bin'}:{os.environ['PATH']}", "OC_BUDGET": "off", "OC_RUN_TIMEOUT": "1"}
        started = time.monotonic()
        proc = self.run_script("oc_run.sh", self.worktree, "Tokenn/gpt-6.1-sol", self.brief, self.dir / "o.ndjson", "OC", "--inline", env=env)
        self.assertEqual(proc.returncode, 142, proc.stderr)
        self.assertIn("OC_RUN_TIMEOUT", proc.stderr)
        self.assertLess(time.monotonic() - started, 15)

    def test_oc_variants_apply_adds_only_variants_backs_up_and_is_idempotent(self):
        config = self.dir / "opencode.json"
        original = {"model": "x", "provider": {"Tokenn": {"options": {"apiKey": "SECRET-DO-NOT-PRINT"}, "models": {"codex-5.3": {"name": "codex-5.3"}, "gpt-6.1-sol": {"variants": {"low": {"reasoningEffort": "minimal"}}}}}}}
        config.write_text(json.dumps(original), encoding="utf-8")
        env = self.env | {"OC_CONFIG_PATH": str(config)}
        proc = self.run_script("oc_variants.py", "apply", env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("SECRET-DO-NOT-PRINT", proc.stdout + proc.stderr)
        data = json.loads(config.read_text(encoding="utf-8"))
        models = data["provider"]["Tokenn"]["models"]
        self.assertEqual(sorted(models["codex-5.3"]["variants"]), ["high", "low", "medium"])
        self.assertEqual(models["gpt-6.1-sol"]["variants"]["low"], {"reasoningEffort": "minimal"})  # существующий вариант не затёрт
        self.assertEqual(data["provider"]["Tokenn"]["options"], original["provider"]["Tokenn"]["options"])
        self.assertEqual(len(list(self.dir.glob("opencode.json.bak-*"))), 1)
        self.assertEqual(self.run_script("oc_variants.py", "has", "Tokenn/codex-5.3", "high", env=env).returncode, 0)
        self.assertEqual(self.run_script("oc_variants.py", "has", "Tokenn/gpt-5.5", "high", env=env).returncode, 1)
        again = self.run_script("oc_variants.py", "apply", env=env)
        self.assertIn("уже определены", again.stdout)

    def test_status_pick_and_report_run_without_network(self):
        status = self.run_script("agents.sh", "status")
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertIn("tool=dsh", status.stdout)
        self.assertIn("tool=opencode", status.stdout)
        self.assertIn("jev=unset", status.stdout)
        pick = self.run_script("agents.sh", "pick", "review")
        self.assertEqual(pick.returncode, 0, pick.stderr)
        self.assertRegex(pick.stdout, r"executor=(agy|dsh)")  # agy первый, если он установлен; в CI-контейнере его нет — тогда dsh
        self.assertEqual(self.run_script("agents.sh", "bogus").returncode, 2)

    def test_orchestra_config_is_valid_and_printed(self):
        valid = self.run_script("agents.sh", "orchestra", "validate")
        self.assertEqual(valid.returncode, 0, valid.stdout + valid.stderr)
        shown = self.run_script("agents.sh", "orchestra")
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertIn("executor-complex    agy → dsh", shown.stdout)
        self.assertIn("review              agy → dsh → opencode", shown.stdout)
        self.assertIn("strategy            argon-web* → claude*", shown.stdout)

    def test_check_builds_brief_without_tools_for_dsh(self):
        context = self.dir / "ctx.txt"
        context.write_text("def f():\n    return 1\n", encoding="utf-8")
        env = self.env | {"AGENTS_CHECK_WORKTREE": str(self.worktree)}
        proc = self.run_script("agents.sh", "check", "dsh", "t", "Есть ли ошибка?", context, self.dir / "checks.ndjson", env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        task = (self.dir / "task.txt").read_text(encoding="utf-8")
        self.assertIn("КОРОТКАЯ СВЕРКА", task)
        self.assertIn("ВОПРОС: Есть ли ошибка?", task)
        self.assertIn("def f()", task)
        self.assertTrue(task.startswith("ТОЛЬКО ЧТЕНИЕ"))


class PolicyTests(DshBase):
    def setUp(self):
        super().setUp()
        fake_agy = self.dir / "bin" / "agy"  # agy есть не везде (в CI-контейнере нет): лестнице нужен только факт наличия
        fake_agy.write_text("#!/bin/sh\n", encoding="utf-8")
        fake_agy.chmod(fake_agy.stat().st_mode | stat.S_IEXEC)
        self.env["PATH"] = f"{self.dir / 'bin'}:{os.environ['PATH']}"
        # Меняем только то, что отличается от текущего окружения, и возвращаем прежние значения (а не вычищаем окружение целиком).
        previous = {key: os.environ.get(key) for key, value in self.env.items() if os.environ.get(key) != value}
        os.environ.update({key: self.env[key] for key in previous})

        def restore():
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        self.addCleanup(restore)
        self.policy = load("executor_policy")

    def test_ladder_is_agy_then_dsh_and_opencode_only_for_checks_and_audits(self):
        # Приоритет стоимости (владелец, 17.10): agy бесплатный и несёт основную нагрузку, dsh платный, opencode дорогой и последний.
        for kind in self.policy.KINDS:
            order = self.policy.executor_order(kind)
            self.assertEqual(order[:2], ["agy", "dsh"], kind)
            self.assertEqual("opencode" in order, kind in ("review", "adversarial-review", "merge-audit", "short-check"), kind)
            if "opencode" in order:
                self.assertEqual(order[-1], "opencode", kind)
        self.assertEqual(self.policy.choose("review")["executor"], "agy")

    def test_unavailable_agy_falls_back_to_dsh_and_exhausted_dsh_budget_stops_the_ladder(self):
        real = self.policy.availability
        self.policy.availability = lambda agent, kind: (False, "agy недоступен (тест)") if agent == "agy" else real(agent, kind)
        self.assertEqual(self.policy.choose("review")["executor"], "dsh")
        os.environ["DSH_SPRINT_TOKEN_LIMIT"] = "1000"
        result = self.policy.choose("review")
        self.assertIsNone(result["executor"])  # opencode без ключа Jev недоступен, ручные ступени скрипты не запускают
        self.assertFalse(result["ladder"][1]["available"])

    def test_manual_steps_are_reported_but_never_run_by_scripts(self):
        self.assertEqual(self.policy.manual_steps("strategy"), ["argon-web", "claude"])
        self.assertEqual(self.policy.executor_order("strategy"), [])
        self.assertIsNone(self.policy.choose("strategy")["executor"])
        self.assertIn("argon-web", self.policy.manual_steps("adversarial-review"))

    def test_allowed_opencode_needs_a_jev_key_and_budget(self):
        self.policy.opencode_allowed = lambda kind: kind == "merge-audit"
        self.assertEqual(self.policy.executor_order("merge-audit"), ["agy", "dsh", "opencode"])
        os.environ["DSH_SPRINT_TOKEN_LIMIT"] = "1000"
        os.environ["PATH"] = f"{self.dir / 'bin'}:{os.environ['PATH']}"
        fake_oc = self.dir / "bin" / "opencode"
        fake_oc.write_text("#!/bin/sh\n", encoding="utf-8")
        fake_oc.chmod(fake_oc.stat().st_mode | stat.S_IEXEC)
        no_key = {step["agent"]: step for step in self.policy.choose("merge-audit")["ladder"]}["opencode"]
        self.assertFalse(no_key["available"])
        self.assertIn("Jev", no_key["detail"])
        os.environ["OPENROUTER_API_KEY"] = "test-key"
        self.assertTrue({step["agent"]: step for step in self.policy.choose("merge-audit")["ladder"]}["opencode"]["available"])

    def test_every_agent_has_a_static_policy_for_every_kind(self):
        policy = json.loads((TOOLS / "routing.json").read_text(encoding="utf-8"))
        for kind in self.policy.KINDS:
            for agent in ("agy", "opencode", "dsh"):
                answer = policy["tasks"][kind][agent]
                self.assertIn(answer["model"], policy["candidates"][agent], f"{kind}/{agent}")
                levels = policy["efforts"].get(agent) or policy["efforts"]["default"]
                self.assertIn(answer["effort"], levels, f"{kind}/{agent}")


class MonitorTests(DshBase):
    def test_digest_reports_alerts_commits_red_tests_and_run_end(self):
        stream = "".join(json.dumps(e) + "\n" for e in [
            {"type": "tool_call", "callId": "1", "tool": "bash", "input": {"command": "python -m unittest tests.x"}},
            {"type": "tool_result", "callId": "1", "status": "completed", "result": "FAIL: test_a (x.T)\nERROR: test_b (x.T)\nFAILED (failures=1, errors=1)"},
            {"type": "tool_call", "callId": "2", "tool": "bash", "input": {"command": "git commit -m x"}},
            {"type": "tool_call", "callId": "3", "tool": "bash", "input": {"command": "git push origin main"}},
            {"type": "tool_call", "callId": "4", "tool": "write", "input": {"file_path": "a/b/c.py", "content": "git push is only text here"}},
            {"type": "error", "message": "boom"},
            {"type": "run_end", "title": "T", "code": 0, "model": "deepseek-flash", "effort": "high"},
        ]) + step_end(10, 5, 7)
        proc = subprocess.run([sys.executable, str(TOOLS / "dsh_digest.py")], input=stream, text=True, capture_output=True, check=False, timeout=30)
        out = proc.stdout
        self.assertIn("DSH TESTS RED test_a,test_b", out)
        self.assertIn("DSH COMMIT", out)
        self.assertEqual(out.count("DSH ALERT"), 1)  # git push в shell — тревога, в тексте файла — нет
        self.assertIn("DSH ERROR boom", out)
        self.assertIn("DSH RUN END code=0 title=T model=deepseek-flash", out)

    def test_usage_lists_dsh_runs_with_cache(self):
        log = self.dir / "dsh.ndjson"
        log.write_text(step_end(100, 20, 300) + step_end(50, 10, 400) + json.dumps({"type": "run_end", "title": "T9", "code": 0, "model": "deepseek-flash"}) + "\n", encoding="utf-8")
        env = self.env | {"DSH_LOG": str(log), "AGY_LOG_DIR": str(self.dir), "AGENT_ROUTE_LOG": os.devnull, "OC_LOG": ""}
        proc = self.run_script("usage.sh", env=env)
        self.assertIn("agent=dsh title=T9 selected_model=- actual_model=deepseek-flash input=150 output=30 total=180 cost=cache_read=700", proc.stdout)

    def test_jev_report_counts_sources_cost_and_fallbacks(self):
        routes = self.dir / "routes.ndjson"
        routes.write_text("".join(json.dumps(r) + "\n" for r in [
            {"agent": "dsh", "model": "deepseek-flash", "source": "jev", "meta": {"cost": 0.0001, "latency_ms": 900, "confidence": 0.8}},
            {"agent": "dsh", "model": "deepseek-flash", "source": "fallback", "fallback_reason": "нет ключа OpenRouter"},
        ]), encoding="utf-8")
        decisions = self.dir / "decisions.ndjson"
        decisions.write_text(json.dumps({"choice": "ASK_HUMAN", "meta": {"cost": 0.0002}}) + "\n", encoding="utf-8")
        env = self.env | {"AGENT_ROUTE_LOG": str(routes), "AGENT_DECISION_LOG": str(decisions)}
        proc = subprocess.run([sys.executable, str(TOOLS / "jev_report.py")], text=True, capture_output=True, env=env, check=False)
        self.assertIn("выборов модели 2 (jev 1, откат на статику 1)", proc.stdout)
        self.assertIn("простых решений 1 (к человеку 1)", proc.stdout)
        self.assertIn("нет ключа OpenRouter ×1", proc.stdout)
        self.assertIn("0.00030000", proc.stdout)


if __name__ == "__main__":
    unittest.main()
