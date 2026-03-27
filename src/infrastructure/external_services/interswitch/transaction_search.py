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
from typing import Any, Optional

import httpx

from src.config.settings import Settings
from src.infrastructure.external_services.interswitch.auth import InterswitchAuth

logger = logging.getLogger(__name__)

_QUICK_SEARCH_PATH = "/switch-online-gateway-service/api/v1/gateway/quick-search"


class TransactionSearchClient:

    @staticmethod
    def _normalize_transaction(item: dict[str, Any]) -> dict[str, Any]:
        """Map Transaction Search payloads to the shape used across FlowPilot."""
        amount = item.get("amount", item.get("transactionAmount"))
        try:
            normalized_amount = float(amount) if amount is not None else None
        except (TypeError, ValueError):
            normalized_amount = None

        status = item.get("status", item.get("transactionStatus"))
        if isinstance(status, str):
            status = status.upper()

        return {
            **item,
            "transactionReference": item.get("transactionReference")
            or item.get("uniqueReference")
            or item.get("reference"),
            "amount": normalized_amount,
            "status": status,
            "channel": item.get("channel") or item.get("paymentChannel"),
            "timestamp": item.get("timestamp")
            or item.get("transactionDate")
            or item.get("paymentDate"),
            "settlementDate": item.get("settlementDate") or item.get("settlement_date"),
            "counterpartyName": item.get("counterpartyName")
            or item.get("customerName")
            or item.get("accountName"),
            "counterpartyBank": item.get("counterpartyBank")
            or item.get("bankName")
            or item.get("bankCode"),
            "currency": item.get("currency") or item.get("currencyCode") or "NGN",
            "direction": item.get("direction", "inflow"),
        }

    def _normalize_search_response(self, payload: dict[str, Any]) -> dict[str, Any]:
        records = payload.get("transactions")
        if records is None:
            records = payload.get("data")
        if not isinstance(records, list):
            records = []

        normalized = [self._normalize_transaction(item) for item in records if isinstance(item, dict)]
        return {
            **payload,
            "transactions": normalized,
            "total_count": payload.get("dataSize", payload.get("total_count", len(normalized))),
        }

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
        merchant_code: Optional[str] = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        terminal_id: Optional[str] = None,
        to_account: Optional[str] = None,
        transaction_amount: Optional[int] = None,
        cursor: Optional[str] = None,
        statuses: Optional[list[str]] = None,
        page: Optional[int] = None,
        page_size: Optional[int] = None,
        merchant_id: Optional[str] = None,
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
        merchant = merchant_code or merchant_id
        if not merchant:
            raise ValueError("merchant_code is required for transaction search")
        if start_date is None or end_date is None:
            raise ValueError("start_date and end_date are required for transaction search")

        payload: dict[str, Any] = {
            "merchant_code": merchant,
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
        if statuses:
            payload["status"] = ",".join(statuses)
        if page is not None:
            payload["page"] = page
        if page_size is not None:
            payload["page_size"] = page_size

        url = f"{self._base_url}{_QUICK_SEARCH_PATH}"
        headers = await self._auth.get_headers()
        if Settings.INTERSWITCH_CLIENT_ID:
            headers["ClientId"] = Settings.INTERSWITCH_CLIENT_ID

        logger.info(
            f"TransactionSearch quick_search: merchant={merchant}, "
            f"{start_date} to {end_date}, url={url}"
        )

        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
            response = await client.post(url, json=payload, headers=headers)
            data = response.json()

            logger.info(
                f"TransactionSearch result: status={response.status_code}, "
                f"responseCode={data.get('responseCode')}, "
                f"dataSize={data.get('dataSize', 0)}"
            )
            return self._normalize_search_response(data)

    async def reference_search(
        self,
        unique_reference: Optional[str] = None,
        merchant_code: Optional[str] = None,
        transaction_reference: Optional[str] = None,
        merchant_id: Optional[str] = None,
    ) -> dict:
        """Lookup a single transaction by its unique reference.

        Uses the same quick_search endpoint with unique_reference filter,
        as Interswitch does not expose a separate reference-search path
        in the Transaction Search API docs.
        """
        reference = unique_reference or transaction_reference
        merchant = merchant_code or merchant_id
        if not reference:
            raise ValueError("unique_reference is required for transaction search")
        if not merchant:
            raise ValueError("merchant_code is required for transaction search")

        payload = {
            "unique_reference": reference,
            "merchant_code": merchant,
        }

        url = f"{self._base_url}{_QUICK_SEARCH_PATH}"
        headers = await self._auth.get_headers()
        if Settings.INTERSWITCH_CLIENT_ID:
            headers["ClientId"] = Settings.INTERSWITCH_CLIENT_ID

        logger.info(f"TransactionSearch reference_search: ref={reference}")

        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
            response = await client.post(url, json=payload, headers=headers)
            data = response.json()
            normalized = self._normalize_search_response(data).get("transactions", [])
            return normalized[0] if normalized else data
