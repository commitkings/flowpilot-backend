from __future__ import annotations

from src.config.settings import Settings
from src.infrastructure.external_services.monnify.client import MonnifyClient
from src.services.contracts import AccountValidationResult, TransferRequest, TransferResponse


class PaymentService:
    def __init__(self) -> None:
        self.client = MonnifyClient()

    async def create_reserved_account(
        self,
        *,
        account_reference: str,
        account_name: str,
        customer_email: str,
        customer_name: str | None = None,
        bvn: str | None = None,
        nin: str | None = None,
        get_all_available_banks: bool = True,
    ) -> dict:
        """Create a Monnify reserved account (BVN or NIN required by Monnify)."""
        cn = (customer_name or "").strip() or account_name
        return await self.client.create_reserved_account(
            account_reference=account_reference,
            account_name=account_name,
            customer_email=customer_email,
            customer_name=cn,
            bvn=bvn,
            nin=nin,
            get_all_available_banks=get_all_available_banks,
        )

    async def attach_bvn(self, *, account_reference: str, bvn: str) -> None:
        await self.client.attach_bvn(account_reference=account_reference, bvn=bvn)

    async def validate_account(self, *, account_number: str, bank_code: str) -> AccountValidationResult:
        data = await self.client.validate_account(account_number=account_number, bank_code=bank_code)
        body = data.get("responseBody", {})
        return AccountValidationResult(
            account_name=body.get("accountName", ""),
            account_number=account_number,
            bank_code=bank_code,
        )

    async def single_transfer(self, req: TransferRequest) -> TransferResponse:
        payload = {
            "amount": float(req.amount),
            "reference": req.reference,
            "narration": req.narration,
            "destinationBankCode": req.destination_bank_code,
            "destinationAccountNumber": req.destination_account_number,
            "currency": req.currency,
            "sourceAccountNumber": req.source_account_number,
        }
        data = await self.client.single_transfer(payload)
        body = data.get("responseBody", {})
        return TransferResponse(
            provider_reference=body.get("disbursementReference", ""),
            status=body.get("status", "PENDING"),
            response_reference=body.get("reference", req.reference),
        )

    async def transfer_status(self, reference: str) -> str:
        data = await self.client.transfer_status(reference)
        body = data.get("responseBody", {})
        return body.get("status", "PENDING")

    async def batch_transfer(self, *, batch_reference: str, title: str, narration: str, transaction_list: list[dict]) -> dict:
        payload = {
            "title": title,
            "batchReference": batch_reference,
            "narration": narration,
            "sourceAccountNumber": self.source_account(),
            "onValidationFailure": "CONTINUE",
            "notificationInterval": 25,
            "transactionList": transaction_list,
        }
        return await self.client.batch_transfer(payload)

    async def bvn_match(self, bvn: str, name: str, date_of_birth: str) -> str:
        data = await self.client.bvn_match(
            bvn=bvn, name=name, date_of_birth=date_of_birth
        )
        return data.get("responseBody", {}).get("matchStatus", "NO_MATCH")

    async def nin_lookup(self, nin: str, date_of_birth: str) -> dict:
        data = await self.client.nin_lookup(nin=nin, date_of_birth=date_of_birth)
        return data.get("responseBody", {})

    def source_account(self) -> str:
        return Settings.MONNIFY_SOURCE_ACCOUNT_NUMBER or ""
