"""
Developer routes — API keys and Webhooks.

All routes require a valid JWT. API key routes are owner-only.
Webhook routes are also owner-only.

Endpoints:
    GET    /developer/api-keys          — { keys: [...] }
    POST   /developer/api-keys          — create (raw key shown once)
    DELETE /developer/api-keys/{id}     — { status: "revoked" }
    PATCH  /developer/api-keys/{id}     — update name / scopes

    GET    /developer/webhooks          — { webhooks: [...] }
    POST   /developer/webhooks          — create (raw secret shown once)
    DELETE /developer/webhooks/{id}     — { status: "deleted" }
    PATCH  /developer/webhooks/{id}     — update is_active / events
"""

import base64
import hashlib
import hmac
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, HttpUrl
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth.dependencies import get_current_user
from app.api.auth.role_deps import require_role
from app.api.auth.api_key_auth import generate_api_key
from src.infrastructure.database.connection import get_db_session
from src.infrastructure.database.flowpilot_models import (
    ApiKeyModel,
    BusinessMemberModel,
    WebhookModel,
)

router = APIRouter(prefix="/developer", tags=["developer"])

VALID_SCOPES = {
    "runs:read",
    "runs:write",
    "transactions:read",
    "audit:read",
    "approvals:read",
    "approvals:write",
}

VALID_EVENTS = {
    "run.completed",
    "run.failed",
    "payout.succeeded",
    "payout.failed",
    "approval.requested",
    "approval.completed",
    "candidate.flagged",
}

_PBKDF2_ITERATIONS = 100_000


def _hash_secret(raw: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", raw.encode(), salt, _PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${base64.urlsafe_b64encode(salt).decode()}${base64.urlsafe_b64encode(dk).decode()}"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

async def _get_business_id(current_user, session: AsyncSession) -> uuid.UUID:
    result = await session.execute(
        select(BusinessMemberModel).where(
            BusinessMemberModel.user_id == current_user.id
        )
    )
    membership = result.scalars().first()
    if not membership:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No business membership found")
    return membership.business_id


# =========================================================================== #
# API Keys
# =========================================================================== #

class ApiKeyOut(BaseModel):
    id: str
    name: str
    prefix: str          # first 10 chars of raw key
    scopes: list[str]
    last_used_at: Optional[str]
    expires_at: Optional[str]
    created_at: str


class ApiKeyCreatedOut(ApiKeyOut):
    raw_key: str


class CreateApiKeyRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    scopes: list[str] = Field(default_factory=list)
    expires_in_days: Optional[int] = Field(None, ge=1, le=3650)


class PatchApiKeyRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=128)
    scopes: Optional[list[str]] = None


def _serialize_key(k: ApiKeyModel) -> ApiKeyOut:
    return ApiKeyOut(
        id=str(k.id),
        name=k.name,
        prefix=k.key_prefix,
        scopes=list(k.scopes or []),
        last_used_at=k.last_used_at.isoformat() if k.last_used_at else None,
        expires_at=k.expires_at.isoformat() if k.expires_at else None,
        created_at=k.created_at.isoformat(),
    )


@router.get("/api-keys")
async def list_api_keys(
    current_user=Depends(get_current_user),
    _=Depends(require_role("owner")),
    session: AsyncSession = Depends(get_db_session),
):
    """Return { keys: [...] }"""
    business_id = await _get_business_id(current_user, session)
    result = await session.execute(
        select(ApiKeyModel).where(
            ApiKeyModel.business_id == business_id,
            ApiKeyModel.revoked_at.is_(None),
        ).order_by(ApiKeyModel.created_at.desc())
    )
    keys = result.scalars().all()
    return {"keys": [_serialize_key(k) for k in keys]}


