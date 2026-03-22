"""
Interswitch Transaction Search — Quick Search and Reference Search.

Uses the Transaction Search API subscribed via the Developer Marketplace.
Docs: https://docs.interswitchgroup.com/docs/quick-search

Base URL:  INTERSWITCH_TRANSACTION_SEARCH_BASE_URL
           (default: https://switch-online-gateway-service.k9.isw.la)
Auth:      INTERSWITCH_TRANSACTION_SEARCH_PASSPORT_URL
           (default: https://passport-v2.k8.isw.la)
"""

import logging
from datetime import datetime
from typing import Optional

import httpx

from src.config.settings import Settings
from src.infrastructure.external_services.interswitch.auth import InterswitchAuth

logger = logging.getLogger(__name__)

_QUICK_SEARCH_PATH = "/switch-online-gateway-service/api/v1/gateway/quick-search"


class TransactionSearchClient:

    def __init__(self) -> None:
        # Transaction Search has its OWN passport instance
        passport_url = getattr(
            Settings,
            "INTERSWITCH_TRANSACTION_SEARCH_PASSPORT_URL",
            "https://passport-v2.k8.isw.la",
        )
        self._auth = InterswitchAuth(base_url=passport_url)
        self._base_url = Settings.INTERSWITCH_TRANSACTION_SEARCH_BASE_URL.rstrip("/")

    async def quick_search(
        self,
        merchant_code: str,
        start_date: datetime,
        end_date: datetime,
        terminal_id: Optional[str] = None,
        to_account: Optional[str] = None,
        transaction_amount: Optional[int] = None,
        cursor: Optional[str] = None,
    ) -> dict:
        """Search transactions by date range and optional filters.

        Args:
            merchant_code: Interswitch merchant code (e.g. 'MX272008').
            start_date: Search range start (YYYY-MM-DD).
            end_date: Search range end (YYYY-MM-DD).
            terminal_id: Optional terminal ID filter.
            to_account: Optional beneficiary account filter.
            transaction_amount: Optional amount in lower denomination (kobo).
            cursor: Pagination cursor from a previous response.

        Returns:
            dict with responseCode, responseMessage, data[], dataSize, etc.
        """
        payload: dict = {
            "merchant_code": merchant_code,
            "start_date": start_date.strftime("%Y-%m-%d"),
            "end_date": end_date.strftime("%Y-%m-%d"),
        }

        if terminal_id:
            payload["terminal_id"] = terminal_id
        if to_account:
            payload["to_account"] = to_account
        if transaction_amount is not None:
            payload["transaction_amount"] = transaction_amount
        if cursor:
            payload["cursor"] = cursor

        url = f"{self._base_url}{_QUICK_SEARCH_PATH}"
        headers = await self._auth.get_headers()

        logger.info(
            f"TransactionSearch quick_search: merchant={merchant_code}, "
            f"{start_date.date()} to {end_date.date()}, url={url}"
        )

        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
            response = await client.post(url, json=payload, headers=headers)
            data = response.json()

            logger.info(
                f"TransactionSearch result: status={response.status_code}, "
                f"responseCode={data.get('responseCode')}, "
                f"dataSize={data.get('dataSize', 0)}"
            )
            return data

    async def reference_search(
        self,
        unique_reference: str,
        merchant_code: str,
    ) -> dict:
        """Lookup a single transaction by its unique reference.

        Uses the same quick_search endpoint with unique_reference filter,
        as Interswitch does not expose a separate reference-search path
        in the Transaction Search API docs.
        """
        payload = {
            "unique_reference": unique_reference,
            "merchant_code": merchant_code,
        }

        url = f"{self._base_url}{_QUICK_SEARCH_PATH}"
        headers = await self._auth.get_headers()

        logger.info(f"TransactionSearch reference_search: ref={unique_reference}")

        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
            response = await client.post(url, json=payload, headers=headers)
            return response.json()
