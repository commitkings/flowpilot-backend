"""GET /transactions — list & summarise reconciled transactions + payout records."""
from __future__ import annotations

import datetime
import logging
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth.dependencies import get_current_user
from src.infrastructure.database.connection import get_db_session
from src.infrastructure.database.repositories import (
    CandidateRepository,
    TransactionRepository,
)

logger = logging.getLogger(__name__)
router = APIRouter()

_EXEC_STATUS_MAP = {
    "success": "SUCCESS",
    "pending": "PENDING",
    "failed": "FAILED",
    "not_started": "PENDING",
    "requires_followup": "PENDING",
}


def _serialize_transaction(txn) -> dict:
    """Map a ReconciledTransactionModel to the shape the frontend expects."""
    anomaly = "Flagged" if txn.has_anomaly else "Clean"

    return {
        "id": str(txn.id),
        "run_id": str(txn.run_id),
        "reference": txn.interswitch_ref,
        "channel": txn.channel or "",
        "amount": float(txn.amount),
        "currency": txn.currency or "NGN",
        "direction": txn.direction or "",
        "status": txn.status or "",
        "narration": txn.narration or "",
        "counterparty_name": txn.counterparty_name or "",
        "counterparty_bank": txn.counterparty_bank or "",
        "date": txn.transaction_timestamp.isoformat() if txn.transaction_timestamp else None,
        "settlement_date": txn.settlement_date.isoformat() if txn.settlement_date else None,
        "anomaly": anomaly,
        "anomaly_count": txn.anomaly_count or 0,
        "record_type": "reconciled",
    }


def _serialize_candidate_as_transaction(candidate) -> dict:
    """Map an approved/executed PayoutCandidateModel to the transaction row shape."""
    status = _EXEC_STATUS_MAP.get(candidate.execution_status, "PENDING")
    return {
        "id": str(candidate.id),
        "run_id": str(candidate.run_id),
        "reference": candidate.client_reference or candidate.provider_reference or str(candidate.id)[:12],
        "channel": "PAYOUT",
        "amount": float(candidate.amount),
        "currency": candidate.currency or "NGN",
        "direction": "OUTFLOW",
        "status": status,
        "narration": candidate.purpose or "",
        "counterparty_name": candidate.beneficiary_name or "",
        "counterparty_bank": candidate.institution_code or "",
        "date": candidate.executed_at.isoformat() if candidate.executed_at else candidate.created_at.isoformat(),
        "settlement_date": None,
        "anomaly": "Clean",
        "anomaly_count": 0,
        "record_type": "payout",
    }


def _parse_date(value: Optional[str]) -> Optional[datetime.date]:
    if value is None:
        return None
    return datetime.date.fromisoformat(value)


@router.get("/transactions")
async def list_transactions(
    run_id: Optional[str] = Query(None, description="Filter by run UUID"),
    status: Optional[str] = Query(None, description="Filter: SUCCESS | PENDING | FAILED | REVERSED"),
    channel: Optional[str] = Query(None, description="Filter: CARD | TRANSFER | USSD | QR"),
    search: Optional[str] = Query(None, description="Substring match on interswitch_ref"),
    from_date: Optional[str] = Query(None, description="ISO date YYYY-MM-DD"),
    to_date: Optional[str] = Query(None, description="ISO date YYYY-MM-DD"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_db_session),
    current_user=Depends(get_current_user),
):
    """Return paginated transactions with summary metrics.

    When a run_id is provided: returns reconciled transactions merged with
    payout execution records from approved candidates, giving a complete
    picture of all financial activity for the run.
    """
    repo = TransactionRepository(session)
    run_uuid = UUID(run_id) if run_id else None

    filter_kwargs = dict(
        run_id=run_uuid,
        status=status,
        channel=channel,
        search=search,
        from_date=_parse_date(from_date),
        to_date=_parse_date(to_date),
    )

    transactions, total = await repo.list_all(**filter_kwargs, limit=limit, offset=offset)
    summary = await repo.get_summary(**filter_kwargs)

    serialized = [_serialize_transaction(t) for t in transactions]

    # When scoped to a run, also include approved/executed payout candidates
    # so successful runs always show their payment activity.
    payout_rows: list[dict] = []
    if run_uuid is not None:
        candidate_repo = CandidateRepository(session)
        approved = await candidate_repo.get_by_run(run_uuid, approval_status="approved")
        for c in approved:
            payout_rows.append(_serialize_candidate_as_transaction(c))

        if payout_rows:
            # Merge: deduplicate by reference overlap, then combine
            existing_refs = {row["reference"] for row in serialized}
            unique_payouts = [p for p in payout_rows if p["reference"] not in existing_refs]
            serialized.extend(unique_payouts)

            # Recompute summary to reflect combined data
            payout_total = sum(p["amount"] for p in unique_payouts)
            payout_success = sum(1 for p in unique_payouts if p["status"] == "SUCCESS")
            payout_failed = sum(1 for p in unique_payouts if p["status"] == "FAILED")
            summary["total_transactions"] += len(unique_payouts)
            summary["total_volume"] += payout_total
            summary["failed_count"] += payout_failed
            total += len(unique_payouts)

    return {
        "transactions": serialized,
        "total": total,
        "limit": limit,
        "offset": offset,
        "summary": summary,
    }
