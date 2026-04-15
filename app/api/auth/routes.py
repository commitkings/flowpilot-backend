"""
Auth routes — Google OAuth, local email/password, and password reset flows.
"""

import logging
from typing import Optional
from urllib.parse import urlencode

import httpx
import pyotp
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
    verify_password,
)
from src.config.settings import Settings
from src.infrastructure.database.connection import get_db_session
from src.infrastructure.database.repositories.invitation_repository import (
    InvitationRepository,
)
from src.infrastructure.database.repositories.notification_repository import (
    NotificationRepository,
)
from src.infrastructure.database.repositories.user_repository import UserRepository
from src.services.email_service import send_password_reset_email, send_verification_email
from src.infrastructure.cache import otp_store
from src.infrastructure.cache import password_reset_store


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
    has_taken_tour: Optional[bool] = None


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=512)
    new_password: str = Field(min_length=1, max_length=512)
    totp_code: Optional[str] = None


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
    # Only expose active memberships — disabled members lose dashboard access
    active_memberships = [
        m for m in memberships if getattr(m, "is_active", True)
    ]
    from src.infrastructure.storage import s3_client as _s3
    return {
        "id": str(user.id),
        "email": user.email,
        "display_name": user.display_name,
        "avatar_url": _s3.make_file_url(user.avatar_url),
        "first_name": user.first_name,
        "last_name": user.last_name,
        "job_title": user.job_title,
        "phone": user.phone,
        "timezone": user.timezone,
        "department": user.department,
        "is_active": user.is_active,
        "has_taken_tour": getattr(user, "has_taken_tour", False),
        "email_verified": user.email_verified_at is not None,
        "last_login_at": (
            user.last_login_at.isoformat() if user.last_login_at else None
        ),
        "memberships": [
            {"business_id": str(m.business_id), "role": m.role}
            for m in active_memberships
        ],
        "has_completed_onboarding": len(active_memberships) > 0,
        # 2FA fields
        "totp_enabled": getattr(user, "totp_enabled_at", None) is not None,
        "totp_grace_until": (
            user.totp_grace_until.isoformat()
            if getattr(user, "totp_grace_until", None)
            else None
        ),
    }


# ── Local email / password auth ───────────────────────────────


@router.post("/register", status_code=status.HTTP_201_CREATED)
async def register_with_password(
    body: RegisterRequest,
    session=Depends(get_db_session),
):
    """Create a new user with email + password.

    OTP is stored in Redis (15-minute TTL) — no token columns in the DB.
    After creating the user, auto-accepts any pending invitations matching
    the email address, then sends the verification code.
    """
    from datetime import datetime, timezone as tz

    from src.infrastructure.database.flowpilot_models import (
        BusinessMemberModel,
        UserModel,
    )

    repo = UserRepository(session)
    normalized = normalize_email(body.email)

    existing = await repo.get_by_email(normalized)
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with this email already exists",
        )

    new_user = UserModel(
        email=normalized,
        display_name=body.name.strip(),
        password_hash=hash_password(body.password),
        is_active=True,
    )
    session.add(new_user)
    await session.flush()
    await session.refresh(new_user)

    # Auto-accept any pending invites for this email
    invite_repo = InvitationRepository(session)
    pending = await invite_repo.get_pending_by_email(normalized)
    for invite in pending:
        session.add(BusinessMemberModel(
            business_id=invite.business_id,
            user_id=new_user.id,
            role=invite.role,
            joined_at=datetime.now(tz.utc),
        ))
        await invite_repo.mark_accepted(invite)

    token = create_access_token(new_user.id, new_user.email)
    await session.commit()

    # Generate OTP → Redis (best-effort; code still generated even if Redis is down)
    code = otp_store.generate_code()
    await otp_store.save(str(new_user.id), code)

    await send_verification_email(
        to=normalized,
        code=code,
        display_name=body.name.strip(),
        frontend_url=Settings.FRONTEND_URL,
    )

    return {
        "token": token,
        "user": {
            "id": str(new_user.id),
            "email": new_user.email,
            "display_name": new_user.display_name,
        },
    }


class VerifyEmailRequest(BaseModel):
    code: str = Field(min_length=6, max_length=6)


@router.post("/verify-email", response_model=MessageResponse)
async def verify_email(
    body: VerifyEmailRequest,
    current_user=Depends(get_current_user),
    session=Depends(get_db_session),
):
    """Verify the user's email with the 6-digit OTP (stored in Redis)."""
    from datetime import datetime, timezone as tz

    if current_user.email_verified_at is not None:
        return MessageResponse(message="Email already verified")

    matched = await otp_store.verify(str(current_user.id), body.code)
    if not matched:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired verification code",
        )

    current_user.email_verified_at = datetime.now(tz.utc)
    await session.commit()

    return MessageResponse(message="Email verified successfully")


