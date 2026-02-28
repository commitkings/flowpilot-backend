"""
ExecutionDetailRepository — persists per-API-call detail records.

Writes to:
  - customer_lookup_result  (CreditInquiry call details)
  - payout_execution        (AccountCredit submission + Requery poll details)
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from src.infrastructure.database.flowpilot_models import (
    CustomerLookupResultModel,
    PayoutExecutionModel,
)


class ExecutionDetailRepository:
    """Persists per-API-call detail records for execution audit trail."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_lookup_result(
        self,
        candidate_id: UUID,
        run_id: UUID,
        account_number: str,
        institution_code: str,
        can_credit: bool | None = None,
        name_returned: str | None = None,
        similarity_score: Decimal | None = None,
        transaction_reference: str | None = None,
        http_status_code: int = 200,
        response_message: str | None = None,
        raw_response: dict | None = None,
        attempt_number: int = 1,
        duration_ms: int = 0,
        called_at: datetime | None = None,
    ) -> CustomerLookupResultModel:
        model = CustomerLookupResultModel(
            candidate_id=candidate_id,
            run_id=run_id,
            account_number=account_number,
            institution_code=institution_code,
            can_credit=can_credit,
            name_returned=name_returned,
            similarity_score=similarity_score,
            transaction_reference=transaction_reference,
            http_status_code=http_status_code,
            response_message=response_message,
            raw_response=raw_response or {},
            attempt_number=attempt_number,
            duration_ms=duration_ms,
            called_at=called_at or datetime.utcnow(),
        )
        self._session.add(model)
        await self._session.flush()
        return model

    async def create_payout_execution(
        self,
        candidate_id: UUID,
        run_id: UUID,
        submission_type: str,
        http_status_code: int = 200,
        interswitch_reference: str | None = None,
        response_message: str | None = None,
        execution_status: str = "pending",
        raw_response: dict | None = None,
        attempt_number: int = 1,
        duration_ms: int = 0,
        called_at: datetime | None = None,
    ) -> PayoutExecutionModel:
        model = PayoutExecutionModel(
            candidate_id=candidate_id,
            run_id=run_id,
            submission_type=submission_type,
            http_status_code=http_status_code,
            interswitch_reference=interswitch_reference,
            response_message=response_message,
            execution_status=execution_status,
            raw_response=raw_response or {},
            attempt_number=attempt_number,
            duration_ms=duration_ms,
            called_at=called_at or datetime.utcnow(),
        )
        self._session.add(model)
        await self._session.flush()
        return model
