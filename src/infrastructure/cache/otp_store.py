"""Redis-backed OTP store for email verification codes.

Keys:   fp:otp:verify:{user_id}   → 6-digit code
TTL:    15 minutes (auto-deleted by Redis on expiry)

Falls back to a simple in-process dict when Redis is unavailable so the
app stays functional without Redis configured (useful in dev/test).
"""

from __future__ import annotations

import logging
import os
import random
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)

_KEY_PREFIX = "fp:otp:verify:"
_TTL_SECONDS = 15 * 60  # 15 minutes

# In-process fallback when Redis is not available
_fallback: dict[str, tuple[str, datetime]] = {}

_client = None  # lazy singleton


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
            logger.warning("OTP Redis unavailable, using in-process fallback: %s", exc)
            _client = False
            return None
    return _client


def generate_code() -> str:
    """Return a random 6-digit string, zero-padded."""
    return f"{random.randint(0, 999999):06d}"


async def save(user_id: str, code: str, ttl_seconds: int = _TTL_SECONDS) -> None:
    """Persist the OTP for ttl_seconds (default 15 min). Replaces any previous code."""
    r = await _redis()
    if r:
        await r.set(f"{_KEY_PREFIX}{user_id}", code, ex=ttl_seconds)
    else:
        _fallback[user_id] = (code, datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds))


async def get(user_id: str) -> Optional[str]:
    """Return the stored value without consuming it. Returns None if missing or expired."""
    r = await _redis()
    if r:
        return await r.get(f"{_KEY_PREFIX}{user_id}")
    else:
        entry = _fallback.get(user_id)
        if entry and datetime.now(timezone.utc) < entry[1]:
            return entry[0]
        return None


async def verify(user_id: str, code: str) -> bool:
    """Return True if the code matches and has not expired.  Consumes the code."""
    r = await _redis()
    if r:
        key = f"{_KEY_PREFIX}{user_id}"
        stored = await r.get(key)
        if stored and stored == code:
            await r.delete(key)
            return True
        return False
    else:
        entry = _fallback.get(user_id)
        if entry and entry[0] == code and datetime.now(timezone.utc) < entry[1]:
            del _fallback[user_id]
            return True
        return False


async def delete(user_id: str) -> None:
    """Remove the OTP regardless of whether it matched (e.g. on resend)."""
    r = await _redis()
    if r:
        await r.delete(f"{_KEY_PREFIX}{user_id}")
    else:
        _fallback.pop(user_id, None)
