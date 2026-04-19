"""TOTP replay-attack prevention.

A TOTP code is valid for 30 seconds (pyotp default window). Without tracking
used codes, an attacker who intercepts a valid code can replay it within that
window. This store marks each (user_id, code) pair as consumed with a 90-second
TTL (3 × 30 s) — enough to outlive the TOTP window regardless of clock drift.
"""

from __future__ import annotations

import redis.asyncio as aioredis

from src.config.settings import Settings

_redis: aioredis.Redis | None = None


def _get_redis() -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(Settings.REDIS_URL, decode_responses=True)
    return _redis


async def mark_used(user_id: str, code: str) -> bool:
    """Atomically mark a TOTP code as used.

    Returns True if the code was NOT previously used (first use — allow).
    Returns False if it was already marked (replay — deny).
    TTL is 90 seconds to cover 3 TOTP windows.
    """
    key = f"totp_used:{user_id}:{code}"
    r = _get_redis()
    # SET NX EX — only sets if key does not exist; returns True on set, None on collision
    result = await r.set(key, "1", nx=True, ex=90)
    return result is True
