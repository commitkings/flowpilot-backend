"""Two-factor authentication (TOTP) endpoints.

Setup flow  :  POST /auth/2fa/setup  →  POST /auth/2fa/enable
Login flow  :  Password OK → mfa_token  →  POST /auth/2fa/verify (or /backup)
Management  :  POST /auth/2fa/disable
               POST /auth/2fa/backup-codes/regenerate
               GET  /auth/2fa/status
Org control :  PATCH /auth/2fa/org/require  (owner only)
"""

from __future__ import annotations

import base64
import io
import json
import logging
import secrets
import uuid
from datetime import datetime, timedelta, timezone as _tz

import pyotp
import qrcode
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from src.infrastructure.cache.rate_limiter import is_allowed as _rate_ok

_TOO_MANY_2FA = HTTPException(status_code=429, detail="Too many attempts. Please wait and try again.")
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.auth.dependencies import get_current_user
from app.api.auth.jwt_utils import create_access_token, create_mfa_token, decode_mfa_token
from app.api.auth.passwords import hash_password, verify_password
from src.config.settings import Settings
from src.infrastructure.cache import totp_challenge_store
from src.infrastructure.database.connection import get_db_session
from src.infrastructure.database.flowpilot_models import (
    BusinessConfigModel,
    BusinessMemberModel,
    UserModel,
)
from src.infrastructure.database.repositories.notification_repository import (
    NotificationRepository,
)
from src.infrastructure.database.repositories.user_repository import UserRepository
from src.services.email_service import (
    send_2fa_disabled_email,
    send_2fa_enabled_email,
    send_2fa_enforced_email,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/2fa", tags=["2fa"])

_BACKUP_CODE_COUNT = 8
_GRACE_HOURS = 24
APP_NAME = "FlowPilot"


# ── helpers ───────────────────────────────────────────────────────────────────


def _generate_backup_codes() -> tuple[list[str], list[str]]:
    """Return (plain_codes, hashed_codes).  Plain shown once; hashes stored."""
    plain: list[str] = []
    hashed: list[str] = []
    for _ in range(_BACKUP_CODE_COUNT):
        code = secrets.token_hex(5).upper()  # e.g. "A3F7B2C19D"
        plain.append(code)
        hashed.append(hash_password(code))
    return plain, hashed


def _verify_backup_code(plain: str, hashed_list: list[str]) -> int | None:
    """Return index of matching backup code hash, or None if no match."""
    for i, h in enumerate(hashed_list):
        if verify_password(plain.upper(), h):
            return i
    return None


def _totp_uri(secret: str, email: str) -> str:
    return pyotp.totp.TOTP(secret).provisioning_uri(name=email, issuer_name=APP_NAME)


def _qr_base64(uri: str) -> str:
    img = qrcode.make(uri)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


# ── request / response schemas ────────────────────────────────────────────────


class EnableRequest(BaseModel):
    code: str  # First TOTP code to confirm the app is wired up


class DisableRequest(BaseModel):
    password: str


class VerifyMfaRequest(BaseModel):
    mfa_token: str
    code: str  # TOTP code


class BackupLoginRequest(BaseModel):
    mfa_token: str
    backup_code: str


class RequireTwoFARequest(BaseModel):
    require: bool


# ── routes ────────────────────────────────────────────────────────────────────


@router.get("/status")
async def get_2fa_status(current_user=Depends(get_current_user)):
    """Return the caller's 2FA state and org enforcement flag."""
    return {
        "totp_enabled": current_user.totp_enabled_at is not None,
        "totp_enabled_at": (
            current_user.totp_enabled_at.isoformat()
            if current_user.totp_enabled_at
            else None
        ),
        "grace_until": (
            current_user.totp_grace_until.isoformat()
            if current_user.totp_grace_until
            else None
        ),
        "backup_codes_remaining": (
            len(json.loads(current_user.backup_codes_hash))
            if current_user.backup_codes_hash
            else 0
        ),
    }


@router.post("/setup")
async def setup_2fa(
    current_user=Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    """Generate a TOTP secret and return the QR code.

    Does NOT enable 2FA yet — the client must call /enable with a valid code.
    """
    secret = pyotp.random_base32()

    # Persist the (not-yet-active) secret so /enable can verify it
    current_user.totp_secret = secret
    await session.commit()

    uri = _totp_uri(secret, current_user.email)
    return {
        "secret": secret,
        "qr_code": _qr_base64(uri),
        "uri": uri,
    }


@router.post("/enable")
async def enable_2fa(
    body: EnableRequest,
    current_user=Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    """Verify the first TOTP code and activate 2FA.

    Returns 8 one-time backup codes — shown *once*, never again.
    """
    if not current_user.totp_secret:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Call /2fa/setup first to obtain a secret.",
        )
    if current_user.totp_enabled_at is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="2FA is already enabled.",
        )

    totp = pyotp.TOTP(current_user.totp_secret)
    if not totp.verify(body.code, valid_window=1):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid authenticator code.",
        )

    plain_codes, hashed_codes = _generate_backup_codes()
    current_user.totp_enabled_at = datetime.now(_tz.utc)
    current_user.backup_codes_hash = json.dumps(hashed_codes)

    # Clear the grace period if org enforcement was pending for this user
    current_user.totp_grace_until = None

    await session.commit()

    # In-app notification
    _notif_repo = NotificationRepository(session)
    await _notif_repo.create(
        user_id=current_user.id,
        title="Two-factor authentication enabled",
        message="Your account is now protected with 2FA. Keep your backup codes somewhere safe.",
        type="success",
        resource_type="security",
    )
    await session.commit()

    # Security notification email
    await send_2fa_enabled_email(
        to=current_user.email,
        display_name=current_user.display_name,
        frontend_url=Settings.FRONTEND_URL,
    )

    return {"backup_codes": plain_codes}


