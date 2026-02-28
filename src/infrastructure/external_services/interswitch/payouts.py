"""
Interswitch Payouts — Account Credit, Requery, and Institutions.

Uses the Quickteller Transfer Service for:
  - AccountCredit:  Execute a payout (requires transactionReference from CreditInquiry)
  - Requery:        Check payout status
  - Institutions:   Fetch list of receiving banks/institutions
"""

import logging
from typing import Optional

from src.config.settings import Settings
from src.infrastructure.external_services.interswitch.auth import InterswitchAuth

logger = logging.getLogger(__name__)


def _ngn_to_kobo(amount_ngn: float) -> int:
    """Convert NGN amount to minor denomination (kobo)."""
    return int(round(amount_ngn * 100))


class PayoutClient:

    def __init__(self) -> None:
        self._auth = InterswitchAuth()

    async def get_receiving_institutions(self) -> dict:
        """Fetch supported banks/institutions from Interswitch."""
        async with await self._auth.get_resilient_client() as client:
            logger.info("Fetching receiving institutions from Interswitch")
            response = await client.get(
                "/quicktellerservice/api/v5/institutions",
            )
            return response.json()

    async def execute_single_payout(
        self,
        transaction_reference: str,
        amount_ngn: float,
        narration: str,
        client_ref: str,
    ) -> dict:
        """Execute a single payout via Interswitch AccountCredit.

        This is a per-item operation — each candidate requires a separate
        call.  The transactionReference MUST come from a prior
        CreditInquiry call.

        Args:
            transaction_reference: From CreditInquiry response.
            amount_ngn: Payout amount in NGN (converted to kobo internally).
            narration: Payment narration/description.
            client_ref: Unique client reference for idempotency tracking.

        Returns:
            dict with Interswitch credit response fields.
        """
        payload = {
            "transactionAmount": _ngn_to_kobo(amount_ngn),
            "narration": narration,
            "clientRef": client_ref,
            "transactionReference": transaction_reference,
            "terminalId": Settings.INTERSWITCH_TERMINAL_ID,
        }

        async with await self._auth.get_resilient_client() as client:
            logger.info(
                f"AccountCredit: ref={transaction_reference[:20]}..., "
                f"amount={amount_ngn} NGN ({_ngn_to_kobo(amount_ngn)} kobo), "
                f"clientRef={client_ref}"
            )
            response = await client.post(
                "/quicktellerservice/api/v5/transactions/AccountCredit",
                json=payload,
            )
            data = response.json()
            logger.info(f"AccountCredit result: {data.get('responseCode', '?')} — {data.get('responseMessage', '?')}")
            return data

    async def requery_payout(
        self,
        client_ref: str,
        transaction_reference: Optional[str] = None,
    ) -> dict:
        """Check payout status via Interswitch Requery.

        Args:
            client_ref: The clientRef used in the original AccountCredit call.
            transaction_reference: Optional transaction reference for lookup.

        Returns:
            dict with status, responseCode, settlementStatus, etc.
        """
        payload = {"clientRef": client_ref}
        if transaction_reference:
            payload["transactionReference"] = transaction_reference

        async with await self._auth.get_resilient_client() as client:
            logger.info(f"Requery: clientRef={client_ref}")
            response = await client.post(
                "/quicktellerservice/api/v5/transactions/Requery",
                json=payload,
            )
            data = response.json()
            logger.info(f"Requery result: status={data.get('status', '?')}")
            return data

    # ------------------------------------------------------------------
    # Legacy batch method — kept for backward compat but now delegates
    # to per-item execute_single_payout internally.
    # ------------------------------------------------------------------

    async def execute_payout(
        self,
        batch_reference: str,
        items: list[dict],
        currency: str = "NGN",
        source_account_id: Optional[str] = None,
    ) -> dict:
        """Execute payouts for a batch of items (sequentially per-item).

        Each item dict must contain:
            transaction_reference (str): From prior CreditInquiry
            amount (float): In NGN
            client_reference (str): Unique per-item ref
            narration (str, optional)
        """
        results = []
        accepted = 0
        rejected = 0

        for item in items:
            try:
                result = await self.execute_single_payout(
                    transaction_reference=item["transaction_reference"],
                    amount_ngn=item["amount"],
                    narration=item.get("narration", "FlowPilot Payout"),
                    client_ref=item["client_reference"],
                )
                resp_code = result.get("responseCode", "")
                status = "PENDING" if resp_code == "00" or resp_code == "09" else "FAILED"
                results.append({
                    "clientReference": item["client_reference"],
                    "providerReference": result.get("transactionReference", ""),
                    "status": status,
                    "responseCode": resp_code,
                    "responseMessage": result.get("responseMessage", ""),
                })
                if status != "FAILED":
                    accepted += 1
                else:
                    rejected += 1
            except Exception as e:
                logger.error(f"Payout failed for {item.get('client_reference')}: {e}")
                results.append({
                    "clientReference": item.get("client_reference", ""),
                    "providerReference": "",
                    "status": "FAILED",
                    "responseCode": "XX",
                    "responseMessage": str(e),
                })
                rejected += 1

        return {
            "batchReference": batch_reference,
            "submissionStatus": "ACCEPTED" if accepted > 0 else "REJECTED",
            "acceptedCount": accepted,
            "rejectedCount": rejected,
            "items": results,
        }

    async def get_payout_status(self, provider_reference: str) -> dict:
        """Check status of a single payout (delegates to requery)."""
        return await self.requery_payout(client_ref=provider_reference)
