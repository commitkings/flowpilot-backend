"""
HybridPayoutGateway — real Interswitch account verification, simulated payouts.

Used when PAYOUT_MODE=lookup_only:
  - lookup_customer → REAL call to Interswitch Bank Account Verification API
    (via API Marketplace routing, NOT the Payouts customer-lookup endpoint)
  - execute_payout  → SIMULATED (no real money moves)
  - requery_payout  → SIMULATED (instant success)

This gives you real account verification without the risk of moving funds.
"""

import logging

from src.infrastructure.external_services.interswitch.bank_account_verification import (
    BankAccountVerificationClient,
)
from src.infrastructure.external_services.interswitch.payout_gateway import PayoutGateway
from src.infrastructure.external_services.interswitch.simulated_gateway import SimulatedPayoutGateway

logger = logging.getLogger(__name__)


class HybridPayoutGateway(PayoutGateway):
    """Real account lookups via Interswitch BAV API, simulated payouts."""

    def __init__(self) -> None:
        self._bav = BankAccountVerificationClient()
        self._sim = SimulatedPayoutGateway()

    @property
    def is_simulated(self) -> bool:
        # Payouts are simulated, but lookups are real
        return True

    async def lookup_customer(
        self,
        institution_code: str,
        account_number: str,
        transaction_reference: str,
        currency_code: str = "NGN",
    ) -> dict:
        """REAL Interswitch Bank Account Verification — resolves account name."""
        logger.info(
            f"[HybridGateway] REAL BAV lookup: "
            f"bank={institution_code}, account={account_number}"
        )
        result = await self._bav.resolve_account(
            account_number=account_number,
            bank_code=institution_code,
        )
        # Add transactionReference for compatibility with the gateway interface
        result["transactionReference"] = transaction_reference
        return result

    async def execute_payout(
        self,
        batch_reference: str,
        items: list[dict],
        currency: str = "NGN",
    ) -> dict:
        """SIMULATED payout — no real money moves."""
        logger.info(
            f"[HybridGateway] SIMULATED execute_payout: "
            f"batch={batch_reference}, items={len(items)} (no real funds)"
        )
        return await self._sim.execute_payout(
            batch_reference=batch_reference,
            items=items,
            currency=currency,
        )

    async def requery_payout(
        self,
        transaction_reference: str,
    ) -> dict:
        """SIMULATED requery — instant success."""
        return await self._sim.requery_payout(
            transaction_reference=transaction_reference,
        )
