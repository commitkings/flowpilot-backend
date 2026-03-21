"""Short-term working memory: recent chat turns mirrored in Redis."""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)

_MAX_MESSAGES = 40
_KEY_PREFIX = "fp:wm:conv:"
_TTL_SECONDS = 86400 * 7

_client: Any = None


async def _redis() -> Optional[Any]:
    global _client
    if _client is False:
        return None
    url = os.getenv("REDIS_URL", "").strip()
    if not url:
        _client = False
        return None
    if _client is None:
        try:
            import redis.asyncio as redis

            _client = redis.from_url(url, decode_responses=True)
        except Exception as e:
            logger.warning("Redis working memory unavailable: %s", e)
            _client = False
            return None
    return _client


def _key(conversation_id: str) -> str:
    return f"{_KEY_PREFIX}{conversation_id}"


async def append_turn(conversation_id: str, role: str, content: str) -> None:
    r = await _redis()
    if not r:
        return
    try:
        payload = json.dumps({"role": role, "content": content})
        k = _key(conversation_id)
        await r.rpush(k, payload)
        await r.ltrim(k, -_MAX_MESSAGES, -1)
        await r.expire(k, _TTL_SECONDS)
    except Exception as e:
        logger.warning("redis append_turn failed: %s", e)


async def get_recent_turns(conversation_id: str, limit: int = 24) -> list[dict[str, str]]:
    r = await _redis()
    if not r:
        return []
    try:
        k = _key(conversation_id)
        raw = await r.lrange(k, -limit, -1)
        out: list[dict[str, str]] = []
        for item in raw:
            try:
                d = json.loads(item)
                if isinstance(d, dict) and "role" in d and "content" in d:
                    out.append({"role": str(d["role"]), "content": str(d["content"])})
            except (json.JSONDecodeError, TypeError):
                continue
        return out
    except Exception as e:
        logger.warning("redis get_recent_turns failed: %s", e)
        return []
