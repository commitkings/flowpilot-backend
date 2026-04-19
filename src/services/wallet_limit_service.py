"""Shared wallet overlimit check — called from both the Monnify webhook and the manual topup."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.config.kyc_limits import get_limits
from src.infrastructure.database.flowpilot_models import (
    BusinessMemberModel,
    BusinessModel,
    UserModel,
    WalletModel,
)

logger = logging.getLogger(__name__)


async def check_and_flag_overlimit(
    session: AsyncSession,
    business: BusinessModel,
    new_balance: Decimal,
) -> None:
    """Flag the wallet and notify the owner if `new_balance` exceeds the KYC tier cap.

    Also clears the flag when the balance comes back within limits (e.g. after payouts).
    Safe to call from both the Monnify webhook and the manual topup route.
    """
    account_type = getattr(business, "account_type", "business") or "business"
    kyc_level = getattr(business, "kyc_level", 0) or 0
    limits = get_limits(account_type, kyc_level)
    if not limits:
        return

    wallet_cap = limits["wallet"]
    wallet_result = await session.execute(
        select(WalletModel).where(WalletModel.business_id == business.id)
    )
    wallet = wallet_result.scalars().first()
    if wallet is None:
        return

    now = datetime.now(timezone.utc)

    if new_balance > wallet_cap:
        if not wallet.is_overlimit:
            wallet.is_overlimit = True
            wallet.overlimit_since = now
            wallet.updated_at = now

        owner_result = await session.execute(
            select(UserModel)
            .join(BusinessMemberModel, BusinessMemberModel.user_id == UserModel.id)
            .where(
                BusinessMemberModel.business_id == business.id,
                BusinessMemberModel.role == "owner",
                BusinessMemberModel.is_active.is_(True),
            )
            .limit(1)
        )
        owner = owner_result.scalars().first()
        if owner:
            from src.infrastructure.database.repositories.notification_repository import NotificationRepository
            from src.services.email_service import send_wallet_overlimit_email, check_notification_pref

            notif_repo = NotificationRepository(session)
            await notif_repo.create(
                user_id=owner.id,
                business_id=business.id,
                title="Wallet balance exceeds KYC limit",
                message=(
                    f"Your wallet balance of ₦{float(new_balance):,.2f} exceeds your KYC tier "
                    f"limit of ₦{float(wallet_cap):,.2f}. Upgrade your KYC level to continue "
                    f"transacting without restrictions."
                ),
                type="warning",
                resource_type="wallet",
            )
            if check_notification_pref(owner, "wallet_alerts"):
                asyncio.create_task(
                    send_wallet_overlimit_email(
                        to=owner.email,
                        display_name=owner.display_name or owner.email,
                        new_balance=float(new_balance),
                        wallet_cap=float(wallet_cap),
                    )
                )

        logger.warning(
            "Wallet overlimit: business %s balance ₦%s exceeds KYC cap ₦%s",
            business.id, new_balance, wallet_cap,
        )

    elif wallet.is_overlimit and new_balance <= wallet_cap:
        wallet.is_overlimit = False
        wallet.overlimit_since = None
        wallet.updated_at = now
