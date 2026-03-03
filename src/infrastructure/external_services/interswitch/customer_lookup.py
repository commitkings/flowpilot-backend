"""
Interswitch Customer Lookup — Payouts Service Account Verification.

Validates a beneficiary account before payout using the Payouts Service
customer-lookup endpoint (POST /payouts/api/v1/payouts/customer-lookup).
"""

import logging

from src.config.settings import Settings
from src.infrastructure.external_services.interswitch.auth import InterswitchAuth

logger = logging.getLogger(__name__)


class CustomerLookupClient:

    def __init__(self) -> None:
        self._auth = InterswitchAuth(base_url=Settings.INTERSWITCH_PAYOUTS_BASE_URL)

    async def lookup_customer(
        self,
        institution_code: str,
        account_number: str,
        transaction_reference: str,
        currency_code: str = "NGN",
    ) -> dict:
        """Verify a recipient account via Interswitch Payouts customer-lookup.

        Args:
            institution_code: Bank code (CBN, NIP, or ISW internal code).
            account_number: Recipient's account number.
            transaction_reference: Caller-generated unique reference. Must be
                reused in the subsequent payout call.
            currency_code: Currency code (default "NGN").

        Returns:
            dict with fields:
                lookupStatus (str) — "SUCCESS" or "FAILED"
                canCredit (bool) — whether the account can receive funds
                accountName (str) — name on the account
                accountNumber (str)
                institutionCode (str)
                transactionReference (str) — pass-through of the caller-provided ref
                raw_response (dict) — full API response
        """
        payload = {
            "payoutChannel": "BANK_TRANSFER",
            "transactionReference": transaction_reference,
            "recipient": {
                "recipientAccount": account_number,
                "recipientBank": institution_code,
                "currencyCode": currency_code,
            },
        }

        async with await self._auth.get_resilient_client() as client:
            logger.info(
                f"CustomerLookup: bank={institution_code}, account={account_number}, "
                f"ref={transaction_reference[:30]}"
            )
            response = await client.post(
                "/payouts/api/v1/payouts/customer-lookup",
                json=payload,
            )
            data = response.json()

            # Parse recipient name from response
            recipient = data.get("recipient", {})
            recipient_name = (
                recipient.get("recipientName")
                or data.get("recipientName")
                or data.get("accountName")
                or ""
            )
            lookup_successful = bool(recipient_name)

            logger.info(
                f"CustomerLookup result: name={recipient_name!r}, "
                f"ref={transaction_reference[:30]}"
            )

            return {
                "lookupStatus": "SUCCESS" if lookup_successful else "FAILED",
                "canCredit": lookup_successful,
                "accountName": recipient_name,
                "accountNumber": recipient.get("recipientAccount", account_number),
                "institutionCode": institution_code,
                "transactionReference": transaction_reference,
                "raw_response": data,
            }
