"""
Business repository — create business with owner for onboarding flow.
"""

import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.infrastructure.database.flowpilot_models import (
    BusinessConfigModel,
    BusinessMemberModel,
    BusinessModel,
)
from src.infrastructure.database.flowpilot_models import (
    BusinessAddressModel,
    BusinessPaymentPolicyModel,
    BusinessProfileModel,
    BusinessSecurityPolicyModel,
    BusinessUseCaseModel,
)


def _generate_virtual_account_number(business_id: uuid.UUID) -> str:
    """Derive a deterministic 10-digit virtual account number from the business UUID.

    Uses the integer representation of the UUID, modulo 9 billion, offset to guarantee
    10 digits. Collision probability across UUIDs is negligible for practical org counts.
    """
    n = (business_id.int % 9_000_000_000) + 1_000_000_000
    return str(n)


class BusinessRepository:

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def create_with_owner(
        self,
        *,
        owner_id: uuid.UUID,
        business_name: str,
        account_type: str = "business",
        business_type: str | None = None,
        interswitch_merchant_id: str | None = None,
        monthly_txn_volume_range: str | None = None,
        avg_monthly_payouts_range: str | None = None,
        primary_bank: str | None = None,
        primary_use_cases: list[str] | None = None,
        risk_appetite: str | None = None,
        merchant_state: str | None = None,
        daily_payout_limit: float | None = None,
        single_payout_cap: float | None = None,
        risk_alert_threshold: float | None = None,
        liquidity_alert_buffer: float | None = None,
    ) -> tuple[BusinessModel, BusinessConfigModel, BusinessMemberModel]:
        """Create a business, its config, and assign the caller as owner.

        All inserts happen in the same flush (single transaction).
        """
        now = datetime.now(timezone.utc)

        business = BusinessModel(
            business_name=business_name,
            account_type=account_type,
            ai_credit_balance=5,
        )
        self._s.add(business)
        await self._s.flush()  # assigns business.id

        self._s.add(
            BusinessProfileModel(
                business_id=business.id,
                business_type=business_type,
                interswitch_merchant_id=interswitch_merchant_id,
            )
        )
        self._s.add(
            BusinessPaymentPolicyModel(
                business_id=business.id,
                monthly_txn_volume_range=monthly_txn_volume_range,
                avg_monthly_payouts_range=avg_monthly_payouts_range,
                primary_bank=primary_bank,
                risk_appetite=risk_appetite,
                merchant_state=merchant_state,
                daily_payout_limit=(
                    Decimal(str(daily_payout_limit)) if daily_payout_limit is not None else None
                ),
                single_payout_cap=(
                    Decimal(str(single_payout_cap)) if single_payout_cap is not None else None
                ),
                risk_alert_threshold=(
                    Decimal(str(risk_alert_threshold)) if risk_alert_threshold is not None else None
                ),
                liquidity_alert_buffer=(
                    Decimal(str(liquidity_alert_buffer)) if liquidity_alert_buffer is not None else None
                ),
            )
        )
        self._s.add(BusinessSecurityPolicyModel(business_id=business.id))

        if primary_use_cases:
            for uc in primary_use_cases:
                self._s.add(
                    BusinessUseCaseModel(
                        business_id=business.id,
                        use_case=str(uc)[:64],
                    )
                )

        # Virtual account is assigned after KYC is verified, not at creation.

        config = BusinessConfigModel(
            business_id=business.id,
            onboarding_step="complete",
            onboarding_completed_at=now,
        )
        self._s.add(config)

        member = BusinessMemberModel(
            business_id=business.id,
            user_id=owner_id,
            role="owner",
            joined_at=now,
        )
        self._s.add(member)

        await self._s.flush()
        return business, config, member

    async def get_by_id(self, business_id: uuid.UUID) -> BusinessModel | None:
        result = await self._s.execute(
            select(BusinessModel)
            .options(
                selectinload(BusinessModel.profile_row),
                selectinload(BusinessModel.address_row),
                selectinload(BusinessModel.virtual_accounts),
                selectinload(BusinessModel.payment_policy),
                selectinload(BusinessModel.security_policy),
                selectinload(BusinessModel.use_case_rows),
                selectinload(BusinessModel.config),
            )
            .where(BusinessModel.id == business_id)
        )
        return result.scalar_one_or_none()

    async def update(
        self, business_id: uuid.UUID, **kwargs: object
    ) -> BusinessModel | None:
        """Update mutable business fields. Only non-None kwargs are applied."""
        biz = await self.get_by_id(business_id)
        if biz is None:
            return None

        profile_fields = {
            "business_type",
            "rc_number",
            "tax_id",
            "website",
            "phone",
            "interswitch_merchant_id",
            "logo_url",
        }
        address_fields = {"city", "state", "country"}

        if biz.profile_row is None:
            biz.profile_row = BusinessProfileModel(business_id=biz.id)
            self._s.add(biz.profile_row)
        if biz.address_row is None:
            biz.address_row = BusinessAddressModel(business_id=biz.id)
            self._s.add(biz.address_row)

        for key, value in kwargs.items():
            if value is None:
                continue
            if key in profile_fields:
                setattr(biz.profile_row, key, value)
            elif key in address_fields:
                setattr(biz.address_row, key, value)
            elif key == "business_name":
                biz.business_name = str(value)

        biz.updated_at = datetime.now(timezone.utc)
        await self._s.flush()
        return biz

    async def update_config(
        self, business_id: uuid.UUID, **kwargs: object
    ) -> BusinessConfigModel | None:
        """Update onboarding config (``preferences``) and payment policy fields."""
        result = await self._s.execute(
            select(BusinessConfigModel)
            .where(BusinessConfigModel.business_id == business_id)
        )
        config = result.scalar_one_or_none()
        if config is None:
            return None

        biz_result = await self._s.execute(
            select(BusinessModel)
            .options(
                selectinload(BusinessModel.payment_policy),
                selectinload(BusinessModel.use_case_rows),
            )
            .where(BusinessModel.id == business_id)
        )
        biz = biz_result.scalar_one_or_none()
        if biz is None:
            return None

        if biz.payment_policy is None:
            biz.payment_policy = BusinessPaymentPolicyModel(business_id=biz.id)
            self._s.add(biz.payment_policy)
            await self._s.flush()

        config_allowed = {"preferences"}
        policy_allowed = {
            "monthly_txn_volume_range",
            "avg_monthly_payouts_range",
            "primary_bank",
            "risk_appetite",
            "default_risk_tolerance",
            "default_budget_cap",
            "merchant_state",
            "daily_payout_limit",
            "single_payout_cap",
            "risk_alert_threshold",
            "liquidity_alert_buffer",
        }

        for key, value in kwargs.items():
            if value is None:
                continue
            if key in config_allowed:
                setattr(config, key, value)
            elif key == "primary_use_cases" and isinstance(value, list):
                existing = {r.use_case for r in (biz.use_case_rows or [])}
                for item in value:
                    uc = str(item)[:64]
                    if uc not in existing:
                        self._s.add(BusinessUseCaseModel(business_id=biz.id, use_case=uc))
            elif key in policy_allowed:
                if key in (
                    "default_risk_tolerance",
                    "default_budget_cap",
                    "daily_payout_limit",
                    "single_payout_cap",
                    "risk_alert_threshold",
                    "liquidity_alert_buffer",
                ):
                    setattr(biz.payment_policy, key, Decimal(str(value)))
                else:
                    setattr(biz.payment_policy, key, value)

        config.updated_at = datetime.now(timezone.utc)
        if biz.payment_policy is not None:
            biz.payment_policy.updated_at = datetime.now(timezone.utc)
        await self._s.flush()
        return config
