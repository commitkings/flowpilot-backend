"""Wallet routes.

GET  /wallet                 — get balance (owner, approver)
POST /wallet/topup           — credit wallet (owner only)
GET  /wallet/transactions    — paginated ledger (owner, approver)
"""

import logging
import uuid
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth.dependencies import get_current_user
from app.api.auth.role_deps import require_role
from app.api.auth.kyc_deps import require_verified_kyc
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
    balance: Decimal
    reserved_balance: Decimal = Decimal("0.00")
    available_balance: Decimal = Decimal("0.00")
    currency: str
    total_credit: Decimal = Decimal("0.00")
    total_debit: Decimal = Decimal("0.00")
    created_at: str
    updated_at: str
    # instant = POST /wallet/topup can credit (simulated / lookup_only with Monnify, or non-Monnify).
    # webhook = live Monnify: fund reserved account; Monnify webhook credits wallet.
    topup_behavior: Literal["instant", "webhook"]


class TopUpRequest(BaseModel):
    amount: Decimal = Field(..., gt=0, description="Amount to add (must be > 0)")
    reference: str = Field(
        ..., min_length=1, max_length=255, description="Unique payment reference"
    )
    description: Optional[str] = Field(None, max_length=500)


class TopUpResponse(BaseModel):
    balance: Decimal
    amount_credited: Decimal
    reference: str
    already_processed: bool
    wallet_cap: Optional[Decimal] = None
    balance_warning: Optional[str] = None


class WalletTransactionResponse(BaseModel):
    id: str
    type: str
    amount: Decimal
    reference: str
    description: Optional[str]
    run_id: Optional[str]
    balance_before: Decimal
    balance_after: Decimal
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

    # Compute aggregate credit / debit totals
    from sqlalchemy import func as _func
    from src.infrastructure.database.flowpilot_models import WalletTransactionModel
    _credit_result = await session.execute(
        select(_func.coalesce(_func.sum(WalletTransactionModel.amount), 0)).where(
            WalletTransactionModel.business_id == business_uuid,
            WalletTransactionModel.type == "credit",
        )
    )
    total_credit = Decimal(str(_credit_result.scalar_one() or 0))
    _debit_result = await session.execute(
        select(_func.coalesce(_func.sum(WalletTransactionModel.amount), 0)).where(
            WalletTransactionModel.business_id == business_uuid,
            WalletTransactionModel.type == "debit",
        )
    )
    total_debit = Decimal(str(_debit_result.scalar_one() or 0))

    bal = wallet.balance
    rb = getattr(wallet, "reserved_balance", Decimal("0.00")) or Decimal("0.00")
    _topup_instant = not (
        Settings.PAYOUT_PROVIDER == "monnify" and not Settings.is_payout_simulated()
    )
    return WalletResponse(
        id=str(wallet.id),
        business_id=str(wallet.business_id),
        balance=bal,
        reserved_balance=rb,
        available_balance=bal - rb,
        currency=wallet.currency,
        total_credit=total_credit,
        total_debit=total_debit,
        created_at=wallet.created_at.isoformat(),
        updated_at=wallet.updated_at.isoformat(),
        topup_behavior="instant" if _topup_instant else "webhook",
    )


