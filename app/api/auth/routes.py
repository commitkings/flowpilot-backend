"""
Google OAuth routes — login redirect, callback, /me, /logout, profile update.
"""

import logging
from typing import Optional
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from app.api.auth.dependencies import get_current_user
from app.api.auth.jwt_utils import create_access_token
from src.config.settings import Settings
from src.infrastructure.database.connection import get_db_session
from src.infrastructure.database.repositories.user_repository import UserRepository


class UpdateProfileRequest(BaseModel):
    display_name: Optional[str] = None
    avatar_url: Optional[str] = None

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"


@router.get("/google/login")
async def google_login(raw_tokens: bool = False):
    """Redirect user to Google consent screen."""
    if not Settings.is_google_oauth_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Google OAuth not configured",
        )
    google_client_id = Settings.get_google_client_id()

    params = {
        "client_id": google_client_id,
        "redirect_uri": Settings.GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope": "openid email profile",
        "access_type": "offline",
        "prompt": "consent",
        "state": "raw_tokens" if raw_tokens else "default",
    }
    return RedirectResponse(f"{GOOGLE_AUTH_URL}?{urlencode(params)}")


@router.get("/google/callback")
async def google_callback(
    code: str,
    state: Optional[str] = None,
    raw_tokens: bool = False,
    session=Depends(get_db_session),
):
    """Exchange authorization code for tokens, upsert user, return JWT."""
    google_client_id = Settings.get_google_client_id()
    google_client_secret = Settings.get_google_client_secret()
    if not google_client_id or not google_client_secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Google OAuth not configured",
        )

    # Exchange code for Google access token
    async with httpx.AsyncClient() as client:
        token_resp = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "code": code,
                "client_id": google_client_id,
                "client_secret": google_client_secret,
                "redirect_uri": Settings.GOOGLE_REDIRECT_URI,
                "grant_type": "authorization_code",
            },
        )

    if token_resp.status_code != 200:
        logger.error("Google token exchange failed: %s", token_resp.text)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Failed to authenticate with Google",
        )

    google_tokens = token_resp.json()
    should_return_raw_tokens = raw_tokens or state == "raw_tokens"
    if should_return_raw_tokens and not Settings.is_production():
        return google_tokens
    access_token = google_tokens["access_token"]

    # Fetch user profile from Google
    async with httpx.AsyncClient() as client:
        userinfo_resp = await client.get(
            GOOGLE_USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )

    if userinfo_resp.status_code != 200:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Failed to fetch Google user info",
        )

    google_user = userinfo_resp.json()

    # Upsert user in local DB
    repo = UserRepository(session)
    user = await repo.upsert_from_oauth(
        external_id=f"google:{google_user['id']}",
        email=google_user["email"],
        display_name=google_user.get("name", google_user["email"]),
        avatar_url=google_user.get("picture"),
    )

    # Issue JWT
    jwt_token = create_access_token(user.id, user.email)

    # Redirect to frontend with token
    redirect_url = f"{Settings.FRONTEND_URL}/auth/callback?token={jwt_token}"
    return RedirectResponse(redirect_url)


@router.get("/me")
async def get_me(
    current_user=Depends(get_current_user),
    session=Depends(get_db_session),
):
    """Return the authenticated user's profile, memberships, and onboarding status."""
    repo = UserRepository(session)
    memberships = await repo.get_memberships(current_user.id)

    return {
        "id": str(current_user.id),
        "email": current_user.email,
        "display_name": current_user.display_name,
        "avatar_url": current_user.avatar_url,
        "is_active": current_user.is_active,
        "last_login_at": (
            current_user.last_login_at.isoformat()
            if current_user.last_login_at
            else None
        ),
        "memberships": [
            {
                "business_id": str(m.business_id),
                "role": m.role,
            }
            for m in memberships
        ],
        "has_completed_onboarding": len(memberships) > 0,
    }


@router.post("/logout", status_code=status.HTTP_200_OK)
async def logout(
    current_user=Depends(get_current_user),
    session=Depends(get_db_session),
):
    """Stateless logout — clears last_login_at. Frontend discards the JWT."""
    repo = UserRepository(session)
    await repo.clear_last_login(current_user.id)
    return {"message": "Logged out"}


@router.patch("/me")
async def update_me(
    body: UpdateProfileRequest,
    current_user=Depends(get_current_user),
    session=Depends(get_db_session),
):
    """Update the authenticated user's mutable profile fields."""
    if body.display_name is None and body.avatar_url is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="At least one field (display_name, avatar_url) must be provided",
        )

    repo = UserRepository(session)
    updated = await repo.update_profile(
        current_user.id,
        display_name=body.display_name,
        avatar_url=body.avatar_url,
    )

    memberships = await repo.get_memberships(current_user.id)

    return {
        "id": str(updated.id),
        "email": updated.email,
        "display_name": updated.display_name,
        "avatar_url": updated.avatar_url,
        "is_active": updated.is_active,
        "last_login_at": (
            updated.last_login_at.isoformat()
            if updated.last_login_at
            else None
        ),
        "memberships": [
            {
                "business_id": str(m.business_id),
                "role": m.role,
            }
            for m in memberships
        ],
        "has_completed_onboarding": len(memberships) > 0,
    }
