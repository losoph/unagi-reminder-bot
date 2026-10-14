"""Человекочитаемая лента opencode --format json для вкладки терминала."""
import json
import sys
import textwrap
import time

CYAN, YELLOW, RED, DIM, BOLD, RESET = "\033[36m", "\033[33m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"
READS = {"read", "glob", "grep", "list"}


def say(prefix: str, text: str, color: str = "") -> None:
    stamp = time.strftime("%H:%M:%S")
    body = textwrap.fill(" ".join(text.split()), width=110, subsequent_indent=" " * 12)
    print(f"{DIM}{stamp}{RESET} {color}{prefix}{RESET} {body}", flush=True)


for line in sys.stdin:
    try:
        ev = json.loads(line)
    except ValueError:
        if line.strip():
            say("  ", line.strip()[:300], DIM)
        continue
    typ = ev.get("type")
    part = ev.get("part") or {}
    if typ in ("text", "reasoning"):
        text = part.get("text") or ""
        if text.strip():
            say("💭" if typ == "reasoning" else "🗣", text, CYAN if typ == "text" else DIM)
    elif typ == "tool_use":
        name = part.get("tool") or "?"
        state = part.get("state") or {}
        inp = state.get("input") or {}
        if name in READS:
            target = inp.get("path") or inp.get("filePath") or inp.get("pattern") or ""
            say("  ·", f"{name} {target}", DIM)
            continue
        brief = inp.get("command") or inp.get("filePath") or inp.get("path") or json.dumps(inp, ensure_ascii=False)
        say("🔧", f"{name}: {str(brief)[:300]}", YELLOW)
        out = str(state.get("output") or "").strip()
        if name == "bash" and out:
            tail = out.splitlines()[-3:]
            say("   ↳", " | ".join(tail)[:300], DIM)
    elif typ == "error":
        say("❌", json.dumps(ev.get("error"), ensure_ascii=False)[:400], RED)
    elif typ == "step_finish":
        reason = part.get("reason") or ""
        if reason and reason != "tool-calls":
            say("■", f"шаг завершён: {reason}", BOLD)
print(f"{BOLD}— прогон opencode завершён —{RESET}", flush=True)