@router.post("/disable")
async def disable_2fa(
    body: DisableRequest,
    current_user=Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    """Disable 2FA.  Requires both the account password and current TOTP code."""
    if current_user.totp_enabled_at is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="2FA is not enabled.",
        )

    # Verify password
    if not current_user.password_hash or not verify_password(
        body.password, current_user.password_hash
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect password.",
        )

    current_user.totp_secret = None
    current_user.totp_enabled_at = None
    current_user.backup_codes_hash = None
    await session.commit()

    # In-app notification
    _notif_repo = NotificationRepository(session)
    await _notif_repo.create(
        user_id=current_user.id,
        title="Two-factor authentication disabled",
        message="2FA has been turned off. Re-enable it in Settings → Security to keep your account protected.",
        type="warning",
        resource_type="security",
    )
    await session.commit()

    await send_2fa_disabled_email(
        to=current_user.email,
        display_name=current_user.display_name,
        frontend_url=Settings.FRONTEND_URL,
    )

    return {"message": "Two-factor authentication disabled."}


@router.post("/verify")
async def verify_mfa(
    body: VerifyMfaRequest,
    session: AsyncSession = Depends(get_db_session),
):
    """Exchange a valid mfa_token + TOTP code for a full access token."""
    payload = decode_mfa_token(body.mfa_token)
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="MFA token is invalid or expired.",
        )

    user_id = uuid.UUID(payload["sub"])
    challenge_id = payload["challenge_id"]

    # Consume the challenge (prevents replay)
    if not await totp_challenge_store.consume(str(user_id), challenge_id):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="MFA challenge has already been used or expired.",
        )

    repo = UserRepository(session)
    user = await repo.get_by_id(user_id)
    if not user or not user.is_active or not user.totp_secret:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid MFA session.",
        )

    totp = pyotp.TOTP(user.totp_secret)
    if not totp.verify(body.code, valid_window=1):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid authenticator code.",
        )

    token = create_access_token(user.id, user.email)
    memberships = await repo.get_memberships(user.id)

    from app.api.auth.routes import _user_response
    return {"token": token, "user": _user_response(user, memberships)}


