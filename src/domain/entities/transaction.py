from datetime import datetime
from typing import Optional
from enum import Enum

from pydantic import BaseModel, Field


class TransactionStatus(str, Enum):
    SUCCESS = "SUCCESS"
    PENDING = "PENDING"
    FAILED = "FAILED"
    REVERSED = "REVERSED"


class TransactionChannel(str, Enum):
    CARD = "CARD"
    TRANSFER = "TRANSFER"
    USSD = "USSD"
    QR = "QR"
    OTHER = "OTHER"


class Transaction(BaseModel):
    transaction_reference: str = Field(..., description="Interswitch transaction reference")
    run_id: Optional[str] = Field(None, description="Associated agent run ID")
    amount: float = Field(..., description="Transaction amount")
    currency: str = Field(default="NGN")
    status: TransactionStatus
    channel: Optional[TransactionChannel] = None
    timestamp: Optional[datetime] = None
    customer_id: Optional[str] = None
    merchant_id: Optional[str] = None
    processor_response_code: Optional[str] = None
    processor_response_message: Optional[str] = None
    settlement_date: Optional[str] = None
    is_anomaly: bool = Field(default=False)
    anomaly_reason: Optional[str] = None

    class Config:
        use_enum_values = True
