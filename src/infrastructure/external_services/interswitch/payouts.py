"""
Interswitch Payouts — Create Payout, Status Check, and Wallet Balance.

Uses the Payouts Service hosted at api.interswitchng.com:
  - Create Payout:   POST /payouts/api/v1/payouts (wallet-funded bank transfer)
  - Status Check:    GET  /payouts/api/v1/payouts/{transactionReference}
  - Customer Lookup: POST /payouts/api/v1/payouts/customer-lookup
  - Wallet Balance:  GET  /merchant-wallet/api/v1/wallet/balance/{merchantCode}
"""

import logging
from typing import Optional

from src.config.settings import Settings
from src.infrastructure.external_services.interswitch.auth import InterswitchAuth

logger = logging.getLogger(__name__)


class PayoutClient:

    def __init__(self) -> None:
        self._auth = InterswitchAuth(base_url=Settings.INTERSWITCH_PAYOUTS_BASE_URL)

    async def get_wallet_balance(self) -> dict:
        """Check wallet balance before payout execution.

        Returns:
            dict with availableBalance, ledgerBalance (in NGN major denomination).
        """
        merchant_code = Settings.INTERSWITCH_MERCHANT_ID
        wallet_id = Settings.INTERSWITCH_WALLET_ID

        async with await self._auth.get_resilient_client() as client:
            logger.info(f"Checking wallet balance: merchant={merchant_code}, wallet={wallet_id}")
            response = await client.get(
                f"/merchant-wallet/api/v1/wallet/balance/{merchant_code}",
                params={"walletId": wallet_id},
            )
            data = response.json()
            logger.info(f"Wallet balance: available={data.get('availableBalance', '?')}")
            return data

    async def execute_single_payout(
        self,
        transaction_reference: str,
        amount_ngn: float,
        narration: str,
        account_number: str,
        institution_code: str,
        currency_code: str = "NGN",
        single_call: bool = False,
    ) -> dict:
        """Execute a single payout via Interswitch Payouts Service.

        This is a per-item operation — each candidate requires a separate call.
        The transactionReference MUST come from a prior customer-lookup call
        (unless singleCall=True).

        Args:
            transaction_reference: Same reference used in customer-lookup.
            amount_ngn: Payout amount in NGN (major denomination — NOT kobo).
            narration: Payment narration/description.
            account_number: Recipient account number.
            institution_code: Bank code (CBN, NIP, or ISW internal).
            currency_code: Currency code (default "NGN").
            single_call: If True, skip separate lookup step.

        Returns:
            dict with Interswitch payout response fields.
        """
        payload = {
            "transactionReference": transaction_reference,
            "payoutChannel": "BANK_TRANSFER",
            "currencyCode": currency_code,
            "amount": amount_ngn,
            "narration": narration,
            "walletDetails": {
                "pin": Settings.INTERSWITCH_WALLET_PIN,
                "walletId": Settings.INTERSWITCH_WALLET_ID,
            },
            "recipient": {
                "recipientAccount": account_number,
                "recipientBank": institution_code,
                "currencyCode": currency_code,
            },
            "singleCall": single_call,
        }

        async with await self._auth.get_resilient_client() as client:
            logger.info(
                f"CreatePayout: ref={transaction_reference[:30]}, "
                f"amount={amount_ngn} NGN, account={account_number}"
            )
            response = await client.post(
                "/payouts/api/v1/payouts",
                json=payload,
            )
            data = response.json()
            logger.info(
                f"CreatePayout result: status={data.get('status', '?')}, "
                f"ref={transaction_reference[:30]}"
            )
            return data

    async def requery_payout(
        self,
        transaction_reference: str,
    ) -> dict:
        """Check payout status via Interswitch Payouts Service.

        Args:
            transaction_reference: The transactionReference from the payout call.

        Returns:
            dict with status (SUCCESSFUL | FAILED | PROCESSING), amount, etc.
        """
        async with await self._auth.get_resilient_client() as client:
            logger.info(f"PayoutStatus: ref={transaction_reference[:30]}")
            response = await client.get(
                f"/payouts/api/v1/payouts/{transaction_reference}",
            )
            data = response.json()
            logger.info(f"PayoutStatus result: status={data.get('status', '?')}")
            return data

    # ------------------------------------------------------------------
    # Batch wrapper — delegates to per-item execute_single_payout.
    # ------------------------------------------------------------------

    async def execute_payout(
        self,
        batch_reference: str,
        items: list[dict],
        currency: str = "NGN",
    ) -> dict:
        """Execute payouts for a batch of items (sequentially per-item).

        Each item dict must contain:
            transaction_reference (str): From prior customer-lookup
            amount (float): In NGN (major denomination)
            account_number (str): Recipient account
            institution_code (str): Bank code
            client_reference (str, optional): Internal tracking ref
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
                    account_number=item["account_number"],
                    institution_code=item["institution_code"],
                    currency_code=currency,
                )
                raw_status = (result.get("status") or "").upper()
                if raw_status in ("SUCCESSFUL", "PROCESSING"):
                    status = "PENDING"
                    accepted += 1
                else:
                    status = "FAILED"
                    rejected += 1

                results.append({
                    "clientReference": item.get("client_reference", ""),
                    "providerReference": item["transaction_reference"],
                    "status": status,
                    "responseCode": result.get("responseCode", ""),
                    "responseMessage": result.get("responseMessage", ""),
                })
            except Exception as e:
                logger.error(f"Payout failed for {item.get('transaction_reference')}: {e}")
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

    async def get_payout_status(self, transaction_reference: str) -> dict:
        """Check status of a single payout."""
        return await self.requery_payout(transaction_reference=transaction_reference)
