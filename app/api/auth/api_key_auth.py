"""
FastAPI dependency for API-key authentication.

API keys are issued per business (org) and carry a set of scopes.
The raw key is never stored — only a PBKDF2-SHA256 hash.

Key format: fp_<43 url-safe base64 chars>  (total ~46 chars)
Stored:     key_prefix (first 10 chars)  +  key_hash (PBKDF2 of full key)

Authentication header:
    Authorization: Bearer fp_<key>
  or
    X-API-Key: fp_<key>
"""

import base64
import hashlib
import hmac
import secrets
import uuid
from datetime import datetime, timezone
from typing import NamedTuple

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.infrastructure.database.connection import get_db_session
from src.infrastructure.database.flowpilot_models import ApiKeyModel

_PBKDF2_ITERATIONS = 100_000


# --------------------------------------------------------------------------- #
# Internal helpers
# --------------------------------------------------------------------------- #

def _hash_api_key(raw_key: str) -> str:
    """Return a PBKDF2-SHA256 hash of *raw_key* in the same format as passwords."""
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", raw_key.encode(), salt, _PBKDF2_ITERATIONS)
    salt_b64 = base64.urlsafe_b64encode(salt).decode()
    dk_b64 = base64.urlsafe_b64encode(dk).decode()
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${salt_b64}${dk_b64}"


def _verify_api_key(raw_key: str, stored_hash: str) -> bool:
    """Verify *raw_key* against a stored PBKDF2 hash."""
    parts = stored_hash.split("$")
    if len(parts) != 4:
        return False
    scheme, iter_str, salt_b64, expected_b64 = parts
    if scheme != "pbkdf2_sha256":
        return False
    iterations = int(iter_str)
    salt = base64.urlsafe_b64decode(salt_b64)
    expected = base64.urlsafe_b64decode(expected_b64)
    candidate = hashlib.pbkdf2_hmac("sha256", raw_key.encode(), salt, iterations)
    return hmac.compare_digest(candidate, expected)


def generate_api_key() -> tuple[str, str, str]:
    """
    Generate a new API key.

    Returns:
        (raw_key, key_prefix, key_hash)
        raw_key    — the full key shown once to the user
        key_prefix — first 10 chars, stored for fast lookup
        key_hash   — PBKDF2 hash, stored in DB
    """
    raw_key = "fp_" + secrets.token_urlsafe(32)
    key_prefix = raw_key[:10]
    key_hash = _hash_api_key(raw_key)
    return raw_key, key_prefix, key_hash


# --------------------------------------------------------------------------- #
# ApiKeyContext — returned by the dependency
# --------------------------------------------------------------------------- #

class ApiKeyContext(NamedTuple):
    business_id: uuid.UUID
    api_key_id: uuid.UUID
    scopes: list[str]


# --------------------------------------------------------------------------- #
# FastAPI dependency
# --------------------------------------------------------------------------- #

async def get_api_key_context(
    x_api_key: str | None = Header(None, alias="X-API-Key"),
    authorization: str | None = Header(None),
    session: AsyncSession = Depends(get_db_session),
) -> ApiKeyContext:
    """
    Extract and validate an API key from the request.

    Accepts:
        X-API-Key: fp_<key>
      or
        Authorization: Bearer fp_<key>

    Raises 401 if the key is missing, malformed, not found, revoked, or invalid.
    """
    raw_key: str | None = None

    if x_api_key and x_api_key.startswith("fp_"):
        raw_key = x_api_key
    elif authorization:
        parts = authorization.split(" ", 1)
        if len(parts) == 2 and parts[0].lower() == "bearer" and parts[1].startswith("fp_"):
            raw_key = parts[1]

    if not raw_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API key required. Pass via X-API-Key or Authorization: Bearer fp_<key>",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Fast prefix lookup
    key_prefix = raw_key[:10]
    result = await session.execute(
        select(ApiKeyModel).where(
            ApiKeyModel.key_prefix == key_prefix,
            ApiKeyModel.revoked_at.is_(None),
        )
    )
    candidates = result.scalars().all()

    matched: ApiKeyModel | None = None
    for candidate in candidates:
        if _verify_api_key(raw_key, candidate.key_hash):
            matched = candidate
            break

    if matched is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or revoked API key",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Reject expired keys
    if matched.expires_at and matched.expires_at < datetime.now(timezone.utc):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API key has expired",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Best-effort update of last_used_at (never fails the request)
    try:
        await session.execute(
            update(ApiKeyModel)
            .where(ApiKeyModel.id == matched.id)
            .values(last_used_at=datetime.now(timezone.utc))
        )
        await session.commit()
    except Exception:
        pass

    return ApiKeyContext(
        business_id=matched.business_id,
        api_key_id=matched.id,
        scopes=list(matched.scopes or []),
    )


# --------------------------------------------------------------------------- #
# Scope-enforcement factory
# --------------------------------------------------------------------------- #

def require_scope(*required_scopes: str):
    """
    Return a FastAPI dependency that checks the API key has all *required_scopes*.

    Usage:
        @router.get("/public/v1/runs")
        async def list_runs(
            ctx: ApiKeyContext = Depends(get_api_key_context),
            _: None = Depends(require_scope("runs:read")),
        ): ...
    """

    async def _check(ctx: ApiKeyContext = Depends(get_api_key_context)) -> ApiKeyContext:
        missing = [s for s in required_scopes if s not in ctx.scopes]
        if missing:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"API key missing required scope(s): {', '.join(missing)}",
            )
        return ctx

    return _check
