import logging

from src.infrastructure.external_services.interswitch.auth import InterswitchAuth

logger = logging.getLogger(__name__)


class CustomerLookupClient:

    def __init__(self) -> None:
        self._auth = InterswitchAuth()

    async def lookup_customer(
        self,
        institution_code: str,
        account_number: str,
        currency: str = "NGN",
    ) -> dict:
        payload = {
            "institutionCode": institution_code,
            "accountNumber": account_number,
            "currency": currency,
        }

        async with self._auth.get_resilient_client() as client:
            logger.info(f"Customer lookup: {institution_code}/{account_number}")
            response = await client.post("/api/v1/payouts/customer-lookup", json=payload)
            data = response.json()
            logger.info(f"Lookup result: {data.get('lookupStatus')}")
            return data
