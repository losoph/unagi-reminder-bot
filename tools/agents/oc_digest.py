"""Экономный монитор opencode --format json: коммиты и ошибки сразу, остальное сводкой раз в PERIOD."""
import json
import os
import re
import select
import sys
import time

PERIOD = int(os.environ.get("PERIOD", "600"))
READS = {"read", "glob", "grep", "list", "webfetch"}
RISK = re.compile(r"git push|reset --hard|rm -rf|\.env\b|msmtprc|TELEGRAM_USER_SESSION")

tools, reads, files, says = 0, 0, set(), []
last = time.time()


def emit():
    global tools, reads, files, says, last
    if tools or says:
        parts = [f"OC DIGEST {time.strftime('%H:%M')}: действий {tools}, чтений {reads}"]
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
            print("OC STREAM END", flush=True)
            break
        try:
            ev = json.loads(line)
        except ValueError:
            if line.strip():
                print("OC RAW " + line.strip()[:300], flush=True)
            continue
        typ = ev.get("type")
        part = ev.get("part") or {}
        if typ == "run_end":
            emit()
            print(f"OC RUN END code={ev.get('code')} title={ev.get('title')}", flush=True)
        elif typ == "error":
            print("OC ERROR " + json.dumps(ev.get("error"), ensure_ascii=False)[:400], flush=True)
        elif typ == "tool_use":
            name = part.get("tool") or "?"
            state = part.get("state") or {}
            raw_input = state.get("input") or {}
            inp = json.dumps(raw_input, ensure_ascii=False)
            # Тревоги и коммиты — только по shell-командам: в патчах файлов те же слова — просто текст.
            command = str(raw_input.get("command") or "") if name in ("bash", "shell") else ""
            if RISK.search(command):
                print(f"OC ALERT {name} {inp[:300]}", flush=True)
            if name in READS:
                reads += 1
                continue
            tools += 1
            for key in ("filePath", "path", "file_path"):
                v = (state.get("input") or {}).get(key)
                if isinstance(v, str):
                    files.add(v.rsplit("/", 1)[-1])
            if "git commit" in command:
                print("OC COMMIT " + inp[:300], flush=True)
            out = str(state.get("output") or "")
            if name in ("bash", "shell") and "unittest" in command and re.search(r"^FAILED \(", out, re.M):
                failed = sorted(set(re.findall(r"^(?:FAIL|ERROR): (\S+)", out, re.M)))
                signature = ",".join(failed) or out.strip().splitlines()[-1][:200]
                if signature != globals().get("_last_red"):
                    globals()["_last_red"] = signature
                    print("OC TESTS RED " + signature[:300], flush=True)
        elif typ == "text":
            text = " ".join(str(part.get("text") or "").split())
            if text:
                says.append(text[:200])
    if time.time() - last >= PERIOD:
        emit()