@router.post("/api-keys", status_code=status.HTTP_201_CREATED)
async def create_api_key(
    body: CreateApiKeyRequest,
    current_user=Depends(get_current_user),
    _=Depends(require_role("owner")),
    session: AsyncSession = Depends(get_db_session),
):
    invalid = set(body.scopes) - VALID_SCOPES
    if invalid:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid scope(s): {', '.join(sorted(invalid))}",
        )

    business_id = await _get_business_id(current_user, session)
    raw_key, key_prefix, key_hash = generate_api_key()

    expires_at = None
    if body.expires_in_days:
        expires_at = datetime.now(timezone.utc) + timedelta(days=body.expires_in_days)

    api_key = ApiKeyModel(
        business_id=business_id,
        created_by=current_user.id,
        name=body.name,
        key_prefix=key_prefix,
        key_hash=key_hash,
        scopes=body.scopes,
        expires_at=expires_at,
    )
    session.add(api_key)
    await session.commit()
    await session.refresh(api_key)

    return ApiKeyCreatedOut(
        id=str(api_key.id),
        name=api_key.name,
        prefix=api_key.key_prefix,
        scopes=list(api_key.scopes or []),
        last_used_at=None,
        expires_at=api_key.expires_at.isoformat() if api_key.expires_at else None,
        created_at=api_key.created_at.isoformat(),
        raw_key=raw_key,
    )


@router.delete("/api-keys/{key_id}")
async def revoke_api_key(
    key_id: uuid.UUID,
    current_user=Depends(get_current_user),
    _=Depends(require_role("owner")),
    session: AsyncSession = Depends(get_db_session),
):
    business_id = await _get_business_id(current_user, session)
    result = await session.execute(
        select(ApiKeyModel).where(
            ApiKeyModel.id == key_id,
            ApiKeyModel.business_id == business_id,
            ApiKeyModel.revoked_at.is_(None),
        )
    )
    if not result.scalars().first():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="API key not found")

    await session.execute(
        update(ApiKeyModel)
        .where(ApiKeyModel.id == key_id)
        .values(revoked_at=datetime.now(timezone.utc))
    )
    await session.commit()
    return {"status": "revoked"}


@router.patch("/api-keys/{key_id}")
async def update_api_key(
    key_id: uuid.UUID,
    body: PatchApiKeyRequest,
    current_user=Depends(get_current_user),
    _=Depends(require_role("owner")),
    session: AsyncSession = Depends(get_db_session),
):
    if body.scopes is not None:
        invalid = set(body.scopes) - VALID_SCOPES
        if invalid:
            raise HTTPException(status_code=422, detail=f"Invalid scope(s): {', '.join(sorted(invalid))}")

    business_id = await _get_business_id(current_user, session)
    result = await session.execute(
        select(ApiKeyModel).where(
            ApiKeyModel.id == key_id,
            ApiKeyModel.business_id == business_id,
            ApiKeyModel.revoked_at.is_(None),
        )
    )
    api_key = result.scalars().first()
    if not api_key:
        raise HTTPException(status_code=404, detail="API key not found")

    values: dict = {}
    if body.name is not None:
        values["name"] = body.name
    if body.scopes is not None:
        values["scopes"] = body.scopes
    if values:
        await session.execute(update(ApiKeyModel).where(ApiKeyModel.id == key_id).values(**values))
        await session.commit()
        await session.refresh(api_key)

    return _serialize_key(api_key)


# =========================================================================== #
# Webhooks
# =========================================================================== #

class WebhookOut(BaseModel):
    id: str
    url: str
    events: list[str]
    is_active: bool
    secret: Optional[str]   # only set on creation
    failure_count: int
    last_triggered_at: Optional[str]
    created_at: str


class CreateWebhookRequest(BaseModel):
    url: str = Field(..., min_length=8)
    events: list[str] = Field(..., min_length=1)


class PatchWebhookRequest(BaseModel):
    is_active: Optional[bool] = None
    events: Optional[list[str]] = None
    url: Optional[str] = None


def _serialize_webhook(w: WebhookModel, secret: Optional[str] = None) -> dict:
    return {
        "id": str(w.id),
        "url": w.url,
        "events": list(w.events or []),
        "is_active": w.is_active,
        "secret": secret,
        "failure_count": w.failure_count,
        "last_triggered_at": w.last_triggered_at.isoformat() if w.last_triggered_at else None,
        "created_at": w.created_at.isoformat(),
    }


