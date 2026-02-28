"""
Interswitch Customer Lookup — Account Inquiry (CreditInquiry).

Validates a beneficiary account before payout using the Quickteller
Transfer Service Account Inquiry endpoint.
"""

import logging

from src.config.settings import Settings
from src.infrastructure.external_services.interswitch.auth import InterswitchAuth

logger = logging.getLogger(__name__)


class CustomerLookupClient:

    def __init__(self) -> None:
        self._auth = InterswitchAuth()

    async def lookup_customer(
        self,
        institution_code: str,
        account_number: str,
        amount: int = 0,
    ) -> dict:
        """Verify a recipient account via Interswitch CreditInquiry.

        Args:
            institution_code: ISW institution code (e.g. "ABK" for Fidelity).
            account_number: Recipient's account number.
            amount: Amount in minor denomination (kobo). 0 for inquiry-only.

        Returns:
            dict with fields:
                canCredit (bool) — whether the account can receive funds
                accountName (str) — name on the account
                transactionReference (str) — reference for subsequent credit
                accountNumber (str)
                ...
        """
        payload = {
            "accountNumber": account_number,
            "institutionCode": institution_code,
            "amount": amount,
            "terminalId": Settings.INTERSWITCH_TERMINAL_ID,
        }

        async with await self._auth.get_resilient_client() as client:
            logger.info(f"CreditInquiry: institution={institution_code}, account={account_number}")
            response = await client.post(
                "/quicktellerservice/api/v5/transactions/CreditInquiry",
                json=payload,
            )
            data = response.json()
            can_credit = data.get("canCredit", False)
            account_name = data.get("accountName", "")
            txn_ref = data.get("transactionReference", "")

            logger.info(
                f"CreditInquiry result: canCredit={can_credit}, "
                f"accountName={account_name}, ref={txn_ref[:20]}..."
            )

            return {
                "lookupStatus": "SUCCESS" if can_credit else "FAILED",
                "canCredit": can_credit,
                "accountName": account_name,
                "accountNumber": data.get("accountNumber", account_number),
                "institutionCode": institution_code,
                "transactionReference": txn_ref,
                "raw_response": data,
            }
