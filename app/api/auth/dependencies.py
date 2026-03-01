"""
FastAPI dependency — extract and validate the current user from JWT.
"""

import uuid
from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.api.auth.jwt_utils import decode_access_token
from src.infrastructure.database.connection import get_db_session
from src.infrastructure.database.repositories.user_repository import UserRepository

_bearer = HTTPBearer(auto_error=False)


async def get_current_user_optional(
    creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
    session=Depends(get_db_session),
):
    """Returns the UserModel or None (for public endpoints)."""
    if creds is None:
        return None

    payload = decode_access_token(creds.credentials)
    if payload is None:
        return None

    user_id = uuid.UUID(payload["sub"])
    repo = UserRepository(session)
    return await repo.get_by_id(user_id)


async def get_current_user(
    creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
    session=Depends(get_db_session),
):
    """Returns the UserModel or raises 401."""
    if creds is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authorization header",
        )

    payload = decode_access_token(creds.credentials)
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        )

    user_id = uuid.UUID(payload["sub"])
    repo = UserRepository(session)
    user = await repo.get_by_id(user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
        )
    return user