@router.get("/webhooks")
async def list_webhooks(
    current_user=Depends(get_current_user),
    _=Depends(require_role("owner")),
    session: AsyncSession = Depends(get_db_session),
):
    business_id = await _get_business_id(current_user, session)
    result = await session.execute(
        select(WebhookModel).where(
            WebhookModel.business_id == business_id,
        ).order_by(WebhookModel.created_at.desc())
    )
    webhooks = result.scalars().all()
    return {"webhooks": [_serialize_webhook(w) for w in webhooks]}


@router.post("/webhooks", status_code=status.HTTP_201_CREATED)
async def create_webhook(
    body: CreateWebhookRequest,
    current_user=Depends(get_current_user),
    _=Depends(require_role("owner")),
    session: AsyncSession = Depends(get_db_session),
):
    invalid_events = set(body.events) - VALID_EVENTS
    if invalid_events:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid event(s): {', '.join(sorted(invalid_events))}. "
                   f"Valid: {', '.join(sorted(VALID_EVENTS))}",
        )

    business_id = await _get_business_id(current_user, session)
    raw_secret = "whsec_" + secrets.token_urlsafe(32)
    secret_hash = _hash_secret(raw_secret)

    # Send a test ping to verify the endpoint is reachable before activating.
    from src.services.webhook_dispatcher import send_test_ping
    verified = await send_test_ping(str(body.url), raw_secret)

    webhook = WebhookModel(
        business_id=business_id,
        created_by=current_user.id,
        url=str(body.url),
        events=body.events,
        is_active=verified,        # only active if test ping succeeded
        secret_hash=secret_hash,
        signing_secret=raw_secret, # stored for HMAC-SHA256 payload signing
        failure_count=0,
    )
    session.add(webhook)
    await session.commit()
    await session.refresh(webhook)

    response = _serialize_webhook(webhook, secret=raw_secret)
    response["verified"] = verified
    if not verified:
        response["verification_message"] = (
            "Your endpoint did not respond with a 2xx status to the test ping. "
            "The webhook has been saved as inactive. Fix your endpoint and "
            "re-enable it via PATCH /developer/webhooks/{id}."
        )
    return response


@router.delete("/webhooks/{webhook_id}")
async def delete_webhook(
    webhook_id: uuid.UUID,
    current_user=Depends(get_current_user),
    _=Depends(require_role("owner")),
    session: AsyncSession = Depends(get_db_session),
):
    business_id = await _get_business_id(current_user, session)
    result = await session.execute(
        select(WebhookModel).where(
            WebhookModel.id == webhook_id,
            WebhookModel.business_id == business_id,
        )
    )
    webhook = result.scalars().first()
    if not webhook:
        raise HTTPException(status_code=404, detail="Webhook not found")

    await session.delete(webhook)
    await session.commit()
    return {"status": "deleted"}


@router.patch("/webhooks/{webhook_id}")
async def update_webhook(
    webhook_id: uuid.UUID,
    body: PatchWebhookRequest,
    current_user=Depends(get_current_user),
    _=Depends(require_role("owner")),
    session: AsyncSession = Depends(get_db_session),
):
    if body.events is not None:
        invalid_events = set(body.events) - VALID_EVENTS
        if invalid_events:
            raise HTTPException(status_code=422, detail=f"Invalid event(s): {', '.join(sorted(invalid_events))}")

    business_id = await _get_business_id(current_user, session)
    result = await session.execute(
        select(WebhookModel).where(
            WebhookModel.id == webhook_id,
            WebhookModel.business_id == business_id,
        )
    )
    webhook = result.scalars().first()
    if not webhook:
        raise HTTPException(status_code=404, detail="Webhook not found")

    values: dict = {"updated_at": datetime.now(timezone.utc)}
    if body.is_active is not None:
        values["is_active"] = body.is_active
    if body.events is not None:
        values["events"] = body.events
    if body.url is not None:
        values["url"] = body.url

    await session.execute(
        update(WebhookModel).where(WebhookModel.id == webhook_id).values(**values)
    )
    await session.commit()
    await session.refresh(webhook)

    return _serialize_webhook(webhook)
