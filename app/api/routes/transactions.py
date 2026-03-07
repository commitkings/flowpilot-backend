"""GET /transactions — list & summarise reconciled transactions."""
from __future__ import annotations

import datetime
import logging
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth.dependencies import get_current_user
from src.infrastructure.database.connection import get_db_session
from src.infrastructure.database.repositories import TransactionRepository

logger = logging.getLogger(__name__)
router = APIRouter()


def _serialize_transaction(txn) -> dict:
    """Map a ReconciledTransactionModel to the shape the frontend expects."""
    # Determine anomaly label
    if txn.has_anomaly:
        anomaly = (
            txn.anomalies[0].anomaly_type
            if getattr(txn, "anomalies", None)
            else "Flagged"
        )
    else:
        anomaly = "Clean"

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
    }


def _parse_date(value: Optional[str]) -> Optional[datetime.date]:
    if value is None:
        return None
    return datetime.date.fromisoformat(value)


@router.get("/transactions")
async def list_transactions(
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
    """Return paginated transactions with summary metrics."""
    repo = TransactionRepository(session)

    filter_kwargs = dict(
        status=status,
        channel=channel,
        search=search,
        from_date=_parse_date(from_date),
        to_date=_parse_date(to_date),
    )

    transactions, total = await repo.list_all(**filter_kwargs, limit=limit, offset=offset)
    summary = await repo.get_summary(**filter_kwargs)

    return {
        "transactions": [_serialize_transaction(t) for t in transactions],
        "total": total,
        "limit": limit,
        "offset": offset,
        "summary": summary,
    }
