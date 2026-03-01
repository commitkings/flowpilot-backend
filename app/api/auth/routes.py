"""
Google OAuth routes — login redirect, callback, /me, /logout.
"""

import logging
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import RedirectResponse

from app.api.auth.dependencies import get_current_user
from app.api.auth.jwt_utils import create_access_token
from src.config.settings import Settings
from src.infrastructure.database.connection import get_db_session
from src.infrastructure.database.repositories.user_repository import UserRepository

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"


@router.get("/google/login")
async def google_login():
    """Redirect user to Google consent screen."""
    if not Settings.is_google_oauth_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Google OAuth not configured",
        )

    params = {
        "client_id": Settings.GOOGLE_CLIENT_ID,
        "redirect_uri": Settings.GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope": "openid email profile",
        "access_type": "offline",
        "prompt": "consent",
    }
    return RedirectResponse(f"{GOOGLE_AUTH_URL}?{urlencode(params)}")


@router.get("/google/callback")
async def google_callback(
    code: str,
    session=Depends(get_db_session),
):
    """Exchange authorization code for tokens, upsert user, return JWT."""
    # Exchange code for Google access token
    async with httpx.AsyncClient() as client:
        token_resp = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "code": code,
                "client_id": Settings.GOOGLE_CLIENT_ID,
                "client_secret": Settings.GOOGLE_CLIENT_SECRET,
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
async def get_me(current_user=Depends(get_current_user)):
    """Return the authenticated user's profile and memberships."""
    repo_session = None  # user already loaded by dependency
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
    }