@router.post("/wallet/topup", response_model=TopUpResponse)
async def topup_wallet(
    request: TopUpRequest,
    business_id: str = Query(..., description="Business UUID"),
    session: AsyncSession = Depends(get_db_session),
    current_user=Depends(get_current_user),
    _=Depends(require_role("owner")),
    _kyc=Depends(require_verified_kyc),
):
    # Live Monnify collections: wallet is credited from the Monnify webhook on bank transfer
    # to the business reserved account — not from this endpoint.
    # Simulated / lookup_only modes still allow manual top-up for local dev and QA.
    if Settings.PAYOUT_PROVIDER == "monnify" and not Settings.is_payout_simulated():
        raise HTTPException(
            status_code=410,
            detail=(
                "Direct wallet top-up is disabled when payouts are live with Monnify. "
                "Fund your reserved account by bank transfer; credits are applied automatically."
            ),
        )
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

    # Enforce wallet balance cap based on KYC level.
    # In production the manual topup endpoint is disabled (Monnify webhook handles credits),
    # so this guard only applies in dev/simulated mode. Rather than blocking, we allow the
    # credit and trigger the overlimit flow so the full warning path can be tested locally.
    from src.config.kyc_limits import get_limits as _get_limits
    biz_for_cap = await session.execute(select(BusinessModel).where(BusinessModel.id == business_uuid))
    biz_cap = biz_for_cap.scalar_one_or_none()
    _topup_would_exceed_cap = False
    if biz_cap:
        from src.infrastructure.database.repositories.wallet_repository import WalletRepository as _WR
        _cap_repo = _WR(session)
        _current_wallet = await _cap_repo.get_or_create(business_uuid)
        _account_type = getattr(biz_cap, "account_type", "business") or "business"
        _kyc_level = getattr(biz_cap, "kyc_level", 0) or 0
        _limits = _get_limits(_account_type, _kyc_level)
        if _limits:
            from decimal import Decimal as _D
            _cap = _limits["wallet"]
            if _current_wallet.balance + _D(str(request.amount)) > _cap:
                if Settings.is_production():
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            f"Wallet balance cap exceeded. Your current KYC level allows a maximum wallet "
                            f"balance of ₦{float(_cap):,.2f}. Complete a higher KYC level to increase this limit."
                        ),
                    )
                # Non-production: allow the credit but flag for overlimit flow below
                _topup_would_exceed_cap = True

    repo = WalletRepository(session)
    tx, created = await repo.credit(
        business_id=business_uuid,
        amount=amount,
        reference=request.reference,
        description=request.description or "Wallet top-up",
    )
    await session.commit()
    await session.refresh(tx)

    new_balance = tx.balance_after

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
                        f"₦{amount:,.2f} has been credited to your wallet. "
                        f"New balance: ₦{new_balance:,.2f}."
                    ),
                    type="success",
                    resource_type="wallet",
                )
                await session.commit()

                # Email
                from src.services.email_service import send_wallet_topup_email, check_notification_pref as _cnp_w
                import asyncio as _asyncio
                if _cnp_w(owner_user, "wallet_alerts"):
                    _asyncio.create_task(
                        send_wallet_topup_email(
                            to=owner_user.email,
                            display_name=owner_user.display_name or owner_user.email,
                            amount=amount,
                            new_balance=new_balance,
                            reference=request.reference,
                        )
                    )
        except Exception as exc:
            logger.warning("[Wallet] Could not send top-up notification: %s", exc)

    # Non-production overlimit simulation: credit went through above the cap,
    # now trigger the same flag + email flow as the Monnify webhook does in prod.
    if created and _topup_would_exceed_cap and biz_cap:
        try:
            from src.services.wallet_limit_service import check_and_flag_overlimit
            await check_and_flag_overlimit(session, biz_cap, new_balance)
            await session.commit()
        except Exception as exc:
            logger.warning("[Wallet] Could not run overlimit check: %s", exc)

    # Compute wallet cap info for response
    _resp_cap: Optional[Decimal] = None
    _resp_warning: Optional[str] = None
    try:
        from src.config.kyc_limits import get_limits as _get_limits_resp
        if biz_cap:
            _at = getattr(biz_cap, "account_type", "business") or "business"
            _kl = getattr(biz_cap, "kyc_level", 0) or 0
            _lim = _get_limits_resp(_at, _kl)
            if _lim:
                _resp_cap = _lim["wallet"]
                _pct = new_balance / _resp_cap
                if new_balance > _resp_cap:
                    _resp_warning = (
                        f"Your balance of ₦{float(new_balance):,.2f} exceeds your KYC tier limit of "
                        f"₦{float(_resp_cap):,.2f}. Upgrade your KYC level to avoid restrictions."
                    )
                elif _pct >= Decimal("0.9"):
                    _remaining = _resp_cap - new_balance
                    _resp_warning = (
                        f"You are approaching your wallet limit. "
                        f"Only ₦{float(_remaining):,.2f} remaining before you reach the "
                        f"₦{float(_resp_cap):,.2f} cap for your KYC level."
                    )
    except Exception:
        pass

    return TopUpResponse(
        balance=new_balance,
        amount_credited=amount,
        reference=tx.reference,
        already_processed=not created,
        wallet_cap=_resp_cap,
        balance_warning=_resp_warning,
    )


class WithdrawRequest(BaseModel):
    amount: Decimal = Field(..., gt=0, description="Amount to withdraw (must be > 0)")
    reference: str = Field(..., min_length=1, max_length=255, description="Unique withdrawal reference")
    description: Optional[str] = Field(None, max_length=500)


class WithdrawResponse(BaseModel):
    balance: Decimal
    amount_debited: Decimal
    reference: str