@router.post("/resend-verification", response_model=MessageResponse)
async def resend_verification(
    current_user=Depends(get_current_user),
):
    """Issue a fresh OTP (stored in Redis) and re-send the verification email."""
    if current_user.email_verified_at is not None:
        return MessageResponse(message="Email already verified")

    code = otp_store.generate_code()
    await otp_store.save(str(current_user.id), code)

    await send_verification_email(
        to=current_user.email,
        code=code,
        display_name=current_user.display_name,
        frontend_url=Settings.FRONTEND_URL,
    )

    return MessageResponse(message="Verification code sent")


class RegisterViaInviteRequest(BaseModel):
    token: str = Field(min_length=1, max_length=512)
    first_name: str = Field(min_length=1, max_length=100)
    last_name: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=512)


@router.post("/register-via-invite", status_code=status.HTTP_201_CREATED)
async def register_via_invite(
    body: RegisterViaInviteRequest,
    session=Depends(get_db_session),
):
    """Create an account and join the invited organisation in one shot.

    Only works with a valid, pending invitation token. The email address
    is taken directly from the invitation — the registrant cannot change it.
    """
    from datetime import datetime, timezone as tz

    from src.infrastructure.database.flowpilot_models import (
        BusinessMemberModel,
        UserModel,
    )

    invite_repo = InvitationRepository(session)
    invite = await invite_repo.get_by_token(body.token)

    if not invite:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Invitation not found",
        )

    if invite.status != "pending" or invite.expires_at < datetime.now(tz.utc):
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="Invitation has expired or already been used",
        )

    user_repo = UserRepository(session)
    existing = await user_repo.get_by_email(invite.invited_email)
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with this email already exists. Please log in instead.",
        )

    display_name = f"{body.first_name.strip()} {body.last_name.strip()}"
    password_hashed = hash_password(body.password)

    new_user = UserModel(
        email=invite.invited_email,
        display_name=display_name,
        first_name=body.first_name.strip(),
        last_name=body.last_name.strip(),
        password_hash=password_hashed,
        is_active=True,
        # Invitation proves email ownership — mark as verified immediately
        email_verified_at=datetime.now(tz.utc),
    )
    session.add(new_user)
    await session.flush()
    await session.refresh(new_user)

    member = BusinessMemberModel(
        business_id=invite.business_id,
        user_id=new_user.id,
        role=invite.role,
        joined_at=datetime.now(tz.utc),
    )
    session.add(member)
    await invite_repo.mark_accepted(invite)

    # Welcome notification for the new team member
    from src.infrastructure.database.flowpilot_models import BusinessModel
    from sqlalchemy import select as sa_select
    biz_result = await session.execute(
        sa_select(BusinessModel).where(BusinessModel.id == invite.business_id)
    )
    biz = biz_result.scalar_one_or_none()
    biz_name = biz.business_name if biz else "your team"
    notif_repo = NotificationRepository(session)
    await notif_repo.create(
        user_id=new_user.id,
        business_id=invite.business_id,
        title="You've joined a workspace!",
        message=f"Welcome to {biz_name}. You can now access the dashboard and collaborate with your team.",
        type="info",
        resource_type="business",
        resource_id=str(invite.business_id),
    )

    # Notify the business owner that a new member has joined
    from sqlalchemy import select as _select
    try:
        owner_result = await session.execute(
            _select(BusinessMemberModel).where(
                BusinessMemberModel.business_id == invite.business_id,
                BusinessMemberModel.role == "owner",
                BusinessMemberModel.is_active.is_(True),
            ).limit(1)
        )
        owner_member = owner_result.scalars().first()
        if owner_member:
            await notif_repo.create(
                user_id=owner_member.user_id,
                business_id=invite.business_id,
                title="New team member joined",
                message=f"{display_name} has joined {biz_name} as {invite.role.capitalize()}.",
                type="info",
                resource_type="team",
                resource_id=str(invite.business_id),
            )
    except Exception as _exc:
        logger.warning("Failed to notify owner of new team member: %s", _exc)

    # Check if org requires 2FA for all members.
    # New members joining after enforcement must set up 2FA immediately — no grace period.
    # (Grace periods are only for existing members who were already in the org when enforcement was toggled on.)
    from src.infrastructure.database.flowpilot_models import BusinessConfigModel
    config_result = await session.execute(
        _select(BusinessConfigModel).where(
            BusinessConfigModel.business_id == invite.business_id
        )
    )
    biz_config = config_result.scalars().first()
    requires_2fa_setup = bool(biz_config and biz_config.require_2fa)

    token = create_access_token(new_user.id, new_user.email)
    await session.commit()

    return {
        "token": token,
        "user": {
            "id": str(new_user.id),
            "email": new_user.email,
            "display_name": new_user.display_name,
        },
        "requires_2fa_setup": requires_2fa_setup,
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

    # If the user hasn't verified their email yet, issue a fresh OTP and
    # resend the verification email so they don't have to wait out the
    # resend cooldown on the frontend.
    if user.email_verified_at is None:
        code = otp_store.generate_code()
        await otp_store.save(str(user.id), code)
        await send_verification_email(
            to=user.email,
            code=code,
            display_name=user.display_name,
            frontend_url=Settings.FRONTEND_URL,
        )

    await session.commit()

    # If 2FA is enabled, return a short-lived mfa_token instead of the real JWT.
    # The client must call POST /auth/2fa/verify (or /backup-login) to get the token.
    if user.totp_enabled_at is not None:
        from src.infrastructure.cache import totp_challenge_store
        from app.api.auth.jwt_utils import create_mfa_token
        challenge_id = await totp_challenge_store.create(str(user.id))
        mfa_token = create_mfa_token(user.id, challenge_id)
        return {
            "mfa_required": True,
            "mfa_token": mfa_token,
        }

    # Check if any org this user belongs to requires 2FA but the user hasn't set it up.
    requires_2fa_setup = False
    from sqlalchemy import select as _select
    from src.infrastructure.database.flowpilot_models import BusinessMemberModel, BusinessConfigModel
    config_q = await session.execute(
        _select(BusinessConfigModel)
        .join(BusinessMemberModel, BusinessMemberModel.business_id == BusinessConfigModel.business_id)
        .where(
            BusinessMemberModel.user_id == user.id,
            BusinessMemberModel.is_active == True,  # noqa: E712
            BusinessConfigModel.require_2fa == True,  # noqa: E712
        )
        .limit(1)
    )
    requires_2fa_setup = config_q.scalars().first() is not None

    token = create_access_token(user.id, user.email)
    return {
        "token": token,
        "user": {
            "id": str(user.id),
            "email": user.email,
            "display_name": user.display_name,
        },
        "requires_2fa_setup": requires_2fa_setup,
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

    _error_redirect = f"{Settings.FRONTEND_URL}/auth/callback?error=google_unavailable"

    # Exchange code for Google access token
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
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
    except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError) as exc:
        logger.error("Google token exchange network error: %s", exc)
        return RedirectResponse(_error_redirect)

    if token_resp.status_code != 200:
        logger.error("Google token exchange failed: %s", token_resp.text)
        return RedirectResponse(_error_redirect)

    google_tokens = token_resp.json()
    should_return_raw_tokens = raw_tokens or state == "raw_tokens"
    if should_return_raw_tokens and not Settings.is_production():
        return google_tokens
    access_token = google_tokens.get("access_token")
    if not access_token:
        logger.error("Google token response missing access_token: %s", google_tokens)
        return RedirectResponse(_error_redirect)

    # Fetch user profile from Google
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            userinfo_resp = await client.get(
                GOOGLE_USERINFO_URL,
                headers={"Authorization": f"Bearer {access_token}"},
            )
    except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError) as exc:
        logger.error("Google userinfo network error: %s", exc)
        return RedirectResponse(_error_redirect)

    if userinfo_resp.status_code != 200:
        logger.error("Google userinfo fetch failed: %s", userinfo_resp.text)
        return RedirectResponse(_error_redirect)

    google_user = userinfo_resp.json()
    google_email = google_user.get("email")
    if not google_email:
        logger.error("Google userinfo missing email field: %s", google_user)
        return RedirectResponse(_error_redirect)

    # Upsert user in local DB — Google already verifies the email.
    # If an email/password account already exists for this email, Google is
    # linked to it so the user can sign in either way going forward.
    from datetime import datetime, timezone as _tz
    repo = UserRepository(session)
    user = await repo.upsert_from_oauth(
        external_id=f"google:{google_user['id']}",
        email=google_email,
        display_name=google_user.get("name", google_email),
        avatar_url=google_user.get("picture"),
        email_verified_at=datetime.now(_tz.utc),
    )

    # Reject disabled accounts
    if not user.is_active:
        return RedirectResponse(f"{Settings.FRONTEND_URL}/auth/callback?error=account_disabled")

    # If 2FA is enabled, redirect to the MFA challenge page instead
    if user.totp_enabled_at is not None:
        from src.infrastructure.cache import totp_challenge_store
        from app.api.auth.jwt_utils import create_mfa_token
        challenge_id = await totp_challenge_store.create(str(user.id))
        mfa_token = create_mfa_token(user.id, challenge_id)
        redirect_url = f"{Settings.FRONTEND_URL}/verify-2fa?mfa_token={mfa_token}"
        return RedirectResponse(redirect_url)

    # Check if any org requires 2FA and user hasn't set it up
    from sqlalchemy import select as _select_g
    from src.infrastructure.database.flowpilot_models import BusinessMemberModel as _BMM, BusinessConfigModel as _BCM
    _cfg_q = await session.execute(
        _select_g(_BCM)
        .join(_BMM, _BMM.business_id == _BCM.business_id)
        .where(
            _BMM.user_id == user.id,
            _BMM.is_active == True,  # noqa: E712
            _BCM.require_2fa == True,  # noqa: E712
        )
        .limit(1)
    )
    requires_2fa_setup = _cfg_q.scalars().first() is not None

    # Issue JWT and redirect to frontend
    jwt_token = create_access_token(user.id, user.email)
    redirect_url = f"{Settings.FRONTEND_URL}/auth/callback?token={jwt_token}"
    if requires_2fa_setup:
        redirect_url += "&requires_2fa_setup=true"
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
    await session.commit()
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

    # If the user has 2FA enabled, require a valid TOTP code
    if getattr(current_user, "totp_enabled_at", None) is not None:
        if not body.totp_code:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Two-factor authentication code required",
            )
        totp = pyotp.TOTP(current_user.totp_secret)
        if not totp.verify(body.totp_code, valid_window=1):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid two-factor authentication code",
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
    await session.commit()
    return {"message": "Password updated successfully"}


