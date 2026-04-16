"""
Sliding-window rate limiter backed by Redis.

Falls back to an in-process counter dict when Redis is unavailable.
"""

from __future__ import annotations

import logging
import os
import time
from collections import defaultdict, deque

logger = logging.getLogger(__name__)

_client = None
_fallback: dict[str, deque] = defaultdict(deque)  # key → deque of timestamps


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
            logger.warning("Rate-limiter Redis unavailable, using in-process fallback: %s", exc)
            _client = False
            return None
    return _client


async def is_allowed(key: str, limit: int, window_seconds: int) -> bool:
    """Return True if the request is within the rate limit, False if it should be rejected.

    Uses a sliding window stored as a Redis sorted set. Score = epoch seconds.
    """
    r = await _redis()
    now = time.time()
    window_start = now - window_seconds

    if r:
        rkey = f"fp:rl:{key}"
        pipe = r.pipeline()
        # Expire old entries outside the window
        pipe.zremrangebyscore(rkey, "-inf", window_start)
        # Add current request
        pipe.zadd(rkey, {str(now): now})
        # Count requests in window
        pipe.zcard(rkey)
        # Auto-expire the set after the window passes
        pipe.expire(rkey, window_seconds + 1)
        results = await pipe.execute()
        count: int = results[2]
        return count <= limit

    # In-process fallback (single process only)
    dq = _fallback[key]
    while dq and dq[0] < window_start:
        dq.popleft()
    dq.append(now)
    return len(dq) <= limit