@router.post("/wallet/withdraw", response_model=WithdrawResponse)
async def withdraw_wallet(
    request: WithdrawRequest,
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

    from src.infrastructure.database.repositories.wallet_repository import InsufficientBalanceError
    repo = WalletRepository(session)
    try:
        tx, created = await repo.debit(
            business_id=business_uuid,
            amount=amount,
            reference=request.reference,
            description=request.description or "Wallet withdrawal",
        )
    except InsufficientBalanceError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Insufficient balance: available ₦{float(exc.balance):,.2f}, requested ₦{float(exc.required):,.2f}",
        )
    await session.commit()
    await session.refresh(tx)

    # In-app notification (best-effort)
    try:
        owner_row = await _get_owner(session, business_uuid)
        if owner_row:
            _, owner_user = owner_row
            from src.infrastructure.database.repositories.notification_repository import NotificationRepository
            notif_repo = NotificationRepository(session)
            await notif_repo.create(
                user_id=owner_user.id,
                business_id=business_uuid,
                title="Wallet withdrawal recorded",
                message=(
                    f"₦{float(amount):,.2f} withdrawal recorded. "
                    f"New balance: ₦{float(tx.balance_after):,.2f}."
                ),
                type="info",
                resource_type="wallet",
            )
            await session.commit()
    except Exception as exc:
        logger.warning("[Wallet] Could not send withdrawal notification: %s", exc)

    return WithdrawResponse(
        balance=tx.balance_after,
        amount_debited=amount,
        reference=tx.reference,
    )


# ── AI Credit routes ──────────────────────────────────────────────────────────

# Credit bundle options: {credits: price_ngn}
_CREDIT_BUNDLES: dict[int, int] = {5: 2500, 20: 9000, 50: 21000}


class CreditBalanceResponse(BaseModel):
    business_id: str
    balance: int
    bundles: list[dict]  # [{credits, price}]


class CreditPurchaseRequest(BaseModel):
    credits: int = Field(..., description="Must be one of: 5, 20, 50")
    reference: str = Field(..., min_length=1, max_length=255, description="Unique payment reference")


class CreditPurchaseResponse(BaseModel):
    balance: int
    credits_added: int
    amount_charged: Decimal
    reference: str
    already_processed: bool


class CreditTransactionResponse(BaseModel):
    id: str
    type: str
    credits: int
    description: Optional[str]
    run_id: Optional[str]
    created_at: str


class CreditTransactionListResponse(BaseModel):
    transactions: list[CreditTransactionResponse]
    total: int


