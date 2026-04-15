"""Wallet routes.

GET  /wallet                 — get balance (owner, approver)
POST /wallet/topup           — credit wallet (owner only)
GET  /wallet/transactions    — paginated ledger (owner, approver)
"""

import logging
import uuid
from decimal import Decimal, InvalidOperation
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth.dependencies import get_current_user
from app.api.auth.role_deps import require_role
from src.config.settings import Settings
from src.infrastructure.database.connection import get_db_session
from src.infrastructure.database.flowpilot_models import (
    BusinessMemberModel,
    BusinessModel,
    UserModel,
)
from src.infrastructure.database.repositories.wallet_repository import (
    WalletRepository,
)

logger = logging.getLogger(__name__)
router = APIRouter()


# ── Pydantic schemas ──────────────────────────────────────────────────────────


class WalletResponse(BaseModel):
    id: str
    business_id: str
    balance: float
    currency: str
    created_at: str
    updated_at: str


class TopUpRequest(BaseModel):
    amount: float = Field(..., gt=0, description="Amount to add (must be > 0)")
    reference: str = Field(
        ..., min_length=1, max_length=255, description="Unique payment reference"
    )
    description: Optional[str] = Field(None, max_length=500)


class TopUpResponse(BaseModel):
    balance: float
    amount_credited: float
    reference: str
    already_processed: bool


class WalletTransactionResponse(BaseModel):
    id: str
    type: str
    amount: float
    reference: str
    description: Optional[str]
    run_id: Optional[str]
    balance_before: float
    balance_after: float
    created_at: str


class WalletTransactionListResponse(BaseModel):
    transactions: list[WalletTransactionResponse]
    total: int
    limit: int
    offset: int


# ── Helpers ───────────────────────────────────────────────────────────────────


async def _get_membership(session, user_id: uuid.UUID, business_id: uuid.UUID):
    result = await session.execute(
        select(BusinessMemberModel).where(
            BusinessMemberModel.user_id == user_id,
            BusinessMemberModel.business_id == business_id,
            BusinessMemberModel.is_active.is_(True),
        )
    )
    return result.scalars().first()


async def _get_owner(session, business_id: uuid.UUID):
    """Return (membership, user) for the business owner."""
    result = await session.execute(
        select(BusinessMemberModel, UserModel)
        .join(UserModel, BusinessMemberModel.user_id == UserModel.id)
        .where(
            BusinessMemberModel.business_id == business_id,
            BusinessMemberModel.role == "owner",
            BusinessMemberModel.is_active.is_(True),
        )
        .limit(1)
    )
    return result.first()


# ── Routes ────────────────────────────────────────────────────────────────────


