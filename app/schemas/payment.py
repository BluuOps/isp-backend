from datetime import datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


PAYMENT_STATUSES = {"pending", "paid", "failed", "reversed", "cancelled"}
PAYMENT_METHODS = {"cash", "bank_transfer", "card", "pos", "ussd", "wallet", "manual", "other"}


class PaymentCreate(BaseModel):
    customer_id: str = Field(..., min_length=1, max_length=100)
    user_id: Optional[int] = None
    transaction_reference: str = Field(..., min_length=1, max_length=120)
    external_reference: Optional[str] = Field(default=None, max_length=255)
    amount: Decimal = Field(..., gt=Decimal("0"))
    currency: str = Field(default="NGN", min_length=3, max_length=3)
    payment_method: str = Field(..., min_length=1, max_length=50)
    payment_status: str = Field(default="paid", min_length=1, max_length=30)
    paid_at: Optional[datetime] = None
    created_by: Optional[str] = Field(default=None, max_length=255)
    notes: Optional[str] = None

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        return value.upper()

    @field_validator("payment_status")
    @classmethod
    def validate_status(cls, value: str) -> str:
        normalized = value.lower()
        if normalized not in PAYMENT_STATUSES:
            raise ValueError(f"payment_status must be one of: {', '.join(sorted(PAYMENT_STATUSES))}")
        return normalized

    @field_validator("payment_method")
    @classmethod
    def validate_method(cls, value: str) -> str:
        normalized = value.lower()
        if normalized not in PAYMENT_METHODS:
            raise ValueError(f"payment_method must be one of: {', '.join(sorted(PAYMENT_METHODS))}")
        return normalized


class PaymentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    organization_id: int
    customer_id: str
    user_id: Optional[int] = None
    transaction_reference: str
    external_reference: Optional[str] = None
    amount: Decimal
    currency: str
    payment_method: str
    payment_status: str
    paid_at: Optional[datetime] = None
    created_at: datetime
    updated_at: Optional[datetime] = None
    created_by: Optional[str] = None
    notes: Optional[str] = None


class PaymentSummary(BaseModel):
    today_total: Decimal
    week_total: Decimal
    month_total: Decimal
    year_total: Decimal
    total_count: int
