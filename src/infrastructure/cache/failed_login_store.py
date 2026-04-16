"""Tracks failed login attempts and temporary account lockouts.

Keys:
  fp:login:fails:{email}  → failure count  (TTL: 15 min rolling window)
  fp:login:lock:{email}   → lockout flag   (TTL: 10 min hard lock)

Falls back to in-process dicts when Redis is unavailable.
"""

from __future__ import annotations

import logging
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)

_FAIL_TTL = 15 * 60    # seconds — window to count failures
_LOCK_TTL = 10 * 60    # seconds — lockout duration
_MAX_FAILURES = 5

_FAIL_PREFIX = "fp:login:fails:"
_LOCK_PREFIX = "fp:login:lock:"

# In-process fallback
_fail_fallback: dict[str, tuple[int, datetime]] = {}  # email → (count, expires)
_lock_fallback: dict[str, datetime] = {}               # email → lock_until

_client = None


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
            logger.warning("Failed login Redis unavailable, using in-process fallback: %s", exc)
            _client = False
            return None
    return _client


async def is_locked(email: str) -> bool:
    """Return True if this email is currently locked out."""
    r = await _redis()
    key = f"{_LOCK_PREFIX}{email.lower()}"
    if r:
        return bool(await r.exists(key))
    entry = _lock_fallback.get(email.lower())
    return entry is not None and datetime.now(timezone.utc) < entry


async def record_failure(email: str) -> int:
    """Increment failure count. Returns new total failures."""
    r = await _redis()
    key = f"{_FAIL_PREFIX}{email.lower()}"
    if r:
        count = await r.incr(key)
        if count == 1:
            await r.expire(key, _FAIL_TTL)
        return count
    else:
        now = datetime.now(timezone.utc)
        entry = _fail_fallback.get(email.lower())
        if entry is None or now >= entry[1]:
            count = 1
        else:
            count = entry[0] + 1
        _fail_fallback[email.lower()] = (count, now + timedelta(seconds=_FAIL_TTL))
        return count


async def lock_account(email: str) -> None:
    """Lock the account for _LOCK_TTL seconds."""
    r = await _redis()
    key = f"{_LOCK_PREFIX}{email.lower()}"
    if r:
        await r.set(key, "1", ex=_LOCK_TTL)
    else:
        _lock_fallback[email.lower()] = datetime.now(timezone.utc) + timedelta(seconds=_LOCK_TTL)


async def reset_failures(email: str) -> None:
    """Clear failure counter on successful login."""
    r = await _redis()
    if r:
        await r.delete(f"{_FAIL_PREFIX}{email.lower()}")
    else:
        _fail_fallback.pop(email.lower(), None)