@router.get("/wallet", response_model=WalletResponse)
async def get_wallet(
    business_id: str = Query(..., description="Business UUID"),
    session: AsyncSession = Depends(get_db_session),
    current_user=Depends(get_current_user),
    _=Depends(require_role("owner", "approver")),
):
    try:
        business_uuid = uuid.UUID(business_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid business_id")

    membership = await _get_membership(session, current_user.id, business_uuid)
    if not membership:
        raise HTTPException(status_code=403, detail="Access denied")

    # KYC check — wallet requires verified KYC
    biz_result = await session.execute(
        select(BusinessModel).where(BusinessModel.id == business_uuid)
    )
    biz = biz_result.scalar_one_or_none()
    if biz and biz.kyc_status not in ("verified",):
        kyc_status = biz.kyc_status or "not_submitted"
        if kyc_status == "pending":
            raise HTTPException(
                status_code=403,
                detail="KYC verification is pending. Wallet access will be enabled once verified.",
                headers={"X-KYC-Status": "pending"},
            )
        elif kyc_status in ("not_submitted", None):
            raise HTTPException(
                status_code=403,
                detail="Complete KYC verification to access the wallet.",
                headers={"X-KYC-Status": "not_submitted"},
            )
        elif kyc_status == "rejected":
            raise HTTPException(
                status_code=403,
                detail="KYC verification was rejected. Please resubmit your documents.",
                headers={"X-KYC-Status": "rejected"},
            )

    repo = WalletRepository(session)
    wallet = await repo.get_or_create(business_uuid)
    await session.commit()

    return WalletResponse(
        id=str(wallet.id),
        business_id=str(wallet.business_id),
        balance=float(wallet.balance),
        currency=wallet.currency,
        created_at=wallet.created_at.isoformat(),
        updated_at=wallet.updated_at.isoformat(),
    )


@router.post("/wallet/topup", response_model=TopUpResponse)
async def topup_wallet(
    request: TopUpRequest,
    business_id: str = Query(..., description="Business UUID"),
    session: AsyncSession = Depends(get_db_session),
    current_user=Depends(get_current_user),
    _=Depends(require_role("owner")),
):
    try:
        business_uuid = uuid.UUID(business_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid business_id")

    membership = await _get_membership(session, current_user.id, business_uuid)
    if not membership:
        raise HTTPException(status_code=403, detail="Access denied")

    try:
        amount = Decimal(str(request.amount))
    except InvalidOperation:
        raise HTTPException(status_code=400, detail="Invalid amount")

    repo = WalletRepository(session)
    tx, created = await repo.credit(
        business_id=business_uuid,
        amount=amount,
        reference=request.reference,
        description=request.description or "Wallet top-up",
    )
    await session.commit()
    await session.refresh(tx)

    new_balance = float(tx.balance_after)

    # Fire top-up confirmation email + in-app notification (best-effort, non-blocking)
    if created:
        try:
            owner_row = await _get_owner(session, business_uuid)
            if owner_row:
                _, owner_user = owner_row

                # In-app notification
                from src.infrastructure.database.repositories.notification_repository import NotificationRepository
                notif_repo = NotificationRepository(session)
                await notif_repo.create(
                    user_id=owner_user.id,
                    business_id=business_uuid,
                    title="Wallet top-up successful",
                    message=(
                        f"₦{float(amount):,.2f} has been credited to your wallet. "
                        f"New balance: ₦{new_balance:,.2f}."
                    ),
                    type="success",
                    resource_type="wallet",
                )
                await session.commit()

                # Email
                from src.services.email_service import send_wallet_topup_email
                import asyncio as _asyncio
                _asyncio.create_task(
                    send_wallet_topup_email(
                        to=owner_user.email,
                        display_name=owner_user.display_name or owner_user.email,
                        amount=float(amount),
                        new_balance=new_balance,
                        reference=request.reference,
                    )
                )
        except Exception as exc:
            logger.warning("[Wallet] Could not send top-up notification: %s", exc)

    return TopUpResponse(
        balance=new_balance,
        amount_credited=float(amount),
        reference=tx.reference,
        already_processed=not created,
    )


@router.get("/wallet/transactions", response_model=WalletTransactionListResponse)
async def list_wallet_transactions(
    business_id: str = Query(..., description="Business UUID"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_db_session),
    current_user=Depends(get_current_user),
    _=Depends(require_role("owner", "approver")),
):
    try:
        business_uuid = uuid.UUID(business_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid business_id")

    membership = await _get_membership(session, current_user.id, business_uuid)
    if not membership:
        raise HTTPException(status_code=403, detail="Access denied")

    repo = WalletRepository(session)
    transactions, total = await repo.list_transactions(
        business_id=business_uuid, limit=limit, offset=offset
    )

    return WalletTransactionListResponse(
        transactions=[
            WalletTransactionResponse(
                id=str(t.id),
                type=t.type,
                amount=float(t.amount),
                reference=t.reference,
                description=t.description,
                run_id=str(t.run_id) if t.run_id else None,
                balance_before=float(t.balance_before),
                balance_after=float(t.balance_after),
                created_at=t.created_at.isoformat(),
            )
            for t in transactions
        ],
        total=total,
        limit=limit,
        offset=offset,
    )
