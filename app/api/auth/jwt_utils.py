"""
JWT token utilities for FlowPilot auth.
"""

import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt

from src.config.settings import Settings


def create_access_token(user_id: uuid.UUID, email: str) -> str:
    payload = {
        "sub": str(user_id),
        "email": email,
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