@router.get("/wallet/credits", response_model=CreditBalanceResponse)
async def get_credits(
    business_id: str = Query(..., description="Business UUID"),
    session: AsyncSession = Depends(get_db_session),
    current_user=Depends(get_current_user),
    _=Depends(require_role("owner", "approver")),
    _kyc=Depends(require_verified_kyc),
):
    try:
        business_uuid = uuid.UUID(business_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid business_id")

    membership = await _get_membership(session, current_user.id, business_uuid)
    if not membership:
        raise HTTPException(status_code=403, detail="Access denied")

    from src.infrastructure.database.flowpilot_models import BusinessModel as _BizModel
    biz_result = await session.execute(
        select(_BizModel).where(_BizModel.id == business_uuid)
    )
    biz = biz_result.scalar_one_or_none()
    if not biz:
        raise HTTPException(status_code=404, detail="Business not found")

    return CreditBalanceResponse(
        business_id=str(biz.id),
        balance=biz.ai_credit_balance,
        bundles=[{"credits": k, "price": v} for k, v in _CREDIT_BUNDLES.items()],
    )


@router.post("/wallet/credits/purchase", response_model=CreditPurchaseResponse)
async def purchase_credits(
    request: CreditPurchaseRequest,
    business_id: str = Query(..., description="Business UUID"),
    session: AsyncSession = Depends(get_db_session),
    current_user=Depends(get_current_user),
    _=Depends(require_role("owner")),
    _kyc=Depends(require_verified_kyc),
):
    try:
        business_uuid = uuid.UUID(business_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid business_id")

    if request.credits not in _CREDIT_BUNDLES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid bundle size. Choose from: {list(_CREDIT_BUNDLES.keys())}",
        )

    membership = await _get_membership(session, current_user.id, business_uuid)
    if not membership:
        raise HTTPException(status_code=403, detail="Access denied")

    bundle_price = _CREDIT_BUNDLES[request.credits]
    bundle_price_decimal = Decimal(str(bundle_price))

    from src.infrastructure.database.flowpilot_models import (
        BusinessModel as _BizModel,
        AiCreditTransactionModel as _CreditTxModel,
    )
    from src.infrastructure.database.repositories.wallet_repository import (
        InsufficientBalanceError as _InsufficientBalance,
    )
    from sqlalchemy import select as _sel

    # ── Idempotency: use the wallet transaction as the authoritative token ────
    # wallet_transaction.reference has a UNIQUE constraint, so the wallet repo
    # guarantees exactly-once processing.  If created=False the debit already
    # happened; skip the credit addition and return the current balance.
    repo = WalletRepository(session)
    wallet_ref = f"credit_purchase_{request.reference}"
    try:
        _debit_tx, created = await repo.debit(
            business_id=business_uuid,
            amount=bundle_price_decimal,
            reference=wallet_ref,
            description=f"AI credit bundle ({request.credits} credits)",
        )
    except _InsufficientBalance:
        raise HTTPException(
            status_code=402,
            detail=f"Insufficient wallet balance. Top up at least ₦{bundle_price:,} before purchasing credits.",
        )

    if not created:
        # Already processed — return current balance without modifying anything
        biz_result = await session.execute(
            _sel(_BizModel).where(_BizModel.id == business_uuid)
        )
        biz = biz_result.scalar_one_or_none()
        return CreditPurchaseResponse(
            balance=biz.ai_credit_balance if biz else 0,
            credits_added=request.credits,
            amount_charged=bundle_price,
            reference=request.reference,
            already_processed=True,
        )

    # New purchase — add credits and log the transaction
    biz_result = await session.execute(
        _sel(_BizModel).where(_BizModel.id == business_uuid)
    )
    biz = biz_result.scalar_one_or_none()
    if not biz:
        raise HTTPException(status_code=404, detail="Business not found")

    biz.ai_credit_balance += request.credits

    session.add(_CreditTxModel(
        business_id=business_uuid,
        type="purchase",
        credits=request.credits,
        description=f"Purchased {request.credits} credits · ref: {request.reference}",
    ))
    await session.commit()

    return CreditPurchaseResponse(
        balance=biz.ai_credit_balance,
        credits_added=request.credits,
        amount_charged=bundle_price,
        reference=request.reference,
        already_processed=False,
    )


@router.get("/wallet/credits/transactions", response_model=CreditTransactionListResponse)
async def list_credit_transactions(
    business_id: str = Query(..., description="Business UUID"),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_db_session),
    current_user=Depends(get_current_user),
    _=Depends(require_role("owner", "approver")),
    _kyc=Depends(require_verified_kyc),
):
    try:
        business_uuid = uuid.UUID(business_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid business_id")

    membership = await _get_membership(session, current_user.id, business_uuid)
    if not membership:
        raise HTTPException(status_code=403, detail="Access denied")

    from src.infrastructure.database.flowpilot_models import AiCreditTransactionModel as _CreditTxModel
    from sqlalchemy import select as _sel, func as _func

    count_result = await session.execute(
        _sel(_func.count()).select_from(_CreditTxModel).where(
            _CreditTxModel.business_id == business_uuid
        )
    )
    total = count_result.scalar_one()

    rows_result = await session.execute(
        _sel(_CreditTxModel)
        .where(_CreditTxModel.business_id == business_uuid)
        .order_by(_CreditTxModel.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    rows = rows_result.scalars().all()

    return CreditTransactionListResponse(
        transactions=[
            CreditTransactionResponse(
                id=str(r.id),
                type=r.type,
                credits=r.credits,
                description=r.description,
                run_id=str(r.run_id) if r.run_id else None,
                created_at=r.created_at.isoformat(),
            )
            for r in rows
        ],
        total=total,
    )


@router.get("/wallet/transactions", response_model=WalletTransactionListResponse)
async def list_wallet_transactions(
    business_id: str = Query(..., description="Business UUID"),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    month: Optional[str] = Query(None, description="Filter by month: YYYY-MM"),
    session: AsyncSession = Depends(get_db_session),
    current_user=Depends(get_current_user),
    _=Depends(require_role("owner", "approver")),
    _kyc=Depends(require_verified_kyc),
):
    try:
        business_uuid = uuid.UUID(business_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid business_id")

    membership = await _get_membership(session, current_user.id, business_uuid)
    if not membership:
        raise HTTPException(status_code=403, detail="Access denied")

    # Parse optional month filter
    month_start = None
    month_end = None
    if month:
        try:
            from datetime import datetime as _dt, timezone as _tz
            month_start = _dt.strptime(month, "%Y-%m").replace(tzinfo=_tz.utc)
            # First day of next month
            if month_start.month == 12:
                month_end = month_start.replace(year=month_start.year + 1, month=1)
            else:
                month_end = month_start.replace(month=month_start.month + 1)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid month format. Use YYYY-MM.")

    repo = WalletRepository(session)
    transactions, total = await repo.list_transactions(
        business_id=business_uuid, limit=limit, offset=offset,
        month_start=month_start, month_end=month_end,
    )

    return WalletTransactionListResponse(
        transactions=[
            WalletTransactionResponse(
                id=str(t.id),
                type=t.type,
                amount=t.amount,
                reference=t.reference,
                description=t.description,
                run_id=str(t.run_id) if t.run_id else None,
                balance_before=t.balance_before,
                balance_after=t.balance_after,
                created_at=t.created_at.isoformat(),
            )
            for t in transactions
        ],
        total=total,
        limit=limit,
        offset=offset,
    )
