"""Redis-backed active session tracker.

Tracks which users have been active in the last SESSION_TTL seconds.
Used to show "X members online" in the dashboard.

Key:  fp:active:{user_id}  →  "1"  (TTL = SESSION_TTL)
Presence of the key == user is online.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

_SESSION_TTL = 15 * 60  # 15 minutes
_KEY_PREFIX = "fp:active:"
_client = None
_fallback: dict[str, datetime] = {}  # user_id → expires_at


async def _redis():
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
        except Exception as exc:
            logger.warning("Session Redis unavailable, using in-process fallback: %s", exc)
            _client = False
            return None
    return _client


async def touch(user_id: str) -> None:
    """Refresh (or create) the active-session marker for this user."""
    r = await _redis()
    if r:
        await r.set(f"{_KEY_PREFIX}{user_id}", "1", ex=_SESSION_TTL)
    else:
        _fallback[user_id] = datetime.now(timezone.utc) + timedelta(seconds=_SESSION_TTL)


async def is_active(user_id: str) -> bool:
    """Return True if the user has been active within the last SESSION_TTL seconds."""
    r = await _redis()
    if r:
        return bool(await r.exists(f"{_KEY_PREFIX}{user_id}"))
    entry = _fallback.get(user_id)
    if entry and datetime.now(timezone.utc) < entry:
        return True
    _fallback.pop(user_id, None)
    return False


async def count_active(user_ids: list[str]) -> int:
    """Return how many of the given user IDs are currently active."""
    if not user_ids:
        return 0
    r = await _redis()
    if r:
        keys = [f"{_KEY_PREFIX}{uid}" for uid in user_ids]
        return await r.exists(*keys)
    now = datetime.now(timezone.utc)
    return sum(1 for uid in user_ids if _fallback.get(uid, now) > now)


async def active_user_ids(user_ids: list[str]) -> list[str]:
    """Return the subset of user_ids that are currently active."""
    if not user_ids:
        return []
    r = await _redis()
    if r:
        pipe = r.pipeline()
        for uid in user_ids:
            pipe.exists(f"{_KEY_PREFIX}{uid}")
        results = await pipe.execute()
        return [uid for uid, alive in zip(user_ids, results) if alive]
    now = datetime.now(timezone.utc)
    return [uid for uid in user_ids if _fallback.get(uid, now) > now]
