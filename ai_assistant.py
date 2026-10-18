import asyncio
import logging
import time

import aiohttp

from config import settings
from http_session import get_session

logger = logging.getLogger(__name__)

DEEPSEEK_API_KEY = settings().ai.api_key
DEEPSEEK_BASE_URL = settings().ai.base_url
DEEPSEEK_MODEL = settings().ai.model
AI_DAILY_LIMIT = settings().ai.daily_limit
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=60)
KEY_CHECK_INTERVAL_SECONDS = settings().ai.key_check_interval_seconds
AUTH_ERROR_COOLDOWN_SECONDS: int = 3600

_last_auth_error_time: float = 0.0
_auth_error_logged: bool = False


def _handle_auth_error(status: int, now: float | None = None) -> None:
    global _last_auth_error_time, _auth_error_logged
    current_time = time.monotonic() if now is None else now
    msg = f"ключ DeepSeek недействителен ({status})"
    if not _auth_error_logged:
        logger.error(msg)
        _auth_error_logged = True
        _last_auth_error_time = current_time
    elif current_time - _last_auth_error_time >= AUTH_ERROR_COOLDOWN_SECONDS:
        logger.warning(msg)
        _last_auth_error_time = current_time


def _handle_auth_success() -> None:
    global _last_auth_error_time, _auth_error_logged
    _auth_error_logged = False
    _last_auth_error_time = 0.0


def reset_auth_error_state() -> None:
    """Сброс состояния троттлинга ошибок авторизации (для тестов)."""
    global _last_auth_error_time, _auth_error_logged
    _auth_error_logged = False
    _last_auth_error_time = 0.0


def is_enabled() -> bool:
    return bool(DEEPSEEK_API_KEY)


async def check_api_key(session: aiohttp.ClientSession | None = None) -> bool:
    """HP1: Проверка ключа DeepSeek при старте или по расписанию.

    Делает легковесный запрос GET /models без списания токенов.
    При HTTP 401/403 пишет в лог ОДНУ строку ERROR (повторы через >= 1 час — WARNING).
    Ключ в лог никогда не выводится, трейсбеки отсутствуют.
    """
    if not DEEPSEEK_API_KEY:
        return False

    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
    }
    http_sess = session or get_session()
    try:
        req = http_sess.get(
            f"{DEEPSEEK_BASE_URL}/models",
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )
        async with req as response:
            if response.status in (401, 403):
                _handle_auth_error(response.status)
                return False
            if response.status != 200:
                logger.warning("DeepSeek API ping returned HTTP %s", response.status)
                return False
            _handle_auth_success()
            return True
    except Exception:
        logger.warning("Не удалось выполнить проверку ключа DeepSeek")
        return False


ping = check_api_key


async def run_key_check_loop(interval_seconds: int = KEY_CHECK_INTERVAL_SECONDS) -> None:
    """Фоновый цикл периодической проверки ключа DeepSeek."""
    while True:
        try:
            await asyncio.sleep(interval_seconds)
            await check_api_key()
        except asyncio.CancelledError:
            break
        except Exception:
            logger.warning("Ошибка в фоновой проверке ключа DeepSeek")


async def ask(
    system_prompt: str,
    user_prompt: str,
    *,
    max_tokens: int = 800,
    temperature: float = 0.4,
    session: aiohttp.ClientSession | None = None,
) -> str | None:
    """Send a single-turn prompt to DeepSeek. Returns None if disabled or on error."""
    if not DEEPSEEK_API_KEY:
        return None

    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": False,
    }
    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json",
    }

    http_sess = session or get_session()
    try:
        post_req = http_sess.post(
            f"{DEEPSEEK_BASE_URL}/chat/completions",
            json=payload,
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )
        async with post_req as response:
            if response.status in (401, 403):
                _handle_auth_error(response.status)
                return None
            if response.status != 200:
                body = await response.text()
                logger.error("DeepSeek HTTP %s: %s", response.status, body[:300])
                return None
            _handle_auth_success()
            data = await response.json()
        return (data["choices"][0]["message"]["content"] or "").strip() or None
    except Exception:
        logger.exception("DeepSeek request failed")
        return None
