from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional


@dataclass
class AccountValidationResult:
    account_name: str
    account_number: str
    bank_code: str


@dataclass
class TransferRequest:
    amount: Decimal
    reference: str
    narration: str
    destination_bank_code: str
    destination_account_number: str
    source_account_number: str
    currency: str = "NGN"


@dataclass
class TransferResponse:
    provider_reference: str
    status: str
    response_reference: str


@dataclass
class TravelRulePayload:
    originator_name: str
    originator_wallet_id: str
    originator_bvn: str
    originator_address: str
    beneficiary_name: str
    beneficiary_account_number: str
    beneficiary_bank_code: str
    beneficiary_bank_name: Optional[str] = None
    beneficiary_address: Optional[str] = None
