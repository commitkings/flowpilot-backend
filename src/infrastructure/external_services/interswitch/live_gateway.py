"""
LivePayoutGateway — delegates to real Interswitch PayoutClient + CustomerLookupClient.

Only used when PAYOUT_MODE=live and all wallet credentials are present.
"""

from src.infrastructure.external_services.interswitch.customer_lookup import CustomerLookupClient
from src.infrastructure.external_services.interswitch.payouts import PayoutClient
from src.infrastructure.external_services.interswitch.payout_gateway import PayoutGateway


class LivePayoutGateway(PayoutGateway):

    def __init__(self) -> None:
        self._lookup = CustomerLookupClient()
        self._payout = PayoutClient()

    @property
    def is_simulated(self) -> bool:
        return False

    async def lookup_customer(
        self,
        institution_code: str,
        account_number: str,
        transaction_reference: str,
        currency_code: str = "NGN",
    ) -> dict:
        return await self._lookup.lookup_customer(
            institution_code=institution_code,
            account_number=account_number,
            transaction_reference=transaction_reference,
            currency_code=currency_code,
        )

    async def execute_payout(
        self,
        batch_reference: str,
        items: list[dict],
        currency: str = "NGN",
    ) -> dict:
        return await self._payout.execute_payout(
            batch_reference=batch_reference,
            items=items,
            currency=currency,
        )

    async def requery_payout(
        self,
        transaction_reference: str,
    ) -> dict:
        return await self._payout.requery_payout(
            transaction_reference=transaction_reference,
        )
