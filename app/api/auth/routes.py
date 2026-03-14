"""
Auth routes — Google OAuth, local email/password, and password reset flows.
"""

import logging
from typing import Optional
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from fastapi.responses import RedirectResponse
from pydantic import AliasChoices, BaseModel, Field

from app.api.auth.dependencies import get_current_user
from app.api.auth.jwt_utils import create_access_token
from app.api.auth.passwords import (
    build_password_reset_url,
    generate_password_reset_token,
    hash_password,
    hash_password_reset_token,
    normalize_email,
    password_reset_expires_at,
    validate_password,
    verify_password,
)
from src.config.settings import Settings
from src.infrastructure.database.connection import get_db_session
from src.infrastructure.database.repositories.password_reset_token_repository import (
    PasswordResetTokenRepository,
)
from src.infrastructure.database.repositories.user_repository import UserRepository


class RegisterRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=1, max_length=512)


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=1, max_length=512)


class UpdateProfileRequest(BaseModel):
    display_name: Optional[str] = None
    avatar_url: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    job_title: Optional[str] = None
    phone: Optional[str] = None
    timezone: Optional[str] = None
    department: Optional[str] = None


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=512)
    new_password: str = Field(min_length=1, max_length=512)


class ForgotPasswordRequest(BaseModel):
    email: str = Field(min_length=3, max_length=255)


class ResetPasswordRequest(BaseModel):
    token: str = Field(min_length=1, max_length=2048)
    new_password: str = Field(
        min_length=1,
        max_length=512,
        validation_alias=AliasChoices("new_password", "password", "newPassword"),
    )


class MessageResponse(BaseModel):
    message: str

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"
FORGOT_PASSWORD_RESPONSE = (
    "If an account exists for that email, a password reset link has been sent."
)

_INVALID_CREDENTIALS = "Invalid email or password"


def _user_response(user, memberships) -> dict:
    """Build the standard user profile dict."""
    return {
        "id": str(user.id),
        "email": user.email,
        "display_name": user.display_name,
        "avatar_url": user.avatar_url,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "job_title": user.job_title,
        "phone": user.phone,
        "timezone": user.timezone,
        "department": user.department,
        "is_active": user.is_active,
        "last_login_at": (
            user.last_login_at.isoformat() if user.last_login_at else None
        ),
        "memberships": [
            {"business_id": str(m.business_id), "role": m.role}
            for m in memberships
        ],
        "has_completed_onboarding": len(memberships) > 0,
    }


# ── Local email / password auth ───────────────────────────────


@router.post("/register", status_code=status.HTTP_201_CREATED)
async def register_with_password(
    body: RegisterRequest,
    session=Depends(get_db_session),
):
    """Create a new user with email + password."""
    repo = UserRepository(session)
    normalized = normalize_email(body.email)

    existing = await repo.get_by_email(normalized)
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with this email already exists",
        )

    password_hashed = hash_password(body.password)

    from src.infrastructure.database.flowpilot_models import UserModel

    new_user = UserModel(
        email=normalized,
        display_name=body.name.strip(),
        password_hash=password_hashed,
        is_active=True,
    )
    session.add(new_user)
    await session.flush()
    await session.refresh(new_user)

    token = create_access_token(new_user.id, new_user.email)
    await session.commit()

    return {
        "token": token,
        "user": {
            "id": str(new_user.id),
            "email": new_user.email,
            "display_name": new_user.display_name,
        },
    }


@router.post("/login")
async def login_with_password(
    body: LoginRequest,
    session=Depends(get_db_session),
):
    """Authenticate an existing user with email + password."""
    repo = UserRepository(session)
    normalized = normalize_email(body.email)

    user = await repo.get_by_email(normalized)
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=_INVALID_CREDENTIALS,
        )

    if not user.password_hash:
        # OAuth-only user — no local password set
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=_INVALID_CREDENTIALS,
        )

    if not verify_password(body.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=_INVALID_CREDENTIALS,
        )

    from datetime import datetime, timezone as tz

    user.last_login_at = datetime.now(tz.utc)
    await session.flush()

    token = create_access_token(user.id, user.email)
    await session.commit()

    return {
        "token": token,
        "user": {
            "id": str(user.id),
            "email": user.email,
            "display_name": user.display_name,
        },
    }


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
    return _user_response(current_user, memberships)


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
    payload = body.model_dump(exclude_none=True)
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="At least one field must be provided",
        )

    repo = UserRepository(session)
    updated = await repo.update_profile(current_user.id, **payload)
    memberships = await repo.get_memberships(current_user.id)
    return _user_response(updated, memberships)


