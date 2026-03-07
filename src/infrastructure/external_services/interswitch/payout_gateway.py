"""
Abstract payout gateway — decouples ExecutionAgent from a specific provider.

Two implementations exist:
  - LivePayoutGateway:      delegates to Interswitch PayoutClient + CustomerLookupClient
  - SimulatedPayoutGateway: returns deterministic demo-safe responses (no network)
"""

from __future__ import annotations

import abc


class PayoutGateway(abc.ABC):
    """Transport-agnostic interface for customer lookup, payout, and status."""

    @property
    @abc.abstractmethod
    def is_simulated(self) -> bool:
        """True when no real funds will move."""

    @abc.abstractmethod
    async def lookup_customer(
        self,
        institution_code: str,
        account_number: str,
        transaction_reference: str,
        currency_code: str = "NGN",
    ) -> dict:
        """Verify a beneficiary account before payout.

        Returns dict with at minimum:
            lookupStatus  (str)  — "SUCCESS" or "FAILED"
            canCredit     (bool)
            accountName   (str)
            accountNumber (str)
            institutionCode (str)
            transactionReference (str)
            raw_response  (dict)
        """

    @abc.abstractmethod
    async def execute_payout(
        self,
        batch_reference: str,
        items: list[dict],
        currency: str = "NGN",
    ) -> dict:
        """Submit a batch of payout items.

        Each item dict must contain:
            transaction_reference, amount, account_number,
            institution_code, client_reference, narration

        Returns dict with:
            batchReference, submissionStatus, acceptedCount,
            rejectedCount, items
        """

    @abc.abstractmethod
    async def requery_payout(
        self,
        transaction_reference: str,
    ) -> dict:
        """Poll the final status of a submitted payout item.

        Returns dict with at minimum:
            status (str) — "SUCCESSFUL" | "FAILED" | "PROCESSING"
        """
