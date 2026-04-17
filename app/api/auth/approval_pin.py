"""Approval step-up PIN — lets users set a 4-6 digit PIN that must be entered
before confirming a payout run approval, wallet withdrawals, and AI credit purchases.
"""

from __future__ import annotations

import logging
import os
import random
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth.dependencies import get_current_user
from app.api.auth.passwords import hash_pin, verify_password
from src.infrastructure.database.connection import get_db_session
from src.infrastructure.database.flowpilot_models import UserModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth/approval-pin", tags=["approval-pin"])

# ── OTP store for PIN reset (separate Redis namespace from email-verify OTPs) ──
_PIN_RESET_KEY_PREFIX = "fp:otp:pin_reset:"
_PIN_RESET_TTL = 10 * 60  # 10 minutes
_pin_reset_fallback: dict[str, tuple[str, datetime]] = {}
_pin_reset_redis = None


async def _get_redis():
    global _pin_reset_redis
    if _pin_reset_redis is False:
        return None
    url = os.getenv("REDIS_URL", "").strip()
    if not url:
        _pin_reset_redis = False
        return None
    if _pin_reset_redis is None:
        try:
            import redis.asyncio as redis
            _pin_reset_redis = redis.from_url(url, decode_responses=True)
        except Exception as exc:
            logger.warning("PIN reset Redis unavailable, using in-process fallback: %s", exc)
            _pin_reset_redis = False
            return None
    return _pin_reset_redis


async def _save_reset_otp(user_id: str, code: str) -> None:
    r = await _get_redis()
    if r:
        await r.set(f"{_PIN_RESET_KEY_PREFIX}{user_id}", code, ex=_PIN_RESET_TTL)
    else:
        _pin_reset_fallback[user_id] = (
            code, datetime.now(timezone.utc) + timedelta(seconds=_PIN_RESET_TTL)
        )


async def _verify_reset_otp(user_id: str, code: str) -> bool:
    r = await _get_redis()
    if r:
        key = f"{_PIN_RESET_KEY_PREFIX}{user_id}"
        stored = await r.get(key)
        if stored and stored == code:
            await r.delete(key)
            return True
        return False
    else:
        entry = _pin_reset_fallback.get(user_id)
        if entry and entry[0] == code and datetime.now(timezone.utc) < entry[1]:
            del _pin_reset_fallback[user_id]
            return True
        return False


# ── Request / Response models ──────────────────────────────────────────────────

class SetPinRequest(BaseModel):
    pin: str = Field(..., min_length=4, max_length=6, pattern=r"^\d{4,6}$")


class VerifyPinRequest(BaseModel):
    pin: str = Field(..., min_length=4, max_length=6, pattern=r"^\d{4,6}$")


class ResetPinConfirmRequest(BaseModel):
    code: str = Field(..., min_length=6, max_length=6)
    new_pin: str = Field(..., min_length=4, max_length=6, pattern=r"^\d{4,6}$")


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/status")
async def get_pin_status(current_user=Depends(get_current_user)):
    """Return whether the current user has set up an approval PIN."""
    return {"has_pin": bool(current_user.approval_pin_hash)}


@router.post("/setup", status_code=status.HTTP_200_OK)
async def setup_pin(
    body: SetPinRequest,
    current_user=Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    """Create or replace the approval PIN."""
    hashed = hash_pin(body.pin)
    await session.execute(
        update(UserModel)
        .where(UserModel.id == current_user.id)
        .values(approval_pin_hash=hashed)
    )
    await session.commit()
    return {"message": "Approval PIN set successfully."}


@router.post("/verify", status_code=status.HTTP_200_OK)
async def verify_pin(
    body: VerifyPinRequest,
    current_user=Depends(get_current_user),
):
    """Verify the approval PIN. Returns 200 on success, 400 on wrong PIN."""
    if not current_user.approval_pin_hash:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No approval PIN configured. Set one up in Settings → Security.",
        )
    if not verify_password(body.pin, current_user.approval_pin_hash):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Incorrect approval PIN.",
        )
    return {"message": "PIN verified."}


@router.delete("/remove", status_code=status.HTTP_200_OK)
async def remove_pin(
    current_user=Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    """Remove the approval PIN."""
    await session.execute(
        update(UserModel)
        .where(UserModel.id == current_user.id)
        .values(approval_pin_hash=None)
    )
    await session.commit()
    return {"message": "Approval PIN removed."}


@router.post("/reset-request", status_code=status.HTTP_200_OK)
async def request_pin_reset(
    current_user=Depends(get_current_user),
):
    """Initiate a PIN reset.

    - If the user has 2FA enabled, they should use their authenticator app code (method=totp).
    - Otherwise, a 6-digit OTP is sent to their registered email (method=email).
    """
    if not current_user.approval_pin_hash:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No approval PIN is configured.",
        )

    has_totp = bool(getattr(current_user, "totp_enabled_at", None))

    if has_totp:
        return {"method": "totp", "message": "Enter your authenticator app code to reset your PIN."}

    # Generate and email the OTP
    code = f"{random.randint(0, 999999):06d}"
    await _save_reset_otp(str(current_user.id), code)

    try:
        from src.services.email_service import send_pin_reset_otp_email
        await send_pin_reset_otp_email(
            to=current_user.email,
            display_name=current_user.display_name or current_user.email,
            code=code,
        )
    except Exception as exc:
        logger.warning("Could not send PIN reset email to %s: %s", current_user.email, exc)

    return {"method": "email", "message": "A 6-digit verification code has been sent to your email."}


@router.post("/reset-confirm", status_code=status.HTTP_200_OK)
async def confirm_pin_reset(
    body: ResetPinConfirmRequest,
    current_user=Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    """Verify the reset code and set a new PIN.

    Accepts either an email OTP or a TOTP code depending on what /reset-request returned.
    """
    if not current_user.approval_pin_hash:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No approval PIN is configured.",
        )

    has_totp = bool(getattr(current_user, "totp_enabled_at", None))

    if has_totp:
        import pyotp
        if not current_user.totp_secret:
            raise HTTPException(status_code=400, detail="2FA is not properly configured.")
        totp = pyotp.TOTP(current_user.totp_secret)
        if not totp.verify(body.code, valid_window=1):
            raise HTTPException(status_code=400, detail="Invalid authenticator code.")
    else:
        valid = await _verify_reset_otp(str(current_user.id), body.code)
        if not valid:
            raise HTTPException(status_code=400, detail="Invalid or expired verification code.")

    hashed = hash_pin(body.new_pin)
    await session.execute(
        update(UserModel)
        .where(UserModel.id == current_user.id)
        .values(approval_pin_hash=hashed)
    )
    await session.commit()
    return {"message": "Approval PIN has been reset successfully."}
