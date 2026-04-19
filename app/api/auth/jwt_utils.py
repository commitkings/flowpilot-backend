"""
JWT token utilities for FlowPilot auth.
"""

import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt

from src.config.settings import Settings


def create_access_token(user_id: uuid.UUID, email: str, security_version: int = 0) -> str:
    payload = {
        "sub": str(user_id),
        "email": email,
        "sv": security_version,
        "iat": datetime.now(timezone.utc),
        "exp": datetime.now(timezone.utc)
        + timedelta(hours=Settings.JWT_EXPIRY_HOURS),
    }
    return jwt.encode(payload, Settings.JWT_SECRET, algorithm=Settings.JWT_ALGORITHM)


def decode_access_token(token: str) -> Optional[dict]:
    try:
        return jwt.decode(
            token, Settings.JWT_SECRET, algorithms=[Settings.JWT_ALGORITHM]
        )
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None


# ── MFA challenge tokens (short-lived, scoped) ────────────────────────────────

_MFA_TOKEN_TTL_MINUTES = 5


def create_mfa_token(user_id: uuid.UUID, challenge_id: str) -> str:
    """Issue a 5-minute token that allows *only* the 2FA verify endpoint."""
    payload = {
        "sub": str(user_id),
        "scope": "mfa_challenge",
        "challenge_id": challenge_id,
        "iat": datetime.now(timezone.utc),
        "exp": datetime.now(timezone.utc) + timedelta(minutes=_MFA_TOKEN_TTL_MINUTES),
    }
    return jwt.encode(payload, Settings.JWT_SECRET, algorithm=Settings.JWT_ALGORITHM)


def decode_mfa_token(token: str) -> Optional[dict]:
    try:
        payload = jwt.decode(
            token, Settings.JWT_SECRET, algorithms=[Settings.JWT_ALGORITHM]
        )
        if payload.get("scope") != "mfa_challenge":
            return None
        return payload
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None
