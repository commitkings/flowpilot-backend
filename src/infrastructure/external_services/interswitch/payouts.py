import logging
from typing import Optional

from src.infrastructure.external_services.interswitch.auth import InterswitchAuth
from src.config.settings import Settings

logger = logging.getLogger(__name__)


class PayoutClient:

    def __init__(self) -> None:
        self._auth = InterswitchAuth()

    async def get_receiving_institutions(self) -> dict:
        async with self._auth.get_resilient_client() as client:
            logger.info("Fetching receiving institutions")
            response = await client.get("/api/v1/payouts/receiving-institutions")
            return response.json()

    async def execute_payout(
        self,
        batch_reference: str,
        items: list[dict],
        currency: str = "NGN",
        source_account_id: Optional[str] = None,
    ) -> dict:
        payload = {
            "batchReference": batch_reference,
            "currency": currency,
            "sourceAccountId": source_account_id or Settings.INTERSWITCH_SOURCE_ACCOUNT_ID,
            "items": [
                {
                    "clientReference": item["client_reference"],
                    "amount": item["amount"],
                    "institutionCode": item["institution_code"],
                    "accountNumber": item["account_number"],
                    "accountName": item["account_name"],
                    "narration": item.get("narration", ""),
                }
                for item in items
            ],
        }

        async with self._auth.get_resilient_client() as client:
            logger.info(f"Executing payout batch: {batch_reference} ({len(items)} items)")
            response = await client.post("/api/v1/payouts", json=payload)
            data = response.json()
            logger.info(f"Payout submission: {data.get('submissionStatus')}, accepted: {data.get('acceptedCount')}")
            return data

    async def get_payout_status(self, provider_reference: str) -> dict:
        async with self._auth.get_resilient_client() as client:
            logger.info(f"Checking payout status: {provider_reference}")
            response = await client.get(f"/api/v1/payouts/{provider_reference}")
            return response.json()
