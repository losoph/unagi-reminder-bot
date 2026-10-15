"""Экономный монитор `dsh --json`: коммиты, ошибки, красные тесты и конец прогона сразу, остальное сводкой раз в PERIOD."""
import json
import os
import re
import select
import sys
import time

PERIOD = int(os.environ.get("PERIOD", "600"))
READS = {"read", "glob", "grep", "list", "ls", "find"}
SHELLS = {"bash", "shell", "exec"}
RISK = re.compile(r"git push|reset --hard|rm -rf|\.env\b|msmtprc|TELEGRAM_USER_SESSION")

tools = reads = steps = fresh = out = cached = 0
files, says = set(), []
commands = {}  # callId -> shell-команда: результат теста привязываем к ней
last = time.time()
last_red = None


def emit():
    global tools, reads, files, says, last
    if tools or says:
        parts = [f"DSH DIGEST {time.strftime('%H:%M')}: действий {tools}, чтений {reads}, токены новых {fresh}+{out} (кеш {cached})"]
        if files:
            parts.append("правки: " + ", ".join(sorted(files))[:300])
        if says:
            parts.append("мысли: " + " | ".join(says[-3:])[:600])
        print(" · ".join(parts), flush=True)
    tools, reads, files, says = 0, 0, set(), []
    last = time.time()


while True:
    r, _, _ = select.select([sys.stdin], [], [], 30)
    if r:
        line = sys.stdin.readline()
        if not line:
            emit()
            print("DSH STREAM END", flush=True)
            break
        try:
            ev = json.loads(line)
        except ValueError:
            if line.strip():
                print("DSH RAW " + line.strip()[:300], flush=True)
            continue
        typ = ev.get("type")
        if typ == "run_end":
            emit()
            print(f"DSH RUN END code={ev.get('code')} title={ev.get('title')} model={ev.get('model')} effort={ev.get('effort')} "
                  f"tokens новых {fresh}+{out}, кеш {cached}, шагов {steps}", flush=True)
        elif typ == "error":
            print("DSH ERROR " + str(ev.get("message") or json.dumps(ev, ensure_ascii=False))[:400], flush=True)
        elif typ == "budget_exceeded":
            print(f"DSH BUDGET STOP {ev.get('reason')} run_tokens={ev.get('run_tokens')}", flush=True)
        elif typ == "status" and ev.get("phase") == "step_end":
            usage = ev.get("usage") or {}
            steps += 1
            fresh += int(usage.get("inputTokens") or 0)
            out += int(usage.get("outputTokens") or 0)
            cached += int(usage.get("cacheReadTokens") or 0)
        elif typ == "tool_call":
            name, raw = ev.get("tool") or "?", ev.get("input") or {}
            command = str(raw.get("command") or "") if name in SHELLS else ""
            if command:
                commands[ev.get("callId")] = command
            # Тревоги и коммиты — только по shell-командам: в патчах файлов те же слова — просто текст.
            if RISK.search(command):
                print(f"DSH ALERT {name} {json.dumps(raw, ensure_ascii=False)[:300]}", flush=True)
            if name in READS:
                reads += 1
                continue
            tools += 1
            for key in ("file_path", "path", "filePath"):
                if isinstance(raw.get(key), str):
                    files.add(raw[key].rsplit("/", 1)[-1])
            if "git commit" in command:
                print("DSH COMMIT " + json.dumps(raw, ensure_ascii=False)[:300], flush=True)
        elif typ == "tool_result":
            command = commands.pop(ev.get("callId"), "")
            result = str(ev.get("result") or "")
            if "unittest" in command and re.search(r"^FAILED \(", result, re.M):
                failed = sorted(set(re.findall(r"^(?:FAIL|ERROR): (\S+)", result, re.M)))
                signature = ",".join(failed) or result.strip().splitlines()[-1][:200]
                if signature != last_red:
                    last_red = signature
                    print("DSH TESTS RED " + signature[:300], flush=True)
        elif typ == "text":
            text = " ".join(str(ev.get("text") or "").split())
            if text:
                says.append(text[:200])
    if time.time() - last >= PERIOD:
        emit()
