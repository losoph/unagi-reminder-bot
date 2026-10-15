#!/usr/bin/env python3
"""Получает решение Jev для выбора модели одной задачи."""

import json
import math
import os
import sys
import time
import urllib.error
import urllib.request
import http.client
from urllib.parse import urlsplit

MAX_BODY = 32 * 1024


class DecisionError(Exception):
    """Ошибка ответа или запроса Jev."""


DEFAULT_EFFORTS = {"low": "Низкий", "medium": "Средний", "high": "Высокий"}


def request_payload(task, candidates, efforts=None):
    efforts = efforts or DEFAULT_EFFORTS
    compact_candidates = dict(candidates)
    description = task.get("description", "")

    def build(text):
        state = {
            "task": text,
            "kind": task.get("task_kind", ""),
            "agent": task.get("agent", ""),
            "candidates": compact_candidates,
        }
        return {
            "model": os.environ.get("JEV_MODEL", "jev-latest"),
            "state": state,
            "questions": {
                "model": {
                    "type": "choice",
                    "instructions": "Выбери самый дешёвый кандидат, который надёжно и качественно справится с задачей.",
                    "criteria": {key: str(value) for key, value in candidates.items()},
                },
                "effort": {
                    "type": "choice",
                    "instructions": "Какой уровень усилий нужен для этой задачи?",
                    "criteria": dict(efforts),
                },
                "escalate": {
                    "type": "noul",
                    "instructions": "Не подходит ли ни один кандидат для надёжного и качественного выполнения задачи?",
                },
            },
        }

    text = description if isinstance(description, str) else json.dumps(description, ensure_ascii=False)
    payload = build(text)
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    if len(encoded) > MAX_BODY:
        # Сохраняем обязательный контекст кандидатов и ограничиваем только описание задачи.
        low, high = 0, len(text)
        while low < high:
            middle = (low + high + 1) // 2
            candidate_payload = build(text[:middle])
            candidate_bytes = json.dumps(candidate_payload, ensure_ascii=False, separators=(",", ":")).encode()
            if len(candidate_bytes) <= MAX_BODY:
                low = middle
            else:
                high = middle - 1
        payload = build(text[:low])
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    if len(encoded) > MAX_BODY:
        raise DecisionError("контекст кандидатов превышает лимит 32 KiB")
    return payload, encoded


def call_jev(body, key):
    """POST к Jev с повтором 429/529 под общим таймаутом. Возвращает (ответ, код_ошибки); коды как у decide()."""
    url = os.environ.get("JEV_URL", "https://openrouter.ai/api/v1/systemone")
    parsed_url = urlsplit(url)
    if parsed_url.scheme != "https" and not (
        parsed_url.scheme == "http" and parsed_url.hostname in {"localhost", "127.0.0.1", "::1"}
    ):
        print("JEV_URL должен использовать HTTPS (HTTP разрешён только для localhost)", file=sys.stderr)
        return None, 4
    if not parsed_url.hostname or parsed_url.username or parsed_url.password:
        print("некорректный JEV_URL", file=sys.stderr)
        return None, 4
    try:
        timeout = float(os.environ.get("JEV_TIMEOUT", "10"))
        if timeout <= 0:
            raise ValueError
    except ValueError:
        print("некорректная настройка таймаута или порога Jev", file=sys.stderr)
        return None, 4
    # Локальный адрес (тестовый сервер) идёт напрямую: системный прокси без NO_PROXY иначе ломает проверки в чужой вкладке.
    local = parsed_url.hostname in {"localhost", "127.0.0.1", "::1"}
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({})) if local else urllib.request.build_opener()
    deadline = time.monotonic() + timeout
    for attempt in range(3):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            print("исчерпан общий таймаут Jev", file=sys.stderr)
            return None, 5
        request = urllib.request.Request(
            url,
            data=body,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with opener.open(request, timeout=remaining) as result:
                return json.loads(result.read()), 0
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 529) and attempt < 2:
                pause = 0.2 * (2**attempt)
                remaining = deadline - time.monotonic()
                if remaining <= pause:
                    time.sleep(max(remaining, 0))
                    print("исчерпан общий таймаут Jev", file=sys.stderr)
                    return None, 5
                time.sleep(pause)
                continue
            print(f"ошибка HTTP Jev: {exc.code}", file=sys.stderr)
            return None, 5
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            # Не выводим тело/заголовки запроса: они могут содержать секреты.
            print(f"сетевая ошибка Jev: {type(exc).__name__}", file=sys.stderr)
            return None, 5
        except http.client.HTTPException:
            print("ошибка HTTP-протокола Jev", file=sys.stderr)
            return None, 5
        except (json.JSONDecodeError, UnicodeDecodeError):
            print("Jev вернул некорректный JSON", file=sys.stderr)
            return None, 4
    return None, 5