@router.post("/me/password", response_model=MessageResponse)
async def change_password(
    body: ChangePasswordRequest,
    current_user=Depends(get_current_user),
    session=Depends(get_db_session),
):
    """Change the authenticated user's password."""
    if not current_user.password_hash:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Password change is not available for OAuth-only accounts",
        )

    if not verify_password(body.current_password, current_user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Current password is incorrect",
        )

    errors = validate_password(body.new_password)
    if errors:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=errors,
        )

    repo = UserRepository(session)
    pw_hash = hash_password(body.new_password)
    await repo.set_password(current_user.id, pw_hash)
    return {"message": "Password updated successfully"}


@router.post("/me/avatar")
async def upload_avatar(
    file: UploadFile = File(...),
    current_user=Depends(get_current_user),
    session=Depends(get_db_session),
):
    """Upload an avatar image for the authenticated user."""
    import shutil, os, uuid as _uuid

    allowed = {"image/jpeg", "image/png", "image/gif", "image/webp"}
    if file.content_type not in allowed:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unsupported image type")

    upload_dir = os.path.join(os.getcwd(), "uploads", "avatars")
    os.makedirs(upload_dir, exist_ok=True)

    ext = file.filename.rsplit(".", 1)[-1] if "." in file.filename else "png"
    filename = f"{_uuid.uuid4().hex}.{ext}"
    filepath = os.path.join(upload_dir, filename)

    with open(filepath, "wb") as buf:
        shutil.copyfileobj(file.file, buf)

    avatar_url = f"/uploads/avatars/{filename}"
    repo = UserRepository(session)
    await repo.update_profile(current_user.id, avatar_url=avatar_url)
    return {"avatar_url": avatar_url}


@router.delete("/me/avatar", response_model=MessageResponse)
async def remove_avatar(
    current_user=Depends(get_current_user),
    session=Depends(get_db_session),
):
    """Remove the authenticated user's avatar."""
    import os

    if current_user.avatar_url and current_user.avatar_url.startswith("/uploads/"):
        filepath = os.path.join(os.getcwd(), current_user.avatar_url.lstrip("/"))
        if os.path.isfile(filepath):
            os.remove(filepath)

    repo = UserRepository(session)
    await repo.update_profile(current_user.id, avatar_url="")
    return {"message": "Avatar removed"}


@router.get("/connections")
async def get_connections(
    current_user=Depends(get_current_user),
):
    """Return linked authentication providers for the user."""
    google_connected = (
        current_user.external_provider == "google" and current_user.external_id is not None
    )
    return {
        "connections": [
            {
                "provider": "google",
                "connected": google_connected,
                "email": current_user.email if google_connected else None,
            },
        ]
    }


@router.post(
    "/forgot-password",
    response_model=MessageResponse,
    status_code=status.HTTP_200_OK,
)
async def forgot_password(
    body: ForgotPasswordRequest,
    session=Depends(get_db_session),
):
    if Settings.is_production():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Password reset delivery is not configured",
        )

    normalized_email = normalize_email(body.email)
    user_repo = UserRepository(session)
    token_repo = PasswordResetTokenRepository(session)

    user = await user_repo.get_by_email(normalized_email)
    if user is None or not user.is_active:
        return MessageResponse(message=FORGOT_PASSWORD_RESPONSE)

    await token_repo.revoke_active_tokens_for_user(user.id)

    raw_token = generate_password_reset_token()
    token_hash = hash_password_reset_token(raw_token)
    await token_repo.create(
        user_id=user.id,
        token_hash=token_hash,
        expires_at=password_reset_expires_at(),
    )

    reset_url = build_password_reset_url(raw_token)
    logger.info(
        "Generated password reset link for user_id=%s email=%s reset_url=%s",
        user.id,
        normalized_email,
        reset_url,
    )

    return MessageResponse(message=FORGOT_PASSWORD_RESPONSE)


@router.post(
    "/reset-password",
    response_model=MessageResponse,
    status_code=status.HTTP_200_OK,
)
async def reset_password(
    body: ResetPasswordRequest,
    session=Depends(get_db_session),
):
    token_repo = PasswordResetTokenRepository(session)
    user_repo = UserRepository(session)

    token_hash = hash_password_reset_token(body.token)
    token_record = await token_repo.get_active_by_token_hash(token_hash)
    if token_record is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired password reset token",
        )

    updated_user = await user_repo.set_password(
        token_record.user_id,
        hash_password(body.new_password),
    )
    if updated_user is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Password reset token references a missing user",
        )

    await token_repo.mark_used(token_record)
    await token_repo.revoke_active_tokens_for_user(
        token_record.user_id,
        exclude_token_id=token_record.id,
    )

    return MessageResponse(message="Password has been reset successfully.")
