"""Человекочитаемая лента `dsh --json` для вкладки терминала и итоговая строка расхода токенов."""
import json
import sys
import textwrap
import time

CYAN, YELLOW, RED, DIM, BOLD, RESET = "\033[36m", "\033[33m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"
READS = {"read", "glob", "grep", "list", "ls", "find"}
SHELLS = {"bash", "shell", "exec"}


def say(prefix: str, text: str, color: str = "") -> None:
    stamp = time.strftime("%H:%M:%S")
    body = textwrap.fill(" ".join(str(text).split()), width=110, subsequent_indent=" " * 12)
    print(f"{DIM}{stamp}{RESET} {color}{prefix}{RESET} {body}", flush=True)


steps = fresh = out = cached = 0
for line in sys.stdin:
    try:
        ev = json.loads(line)
    except ValueError:
        if line.strip():
            say("  ", line.strip()[:300], DIM)
        continue
    typ = ev.get("type")
    if typ == "session":
        say("▶", f"dsh сессия {ev.get('sessionId', '')[-12:]} в {ev.get('cwd', '')}", DIM)
    elif typ == "thinking" and str(ev.get("text") or "").strip():
        say("💭", ev["text"], DIM)
    elif typ == "text" and str(ev.get("text") or "").strip():
        say("🗣", ev["text"], CYAN)
    elif typ == "tool_call":
        name, inp = ev.get("tool") or "?", ev.get("input") or {}
        if name in READS:
            say("  ·", f"{name} {inp.get('file_path') or inp.get('path') or inp.get('pattern') or ''}", DIM)
        else:
            brief = inp.get("command") if name in SHELLS else (inp.get("file_path") or inp.get("path") or json.dumps(inp, ensure_ascii=False))
            say("🔧", f"{name}: {str(brief)[:300]}", YELLOW)
    elif typ == "tool_result" and ev.get("status") not in (None, "completed"):
        say("   ↳", f"{ev.get('status')}: {str(ev.get('result') or '')[:240]}", RED)
    elif typ == "error":
        say("❌", ev.get("message") or json.dumps(ev, ensure_ascii=False), RED)
    elif typ == "budget_exceeded":
        say("⛔", f"бюджет токенов: {ev.get('reason')} (прогон {ev.get('run_tokens')})", RED)
    elif typ == "status" and ev.get("phase") == "step_end":
        usage = ev.get("usage") or {}
        steps += 1
        fresh += int(usage.get("inputTokens") or 0)
        out += int(usage.get("outputTokens") or 0)
        cached += int(usage.get("cacheReadTokens") or 0)
    elif typ == "final" and str(ev.get("text") or "").strip():
        say("■", ev["text"], BOLD)
print(f"{BOLD}— прогон dsh завершён: шагов {steps}, новых входных {fresh}, выходных {out}, из кеша {cached} токенов —{RESET}", flush=True)
