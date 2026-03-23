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
        payload = {
            "merchantId": merchant_id,
            "startDate": start_date.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "endDate": end_date.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "status": statuses or ["SUCCESS", "PENDING", "FAILED"],
            "page": page,
            "pageSize": page_size,
            "currency": currency,
        }

        async with self._auth.get_client() as client:
            logger.info(f"Interswitch quick_search: {merchant_id} from {start_date} to {end_date}")
            response = await client.post("/transaction-search/quick-search", json=payload)
            response.raise_for_status()
            data = response.json()
            logger.info(f"Retrieved {data.get('totalCount', 0)} transactions")
            return data

    async def reference_search(
        self,
        transaction_reference: str,
        merchant_id: str,
    ) -> dict:
        payload = {
            "transactionReference": transaction_reference,
            "merchantId": merchant_id,
        }

        async with self._auth.get_client() as client:
            logger.info(f"Interswitch reference_search: {transaction_reference}")
            response = await client.post("/transaction-search/reference-search", json=payload)
            response.raise_for_status()
            return response.json()
