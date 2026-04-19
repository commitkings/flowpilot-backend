from __future__ import annotations

from dataclasses import asdict
from typing import Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from src.infrastructure.database.flowpilot_models import (
    BusinessMemberModel,
    BusinessModel,
    IndividualKycSubmissionModel,
    PayoutCandidateModel,
    PayoutComplianceRecordModel,
    UserModel,
)
from src.services.contracts import TravelRulePayload


class TravelRuleViolationError(Exception):
    pass


class ComplianceService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def build_payload(
        self, *, business_id: UUID, candidate_id: UUID
    ) -> TravelRulePayload:
        biz = (
            await self.session.execute(
                select(BusinessModel)
                .options(selectinload(BusinessModel.address_row))
                .where(BusinessModel.id == business_id)
            )
        ).scalar_one()
        candidate = (
            await self.session.execute(
                select(PayoutCandidateModel).where(PayoutCandidateModel.id == candidate_id)
            )
        ).scalar_one()
        owner_row = (
            await self.session.execute(
                select(UserModel)
                .join(BusinessMemberModel, BusinessMemberModel.user_id == UserModel.id)
                .where(
                    BusinessMemberModel.business_id == business_id,
                    BusinessMemberModel.role == "owner",
                    BusinessMemberModel.is_active.is_(True),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        kyc = (
            await self.session.execute(
                select(IndividualKycSubmissionModel).where(
                    IndividualKycSubmissionModel.business_id == business_id
                )
            )
        ).scalar_one_or_none()

        originator_name = (
            owner_row.display_name if owner_row and owner_row.display_name else biz.business_name
        )
        originator_bvn = (
            (kyc.level_1_value if kyc and kyc.level_1_type == "bvn" else "") or ""
        )
        originator_address = ", ".join(
            p for p in [biz.city, biz.state, biz.country] if p
        )

        return TravelRulePayload(
            originator_name=originator_name or "",
            originator_wallet_id=str(business_id),
            originator_bvn=originator_bvn,
            originator_address=originator_address,
            beneficiary_name=candidate.beneficiary_name or "",
            beneficiary_account_number=candidate.account_number or "",
            beneficiary_bank_code=candidate.institution_code or "",
        )

    @staticmethod
    def validate_payload(payload: TravelRulePayload) -> list[str]:
        # originator_bvn is optional: businesses that verified via NIN won't have one
        OPTIONAL_FIELDS = {"beneficiary_bank_name", "beneficiary_address", "originator_bvn"}
        missing: list[str] = []
        for field, value in asdict(payload).items():
            if field in OPTIONAL_FIELDS:
                continue
            if not value:
                missing.append(field)
        return missing

    async def enforce_and_record(
        self,
        *,
        run_id: UUID,
        business_id: UUID,
        candidate_id: UUID,
        payload: Optional[TravelRulePayload] = None,
    ) -> None:
        payload = payload or await self.build_payload(
            business_id=business_id, candidate_id=candidate_id
        )
        missing = self.validate_payload(payload)
        status = "blocked" if missing else "passed"
        reason = ", ".join(missing) if missing else None
        existing = (
            await self.session.execute(
                select(PayoutComplianceRecordModel).where(
                    PayoutComplianceRecordModel.candidate_id == candidate_id
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            existing = PayoutComplianceRecordModel(
                run_id=run_id,
                candidate_id=candidate_id,
                business_id=business_id,
                **asdict(payload),
                validation_status=status,
                blocking_reason=reason,
            )
            self.session.add(existing)
        else:
            for key, value in asdict(payload).items():
                setattr(existing, key, value)
            existing.validation_status = status
            existing.blocking_reason = reason
        await self.session.flush()

        if missing:
            raise TravelRuleViolationError(
                "Travel Rule validation failed: missing "
                + ", ".join(missing)
            )