def decide(task, candidates, efforts=None):
    efforts = efforts or DEFAULT_EFFORTS
    key = os.environ.get("OPENROUTER_API_KEY") or os.environ.get("TYPESAFE_API_KEY")
    if not key:
        print("нет ключа OpenRouter", file=sys.stderr)
        return 3

    try:
        payload, body = request_payload(task, candidates, efforts)
    except (TypeError, ValueError, DecisionError) as exc:
        print(f"не удалось подготовить запрос Jev: {exc}", file=sys.stderr)
        return 4

    try:
        min_confidence = float(os.environ.get("JEV_MIN_CONFIDENCE", "0.4"))
        if not 0 <= min_confidence <= 1:
            raise ValueError
    except ValueError:
        print("некорректная настройка таймаута или порога Jev", file=sys.stderr)
        return 4
    started = time.monotonic()
    response, code = call_jev(body, key)
    if code:
        return code

    try:
        if not isinstance(response, dict):
            raise ValueError
        answers = response["answers"]
        model_answer = answers["model"]
        effort_answer = answers["effort"]
        escalate_answer = answers["escalate"]
        model = model_answer["choice"]
        effort = effort_answer["choice"]
        probabilities = model_answer["probabilities"]
        confidence = float(model_answer["confidence"])
        escalate = float(escalate_answer["noul"])
        probability = float(probabilities[model])
        if model not in candidates or effort not in efforts:
            raise ValueError
        if not isinstance(probabilities, dict) or any(key not in candidates for key in probabilities):
            raise ValueError
        if not all(isinstance(value, (int, float)) and math.isfinite(value) and 0 <= value <= 1 for value in probabilities.values()):
            raise ValueError
        if model not in probabilities:
            raise ValueError
        if not all(math.isfinite(value) and 0 <= value <= 1 for value in (confidence, escalate, probability)):
            raise ValueError
    except (KeyError, TypeError, ValueError, AttributeError):
        print("Jev вернул некорректный ответ или модель вне списка кандидатов", file=sys.stderr)
        return 4

    jev_model = response.get("model")
    if not isinstance(jev_model, str) or not jev_model:
        print("Jev вернул некорректную модель решения", file=sys.stderr)
        return 4
    usage = response.get("usage") or {}
    cost = usage.get("cost")
    if cost is not None and (not isinstance(cost, (int, float)) or not math.isfinite(cost) or cost < 0):
        print("Jev вернул некорректную стоимость", file=sys.stderr)
        return 4
    meta = {
        "router": "jev",
        "jev_model": jev_model,
        "probabilities": probabilities,
        "confidence": confidence,
        "escalate": escalate,
        "cost": cost,
        "latency_ms": round((time.monotonic() - started) * 1000),
    }
    if confidence < min_confidence:
        print("уверенность Jev ниже порога", file=sys.stderr)
        return 6
    if escalate >= 0.5:
        print("Jev рекомендует эскалацию: подходящего кандидата нет", file=sys.stderr)
        return 6
    print(f"model={model} effort={effort} reason=jev p={probability:g} conf={confidence:g}")
    print("meta=" + json.dumps(meta, ensure_ascii=False, separators=(",", ":")))
    return 0


def main():
    try:
        task = json.load(sys.stdin)
        if not isinstance(task, dict):
            raise ValueError
        with open(os.environ.get("AGENT_ROUTING_POLICY", os.path.join(os.path.dirname(__file__), "..", "routing.json")), encoding="utf-8") as stream:
            policy = json.load(stream)
        agent = task["agent"]
        candidate_ids = policy["candidates"][agent]
        descriptions = policy.get("candidate_descriptions", {}).get(agent, {})
        candidates = {key: descriptions.get(key, key) for key in candidate_ids}
        levels = (policy.get("efforts") or {}).get(agent) or (policy.get("efforts") or {}).get("default") or list(DEFAULT_EFFORTS)
        described = (policy.get("effort_descriptions") or {}).get(agent) or (policy.get("effort_descriptions") or {}).get("default") or {}
        efforts = {level: described.get(level) or DEFAULT_EFFORTS.get(level, level) for level in levels}
        if not isinstance(candidates, dict) or not candidates:
            raise ValueError
        if any(not isinstance(value, (dict, str)) for value in candidates.values()):
            raise ValueError
    except (json.JSONDecodeError, OSError, KeyError, TypeError, ValueError):
        print("некорректная задача или конфигурация маршрутизации", file=sys.stderr)
        return 4
    return decide(task, candidates, efforts)


if __name__ == "__main__":
    raise SystemExit(main())
