"""Redis-backed store for pending MFA challenges.

After a successful password check the login endpoint issues a short-lived
`mfa_token` JWT instead of the full access token.  We track that token's
ID here so it can only be consumed once.

Keys:   fp:mfa:challenge:{user_id}  → challenge_id (UUID)
TTL:    5 minutes
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)

_KEY_PREFIX = "fp:mfa:challenge:"
_TTL_SECONDS = 5 * 60  # 5 minutes

_fallback: dict[str, tuple[str, datetime]] = {}
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
            logger.warning("MFA Redis unavailable, using in-process fallback: %s", exc)
            _client = False
            return None
    return _client


async def create(user_id: str) -> str:
    """Create a new challenge ID for this user and persist it for 5 minutes."""
    challenge_id = str(uuid.uuid4())
    r = await _redis()
    if r:
        await r.set(f"{_KEY_PREFIX}{user_id}", challenge_id, ex=_TTL_SECONDS)
    else:
        _fallback[user_id] = (
            challenge_id,
            datetime.now(timezone.utc) + timedelta(seconds=_TTL_SECONDS),
        )
    return challenge_id


async def consume(user_id: str, challenge_id: str) -> bool:
    """Return True and delete the entry if the challenge_id matches."""
    r = await _redis()
    if r:
        key = f"{_KEY_PREFIX}{user_id}"
        stored = await r.get(key)
        if stored and stored == challenge_id:
            await r.delete(key)
            return True
        return False
    else:
        entry = _fallback.get(user_id)
        if (
            entry
            and entry[0] == challenge_id
            and datetime.now(timezone.utc) < entry[1]
        ):
            del _fallback[user_id]
            return True
        return False


async def delete(user_id: str) -> None:
    r = await _redis()
    if r:
        await r.delete(f"{_KEY_PREFIX}{user_id}")
    else:
        _fallback.pop(user_id, None)
