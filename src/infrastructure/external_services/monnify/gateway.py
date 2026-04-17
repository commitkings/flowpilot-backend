from __future__ import annotations

from src.infrastructure.external_services.interswitch.payout_gateway import PayoutGateway
from src.services.payment_service import PaymentService


class MonnifyPayoutGateway(PayoutGateway):
    def __init__(self) -> None:
        self.payment = PaymentService()

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
        result = await self.payment.validate_account(
            account_number=account_number,
            bank_code=institution_code,
        )
        return {
            "lookupStatus": "SUCCESS" if result.account_name else "FAILED",
            "canCredit": bool(result.account_name),
            "accountName": result.account_name,
            "accountNumber": account_number,
            "institutionCode": institution_code,
            "transactionReference": transaction_reference,
            "raw_response": {
                "provider": "monnify",
            },
        }

    async def execute_payout(
        self,
        batch_reference: str,
        items: list[dict],
        currency: str = "NGN",
    ) -> dict:
        transaction_list = []
        for item in items:
            transaction_list.append(
                {
                    "amount": float(item["amount"]),
                    "reference": item["transaction_reference"],
                    "narration": item.get("narration") or "FlowPilot payout",
                    "destinationBankCode": item["institution_code"],
                    "destinationAccountNumber": item["account_number"],
                    "currency": currency,
                }
            )
        batch = await self.payment.batch_transfer(
            batch_reference=batch_reference,
            title=f"FlowPilot Batch {batch_reference}",
            narration="Batch payout",
            transaction_list=transaction_list,
        )
        body = batch.get("responseBody", {}) if isinstance(batch, dict) else {}
        status = body.get("status", "PENDING")
        accepted = len(items) if status in {"SUCCESS", "PENDING"} else 0
        rejected = 0 if accepted == len(items) else len(items)
        response_items: list[dict] = []
        for item in items:
            response_items.append(
                {
                    "status": status,
                    "providerReference": body.get("batchReference", batch_reference),
                    "responseMessage": status,
                }
            )
        return {
            "batchReference": batch_reference,
            "submissionStatus": "accepted" if rejected == 0 else "partial",
            "acceptedCount": accepted,
            "rejectedCount": rejected,
            "items": response_items,
        }

    async def requery_payout(self, transaction_reference: str) -> dict:
        status = await self.payment.transfer_status(transaction_reference)
        normalized = {
            "SUCCESS": "SUCCESSFUL",
            "PENDING": "PROCESSING",
            "FAILED": "FAILED",
        }.get(status, "PROCESSING")
        return {"status": normalized}
