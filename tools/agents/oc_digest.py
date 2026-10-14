"""Экономный монитор opencode: срочные события сразу, обычные — периодической сводкой."""
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
    ready, _, _ = select.select([sys.stdin], [], [], 30)
    if ready:
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
            inp_obj = state.get("input") or {}
            inp = json.dumps(inp_obj, ensure_ascii=False)
            if RISK.search(inp):
                print(f"OC ALERT {name} {inp[:300]}", flush=True)
            if name in READS:
                reads += 1
                continue
            tools += 1
            for key in ("filePath", "path", "file_path"):
                value = inp_obj.get(key)
                if isinstance(value, str):
                    files.add(value.rsplit("/", 1)[-1])
            if "git commit" in inp:
                print("OC COMMIT " + inp[:300], flush=True)
            out = str(state.get("output") or "")
            if name == "bash" and "unittest" in inp and re.search(r"FAILED|Error|Traceback", out):
                tail = out.strip().splitlines()[-1:] or [""]
                print("OC TESTS RED " + tail[0][:200], flush=True)
        elif typ == "text":
            text = " ".join(str(part.get("text") or "").split())
            if text:
                says.append(text[:200])
    if time.time() - last >= PERIOD:
        emit()
