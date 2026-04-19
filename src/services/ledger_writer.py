"""Append-only central ledger rows (see docs/SCHEMA_REDESIGN_AND_PAYEE_PORTAL.md)."""

from __future__ import annotations

import secrets
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from src.infrastructure.database.flowpilot_models import LedgerEntryModel


def _ref(prefix: str) -> str:
    d = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"FP-{prefix}-{d}-{secrets.token_hex(4)}"


async def insert_ledger_entry(
    session: AsyncSession,
    *,
    entry_type: str,
    gross_amount: Decimal,
    net_amount: Decimal,
    direction: str,
    status: str,
    originator_type: str,
    beneficiary_type: str,
    business_id: Optional[uuid.UUID] = None,
    run_id: Optional[uuid.UUID] = None,
    fee_amount: Decimal = Decimal("0.00"),
    currency: str = "NGN",
    narration: Optional[str] = None,
    internal_narration: Optional[str] = None,
    originator_business_id: Optional[uuid.UUID] = None,
    originator_name: Optional[str] = None,
    beneficiary_bank_account_id: Optional[uuid.UUID] = None,
    beneficiary_payee_profile_id: Optional[uuid.UUID] = None,
    beneficiary_name: Optional[str] = None,
    beneficiary_account_number: Optional[str] = None,
    beneficiary_bank_name: Optional[str] = None,
    beneficiary_bank_code: Optional[str] = None,
    client_reference: Optional[str] = None,
    provider_reference: Optional[str] = None,
    source_table: Optional[str] = None,
    source_id: Optional[str] = None,
    purpose_code: Optional[str] = None,
    prefix: str = "GEN",
) -> int:
    """Insert one ledger row; returns bigint id."""
    row = LedgerEntryModel(
        internal_reference=_ref(prefix),
        client_reference=client_reference,
        provider_reference=provider_reference,
        entry_type=entry_type,
        gross_amount=gross_amount,
        fee_amount=fee_amount,
        net_amount=net_amount,
        currency=currency,
        direction=direction,
        status=status,
        originator_type=originator_type,
        originator_business_id=originator_business_id,
        originator_name=originator_name,
        beneficiary_type=beneficiary_type,
        beneficiary_bank_account_id=beneficiary_bank_account_id,
        beneficiary_payee_profile_id=beneficiary_payee_profile_id,
        beneficiary_name=beneficiary_name,
        beneficiary_account_number=beneficiary_account_number,
        beneficiary_bank_name=beneficiary_bank_name,
        beneficiary_bank_code=beneficiary_bank_code,
        narration=narration,
        internal_narration=internal_narration,
        business_id=business_id,
        run_id=run_id,
        purpose_code=purpose_code,
        source_table=source_table,
        source_id=source_id,
        initiated_at=datetime.now(timezone.utc),
        completed_at=datetime.now(timezone.utc) if status == "completed" else None,
    )
    session.add(row)
    await session.flush()
    return int(row.id)
