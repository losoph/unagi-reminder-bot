#!/usr/bin/env python3
"""Простое решение через Jev: один вопрос, закрытый список вариантов, уверенность и эскалация к человеку.

Использование:
    jev_ask.py "Вопрос" вариант=описание вариант=описание … [--default вариант] [--context-file файл]
               [--title метка] [--dry-run]

Код выхода: 0 — решение принято (Jev или безопасный вариант --default), 10 — решает человек, 2 — ошибка вызова.
Jev только советует: действия с внешними последствиями (push, откат прода) выполняет наблюдатель.
"""

import argparse
import json
import math
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import jev_decide  # noqa: E402

MAX_BODY = 32 * 1024
OPTION_KEY = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
SECRET = re.compile(r"(?i)(sk-[A-Za-z0-9_\-]{8,}|bearer\s+[A-Za-z0-9._\-]{8,}|(?:api[_-]?key|token|secret|password)\s*[=:]\s*\S+)")


def redact(text):
    """Ключи не должны уходить во внешний сервис, даже случайно попав в контекст."""
    return SECRET.sub("[REDACTED]", text)


def parse_args(argv):
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("question")
    parser.add_argument("options", nargs="+")
    parser.add_argument("--default")
    parser.add_argument("--context-file")
    parser.add_argument("--title", default="")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    options = {}
    for item in args.options:
        key, sep, description = item.partition("=")
        if not sep or not OPTION_KEY.match(key) or not description.strip():
            raise ValueError(f"вариант должен быть вида имя=описание, получено: {item[:40]!r}")
        if key in options:
            raise ValueError(f"вариант повторяется: {key}")
        options[key] = redact(description.strip())
    if not 2 <= len(options) <= 8:
        raise ValueError("нужно от 2 до 8 вариантов")
    if args.default is not None and args.default not in options:
        raise ValueError("--default должен быть одним из вариантов")
    return args, options


def build_request(question, options, context):
    def build(text):
        return {
            "model": os.environ.get("JEV_MODEL", "jev-latest"),
            "state": {"question": question, "context": text},
            "questions": {
                "decision": {"type": "choice", "instructions": question, "criteria": options},
                "escalate": {
                    "type": "noul",
                    "instructions": "Данных недостаточно или ни один вариант не подходит надёжно — решение должен принять человек?",
                },
            },
        }

    def encode(payload):
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()

    low, high = 0, len(context)
    while low < high:
        middle = (low + high + 1) // 2
        if len(encode(build(context[:middle]))) <= MAX_BODY:
            low = middle
        else:
            high = middle - 1
    body = encode(build(context[:low]))
    if len(body) > MAX_BODY:
        raise ValueError("вопрос и варианты превышают лимит 32 KiB")
    return body


def validate(response, options):
    """Возвращает (выбор, вероятности, уверенность, вероятность эскалации); бросает ValueError на любую неточность."""
    if not isinstance(response, dict):
        raise ValueError
    answers = response["answers"]
    answer = answers["decision"]
    choice = answer["choice"]
    probabilities = answer["probabilities"]
    confidence = float(answer["confidence"])
    escalate = float(answers["escalate"]["noul"])
    if choice not in options or not isinstance(probabilities, dict) or choice not in probabilities:
        raise ValueError
    if any(key not in options for key in probabilities):
        raise ValueError
    values = list(probabilities.values()) + [confidence, escalate]
    if not all(isinstance(v, (int, float)) and math.isfinite(v) and 0 <= v <= 1 for v in values):
        raise ValueError
    if not isinstance(response.get("model"), str) or not response["model"]:
        raise ValueError
    return choice, probabilities, confidence, escalate


def threshold():
    value = float(os.environ.get("JEV_DECIDE_MIN_CONFIDENCE", "0.7"))
    if not 0 <= value <= 1:
        raise ValueError
    return value


def finish(args, options, choice, source, confidence=None, reason="", meta=None):
    """Печатает решение и пишет журнал. Без --default и без решения Jev выбор остаётся за человеком."""
    human = choice is None
    shown = "ASK_HUMAN" if human else choice
    line = f"choice={shown} source={source}"
    if confidence is not None:
        line += f" confidence={confidence:g}"
    print(line)
    if reason:
        print(f"fallback={reason}")
    if meta:
        print("meta=" + json.dumps(meta, ensure_ascii=False, separators=(",", ":")))
    if not args.dry_run:
        log = os.environ.get("AGENT_DECISION_LOG", ".agents/decisions.ndjson")
        os.makedirs(os.path.dirname(log) or ".", exist_ok=True)
        row = {
            "ts": int(time.time()),
            "title": args.title,
            "question": redact(args.question),
            "options": list(options),
            "choice": shown,
            "source": source,
            "confidence": confidence,
            "fallback_reason": reason or None,
            "meta": meta or {},
        }
        fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    return 10 if human else 0


def main(argv):
    try:
        args, options = parse_args(argv)
        question = redact(args.question)
        context = ""
        if args.context_file:
            with open(args.context_file, encoding="utf-8", errors="replace") as stream:
                context = redact(stream.read(MAX_BODY * 2))
        body = build_request(question, options, context)
        min_confidence = threshold()
    except (SystemExit, ValueError, OSError) as exc:
        if isinstance(exc, SystemExit):
            print("использование: decide.sh \"вопрос\" вариант=описание вариант=описание … [--default вариант] [--context-file файл]", file=sys.stderr)
        else:
            print(f"ошибка вызова: {exc}", file=sys.stderr)
        return 2

    def fallback(reason):
        if args.default:
            return finish(args, options, args.default, "default", reason=reason)
        return finish(args, options, None, "human", reason=reason)

    key = os.environ.get("OPENROUTER_API_KEY") or os.environ.get("TYPESAFE_API_KEY")
    if not key:
        return fallback("нет ключа OpenRouter")
    started = time.monotonic()
    response, code = jev_decide.call_jev(body, key)
    if code:
        return fallback(f"jev_exit_{code}")
    try:
        choice, probabilities, confidence, escalate = validate(response, options)
    except (KeyError, TypeError, ValueError, AttributeError):
        return fallback("Jev вернул некорректный ответ")
    usage = response.get("usage") or {}
    cost = usage.get("cost")
    meta = {
        "router": "jev",
        "jev_model": response["model"],
        "probabilities": probabilities,
        "confidence": confidence,
        "escalate": escalate,
        "cost": cost if isinstance(cost, (int, float)) and math.isfinite(cost) and cost >= 0 else None,
        "latency_ms": round((time.monotonic() - started) * 1000),
    }
    if escalate >= 0.5:
        return finish(args, options, None, "human", confidence, "Jev рекомендует передать решение человеку", meta)
    if confidence < min_confidence:
        return finish(args, options, args.default, "default" if args.default else "human", confidence, "уверенность Jev ниже порога", meta)
    return finish(args, options, choice, "jev", confidence, meta=meta)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
