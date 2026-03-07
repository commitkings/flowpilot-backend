"""
SimulatedPayoutGateway — demo-safe payout transport that never contacts Interswitch.

Returns deterministic, realistic responses that exercise the full orchestration
pipeline (lookup → payout → status poll) and persist normal DB artifacts.

Simulation rules:
  - Lookup: always SUCCESS, returns beneficiary_name echoed as accountName
  - Payout: all items PROCESSING (accepted)
  - Requery: always SUCCESSFUL on first poll
  - Account "0000000000" triggers a lookup failure (for testing)
  - Amount > 10_000_000 triggers a payout rejection (for testing)
"""

import logging
import uuid

from src.infrastructure.external_services.interswitch.payout_gateway import PayoutGateway

logger = logging.getLogger(__name__)

_FAIL_ACCOUNT = "0000000000"
_REJECT_AMOUNT_THRESHOLD = 10_000_000


class SimulatedPayoutGateway(PayoutGateway):

    @property
    def is_simulated(self) -> bool:
        return True

    async def lookup_customer(
        self,
        institution_code: str,
        account_number: str,
        transaction_reference: str,
        currency_code: str = "NGN",
    ) -> dict:
        logger.info(
            f"[SimulatedGateway] lookup_customer: "
            f"bank={institution_code}, account={account_number}, ref={transaction_reference[:30]}"
        )

        if account_number == _FAIL_ACCOUNT:
            return {
                "lookupStatus": "FAILED",
                "canCredit": False,
                "accountName": "",
                "accountNumber": account_number,
                "institutionCode": institution_code,
                "transactionReference": transaction_reference,
                "raw_response": {"simulated": True, "reason": "test-fail-account"},
            }

        simulated_name = f"SIMULATED ACCOUNT {account_number[-4:]}"
        return {
            "lookupStatus": "SUCCESS",
            "canCredit": True,
            "accountName": simulated_name,
            "accountNumber": account_number,
            "institutionCode": institution_code,
            "transactionReference": transaction_reference,
            "raw_response": {"simulated": True},
        }

    async def execute_payout(
        self,
        batch_reference: str,
        items: list[dict],
        currency: str = "NGN",
    ) -> dict:
        logger.info(
            f"[SimulatedGateway] execute_payout: batch={batch_reference}, items={len(items)}"
        )

        results = []
        accepted = 0
        rejected = 0

        for item in items:
            ref = item["transaction_reference"]
            amount = item["amount"]

            if amount > _REJECT_AMOUNT_THRESHOLD:
                results.append({
                    "clientReference": item.get("client_reference", ""),
                    "providerReference": ref,
                    "status": "FAILED",
                    "responseCode": "51",
                    "responseMessage": "Simulated rejection: amount exceeds threshold",
                })
                rejected += 1
            else:
                results.append({
                    "clientReference": item.get("client_reference", ""),
                    "providerReference": ref,
                    "status": "PENDING",
                    "responseCode": "00",
                    "responseMessage": "Simulated: processing",
                })
                accepted += 1

        return {
            "batchReference": batch_reference,
            "submissionStatus": "ACCEPTED" if accepted > 0 else "REJECTED",
            "acceptedCount": accepted,
            "rejectedCount": rejected,
            "items": results,
        }

    async def requery_payout(
        self,
        transaction_reference: str,
    ) -> dict:
        logger.info(
            f"[SimulatedGateway] requery_payout: ref={transaction_reference[:30]}"
        )
        return {
            "status": "SUCCESSFUL",
            "transactionReference": transaction_reference,
            "amount": 0,
            "responseCode": "00",
            "responseDescription": "Simulated: successful",
        }
