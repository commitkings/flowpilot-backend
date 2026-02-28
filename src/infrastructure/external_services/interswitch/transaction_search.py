"""
Interswitch Transaction Search — Quick Search and Reference Search.

NOTE: These endpoints may be partner-specific / custom.  The paths below
follow the contract defined in the HACKATHON.md architecture doc.  If the
sandbox returns 404, the ReconciliationAgent falls back gracefully.
"""

import logging
from datetime import datetime
from typing import Optional

from src.infrastructure.external_services.interswitch.auth import InterswitchAuth

logger = logging.getLogger(__name__)


class TransactionSearchClient:

    def __init__(self) -> None:
        self._auth = InterswitchAuth()

    async def quick_search(
        self,
        merchant_id: str,
        start_date: datetime,
        end_date: datetime,
        statuses: Optional[list[str]] = None,
        page: int = 1,
        page_size: int = 100,
        currency: str = "NGN",
    ) -> dict:
        """Search transactions by date range and optional status filter."""
        payload = {
            "merchantId": merchant_id,
            "startDate": start_date.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "endDate": end_date.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "status": statuses or ["SUCCESS", "PENDING", "FAILED"],
            "page": page,
            "pageSize": page_size,
            "currency": currency,
        }

        async with await self._auth.get_resilient_client() as client:
            logger.info(f"Interswitch quick_search: {merchant_id} from {start_date} to {end_date}")
            response = await client.post("/transaction-search/quick-search", json=payload)
            data = response.json()
            logger.info(f"Retrieved {data.get('totalCount', 0)} transactions")
            return data

    async def reference_search(
        self,
        transaction_reference: str,
        merchant_id: str,
    ) -> dict:
        """Lookup a single transaction by its reference."""
        payload = {
            "transactionReference": transaction_reference,
            "merchantId": merchant_id,
        }

        async with await self._auth.get_resilient_client() as client:
            logger.info(f"Interswitch reference_search: {transaction_reference}")
            response = await client.post("/transaction-search/reference-search", json=payload)
            return response.json()