@router.post("/backup-login")
async def backup_code_login(
    request: Request,
    body: BackupLoginRequest,
    session: AsyncSession = Depends(get_db_session),
):
    """Exchange an mfa_token + backup code for a full access token.

    The used backup code is removed from the stored list (single-use).
    """
    ip = request.client.host if request.client else "unknown"
    if not await _rate_ok(f"2fa-backup:{ip}", limit=5, window_seconds=900):  # 5 per 15 min
        raise _TOO_MANY_2FA
    payload = decode_mfa_token(body.mfa_token)
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="MFA token is invalid or expired.",
        )

    user_id = uuid.UUID(payload["sub"])
    challenge_id = payload["challenge_id"]

    if not await totp_challenge_store.consume(str(user_id), challenge_id):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="MFA challenge has already been used or expired.",
        )

    repo = UserRepository(session)
    user = await repo.get_by_id(user_id)
    if not user or not user.is_active or not user.backup_codes_hash:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid MFA session.",
        )

    hashed_list: list[str] = json.loads(user.backup_codes_hash)
    match_index = _verify_backup_code(body.backup_code, hashed_list)
    if match_index is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid backup code.",
        )

    # Remove the consumed code
    hashed_list.pop(match_index)
    user.backup_codes_hash = json.dumps(hashed_list)
    await session.commit()

    token = create_access_token(user.id, user.email)
    memberships = await repo.get_memberships(user.id)

    from app.api.auth.routes import _user_response
    return {"token": token, "user": _user_response(user, memberships)}


@router.post("/backup-codes/regenerate")
async def regenerate_backup_codes(
    current_user=Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    """Invalidate all existing backup codes and issue 8 new ones."""
    if current_user.totp_enabled_at is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="2FA is not enabled.",
        )

    plain_codes, hashed_codes = _generate_backup_codes()
    current_user.backup_codes_hash = json.dumps(hashed_codes)
    await session.commit()

    return {"backup_codes": plain_codes}


@router.patch("/org/require")
async def set_org_require_2fa(
    body: RequireTwoFARequest,
    current_user=Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    """Owner-only: toggle org-wide 2FA enforcement.

    When turning ON:
    - Sets require_2fa = True on business_config
    - Sets a 24-hour grace deadline (totp_grace_until) on every member
      who does not yet have 2FA enabled
    - Sends in-app notification + email to each affected member
    """
    # Resolve caller's membership and business
    result = await session.execute(
        select(BusinessMemberModel)
        .options(selectinload(BusinessMemberModel.business))
        .where(BusinessMemberModel.user_id == current_user.id)
    )
    membership = result.scalars().first()
    if not membership or membership.role != "owner":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the business owner can change 2FA enforcement.",
        )

    business_id = membership.business_id

    # Load or create business_config
    config_result = await session.execute(
        select(BusinessConfigModel).where(
            BusinessConfigModel.business_id == business_id
        )
    )
    config = config_result.scalars().first()
    if not config:
        raise HTTPException(status_code=404, detail="Business config not found.")

    config.require_2fa = body.require

    if body.require:
        config.require_2fa_enforced_at = datetime.now(_tz.utc)
        grace_deadline = datetime.now(_tz.utc) + timedelta(hours=_GRACE_HOURS)

        # Load all active members
        members_result = await session.execute(
            select(BusinessMemberModel)
            .options(selectinload(BusinessMemberModel.user))
            .where(
                BusinessMemberModel.business_id == business_id,
                BusinessMemberModel.is_active == True,  # noqa: E712
            )
        )
        members = members_result.scalars().all()

        notif_repo = NotificationRepository(session)
        affected: list[UserModel] = []

        for m in members:
            u = m.user
            if u and u.id != current_user.id and not u.totp_enabled_at:
                u.totp_grace_until = grace_deadline
                affected.append(u)
                await notif_repo.create(
                    user_id=u.id,
                    business_id=business_id,
                    title="Two-factor authentication required",
                    message=(
                        "Your organisation now requires 2FA. "
                        "Please set it up within 24 hours to keep access."
                    ),
                    type="warning",
                    resource_type="security",
                )

        await session.flush()

        # Fire emails outside the loop (best-effort)
        for u in affected:
            await send_2fa_enforced_email(
                to=u.email,
                display_name=u.display_name,
                grace_hours=_GRACE_HOURS,
                frontend_url=Settings.FRONTEND_URL,
            )
    else:
        # Turning off enforcement: clear grace deadlines for members who
        # had not yet set up 2FA (don't strip from those who did)
        members_result = await session.execute(
            select(BusinessMemberModel)
            .options(selectinload(BusinessMemberModel.user))
            .where(
                BusinessMemberModel.business_id == business_id,
                BusinessMemberModel.is_active == True,  # noqa: E712
            )
        )
        for m in members_result.scalars().all():
            if m.user and m.user.totp_grace_until and not m.user.totp_enabled_at:
                m.user.totp_grace_until = None

    await session.commit()
    return {"require_2fa": config.require_2fa}