@router.post("/me/avatar")
async def upload_avatar(
    file: UploadFile = File(...),
    current_user=Depends(get_current_user),
    session=Depends(get_db_session),
):
    """Upload an avatar image for the authenticated user.

    Validates magic bytes (not just MIME type), enforces 3 MB limit,
    and stores in MinIO. Falls back to local disk if MinIO is unavailable.
    """
    from src.infrastructure.storage import s3_client as _s3

    content = await file.read()

    # Magic-byte validation (prevents disguised malware)
    error = _s3.validate_image(content, max_bytes=3 * 1024 * 1024)
    if error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=error)

    # Try MinIO first
    object_key = await _s3.upload_file(content, file.filename or "avatar.jpg", folder="avatars", content_type=file.content_type)

    if object_key:
        # Store the object key — serve via /api/v1/files/{key} proxy
        avatar_url = object_key
    else:
        # MinIO unavailable — fall back to local disk
        import shutil as _shutil, os, uuid as _uuid, io as _io
        upload_dir = os.path.join(os.getcwd(), "uploads", "avatars")
        os.makedirs(upload_dir, exist_ok=True)
        ext = (file.filename or "avatar.jpg").rsplit(".", 1)[-1]
        filename = f"{_uuid.uuid4().hex}.{ext}"
        filepath = os.path.join(upload_dir, filename)
        with open(filepath, "wb") as buf:
            buf.write(content)
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
    """Issue a password-reset link. Token is stored in Redis with TTL — no DB write."""
    normalized_email = normalize_email(body.email)
    user_repo = UserRepository(session)

    user = await user_repo.get_by_email(normalized_email)
    if user is None or not user.is_active:
        # Generic message prevents email enumeration
        return MessageResponse(message=FORGOT_PASSWORD_RESPONSE)

    raw_token = generate_password_reset_token()
    token_hash = hash_password_reset_token(raw_token)

    # Saves to Redis, invalidates any previous reset token for this user
    await password_reset_store.save(str(user.id), token_hash)

    reset_url = build_password_reset_url(raw_token)
    sent = await send_password_reset_email(
        to=normalized_email,
        reset_url=reset_url,
        frontend_url=Settings.FRONTEND_URL,
    )
    if not sent:
        logger.warning(
            "Password reset email could not be delivered — reset_url=%s user_id=%s",
            reset_url,
            user.id,
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
    """Consume the Redis reset token and update the user's password."""
    import uuid

    token_hash = hash_password_reset_token(body.token)
    user_id_str = await password_reset_store.get_user_id(token_hash)

    if not user_id_str:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired password reset token",
        )

    user_repo = UserRepository(session)
    updated_user = await user_repo.set_password(
        uuid.UUID(user_id_str),
        hash_password(body.new_password),
    )
    if updated_user is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Password reset token references a missing user",
        )

    await session.commit()

    # Token is single-use — delete it from Redis
    await password_reset_store.consume(user_id_str, token_hash)

    return MessageResponse(message="Password has been reset successfully.")
