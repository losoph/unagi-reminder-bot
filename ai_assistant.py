import logging
import os

import aiohttp

logger = logging.getLogger(__name__)

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
AI_DAILY_LIMIT = int(os.getenv("AI_DAILY_LIMIT", "10"))
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=60)


def is_enabled() -> bool:
    return bool(DEEPSEEK_API_KEY)


async def ask(
    system_prompt: str,
    user_prompt: str,
    *,
    max_tokens: int = 800,
    temperature: float = 0.4,
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

    try:
        async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
            async with session.post(
                f"{DEEPSEEK_BASE_URL}/chat/completions",
                json=payload,
                headers=headers,
            ) as response:
                if response.status != 200:
                    body = await response.text()
                    logger.error("DeepSeek HTTP %s: %s", response.status, body[:300])
                    return None
                data = await response.json()
        return (data["choices"][0]["message"]["content"] or "").strip() or None
    except Exception:
        logger.exception("DeepSeek request failed")
        return None
