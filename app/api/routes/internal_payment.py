from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from src.config.settings import Settings
from src.services.contracts import TransferRequest
from src.services.payment_service import PaymentService

router = APIRouter(prefix="/internal/payment", tags=["internal-payment"])


def _require_internal_token(x_internal_token: str | None = Header(default=None)) -> None:
    expected = Settings.INTERNAL_SERVICE_TOKEN
    if not expected:
        raise HTTPException(status_code=503, detail="Internal service auth is not configured")
    if x_internal_token != expected:
        raise HTTPException(status_code=401, detail="Unauthorized internal request")


class ValidateAccountRequest(BaseModel):
    account_number: str
    bank_code: str


class SingleTransferRequest(BaseModel):
    amount: float
    reference: str
    narration: str
    destination_bank_code: str
    destination_account_number: str
    source_account_number: str
    currency: str = "NGN"


@router.post("/account/validate")
async def validate_account(
    body: ValidateAccountRequest,
    _=Depends(_require_internal_token),
):
    service = PaymentService()
    result = await service.validate_account(
        account_number=body.account_number,
        bank_code=body.bank_code,
    )
    return result.__dict__


@router.post("/transfer/single")
async def transfer_single(
    body: SingleTransferRequest,
    _=Depends(_require_internal_token),
):
    service = PaymentService()
    result = await service.single_transfer(TransferRequest(**body.model_dump()))
    return result.__dict__
