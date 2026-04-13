"""Redis-backed password-reset token store.

Design
------
We store two keys per reset request:

  fp:pwd_reset:hash:{token_hash}  → user_id   (TTL = expiry minutes)
  fp:pwd_reset:user:{user_id}     → token_hash (TTL = expiry minutes)

The second key lets us invalidate the previous token when a new one is issued
(one active reset per user at a time), mirroring what the DB repo did with
`revoke_active_tokens_for_user`.

Falls back to an in-process dict when Redis is not configured.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from src.config.settings import Settings

logger = logging.getLogger(__name__)

_HASH_PREFIX = "fp:pwd_reset:hash:"
_USER_PREFIX = "fp:pwd_reset:user:"

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
            logger.warning("Password-reset Redis unavailable, using fallback: %s", exc)
            _client = False
            return None
    return _client


# In-process fallback: hash → (user_id, expires_at)
_fallback_by_hash: dict[str, tuple[str, datetime]] = {}
# In-process fallback: user_id → hash
_fallback_by_user: dict[str, str] = {}


def _ttl_seconds() -> int:
    return int(getattr(Settings, "PASSWORD_RESET_TOKEN_EXPIRY_MINUTES", 30)) * 60


async def save(user_id: str, token_hash: str) -> None:
    """Persist a reset token and invalidate any previous one for this user."""
    ttl = _ttl_seconds()
    r = await _redis()
    if r:
        # Revoke previous token for this user, if any
        prev_hash = await r.get(f"{_USER_PREFIX}{user_id}")
        if prev_hash:
            await r.delete(f"{_HASH_PREFIX}{prev_hash}")

        await r.set(f"{_HASH_PREFIX}{token_hash}", user_id, ex=ttl)
        await r.set(f"{_USER_PREFIX}{user_id}", token_hash, ex=ttl)
    else:
        # Revoke previous
        prev_hash = _fallback_by_user.get(user_id)
        if prev_hash:
            _fallback_by_hash.pop(prev_hash, None)

        expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl)
        _fallback_by_hash[token_hash] = (user_id, expires_at)
        _fallback_by_user[user_id] = token_hash


async def get_user_id(token_hash: str) -> Optional[str]:
    """Return the user_id for a token hash if it exists and has not expired."""
    r = await _redis()
    if r:
        return await r.get(f"{_HASH_PREFIX}{token_hash}")
    else:
        entry = _fallback_by_hash.get(token_hash)
        if entry and datetime.now(timezone.utc) < entry[1]:
            return entry[0]
        return None


async def consume(user_id: str, token_hash: str) -> None:
    """Delete the token after a successful reset (single-use)."""
    r = await _redis()
    if r:
        await r.delete(f"{_HASH_PREFIX}{token_hash}")
        await r.delete(f"{_USER_PREFIX}{user_id}")
    else:
        _fallback_by_hash.pop(token_hash, None)
        _fallback_by_user.pop(user_id, None)
