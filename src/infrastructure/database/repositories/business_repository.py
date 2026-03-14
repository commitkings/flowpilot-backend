"""
Business repository — create business with owner for onboarding flow.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from src.infrastructure.database.flowpilot_models import (
    BusinessConfigModel,
    BusinessMemberModel,
    BusinessModel,
)


class BusinessRepository:

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def create_with_owner(
        self,
        *,
        owner_id: uuid.UUID,
        business_name: str,
        business_type: str | None = None,
        monthly_txn_volume_range: str | None = None,
        avg_monthly_payouts_range: str | None = None,
        primary_bank: str | None = None,
        primary_use_cases: list[str] | None = None,
        risk_appetite: str | None = None,
    ) -> tuple[BusinessModel, BusinessConfigModel, BusinessMemberModel]:
        """Create a business, its config, and assign the caller as owner.

        All three inserts happen in the same flush (single transaction).
        """
        now = datetime.now(timezone.utc)

        business = BusinessModel(
            business_name=business_name,
            business_type=business_type,
        )
        self._s.add(business)
        await self._s.flush()  # assigns business.id

        config = BusinessConfigModel(
            business_id=business.id,
            onboarding_step="complete",
            onboarding_completed_at=now,
            monthly_txn_volume_range=monthly_txn_volume_range,
            avg_monthly_payouts_range=avg_monthly_payouts_range,
            primary_bank=primary_bank,
            primary_use_cases=primary_use_cases,
            risk_appetite=risk_appetite,
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
        from sqlalchemy import select

        result = await self._s.execute(
            select(BusinessModel).where(BusinessModel.id == business_id)
        )
        return result.scalar_one_or_none()

    async def update(
        self, business_id: uuid.UUID, **kwargs: object
    ) -> BusinessModel | None:
        """Update mutable business fields. Only non-None kwargs are applied."""
        biz = await self.get_by_id(business_id)
        if biz is None:
            return None
        allowed = {
            "business_name", "business_type", "rc_number", "tax_id",
            "city", "state", "country", "website", "phone",
        }
        for key, value in kwargs.items():
            if key in allowed and value is not None:
                setattr(biz, key, value)
        biz.updated_at = datetime.now(timezone.utc)
        await self._s.flush()
        return biz

    async def update_config(
        self, business_id: uuid.UUID, **kwargs: object
    ) -> BusinessConfigModel | None:
        """Update mutable business config fields."""
        from sqlalchemy import select

        result = await self._s.execute(
            select(BusinessConfigModel).where(
                BusinessConfigModel.business_id == business_id
            )
        )
        config = result.scalar_one_or_none()
        if config is None:
            return None
        allowed = {
            "monthly_txn_volume_range", "avg_monthly_payouts_range",
            "primary_bank", "primary_use_cases", "risk_appetite",
            "default_risk_tolerance", "default_budget_cap", "preferences",
        }
        for key, value in kwargs.items():
            if key in allowed and value is not None:
                setattr(config, key, value)
        config.updated_at = datetime.now(timezone.utc)
        await self._s.flush()
        return config
