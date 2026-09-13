from datetime import datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


PAYMENT_STATUSES = {"initiated", "pending", "successful", "paid", "failed", "abandoned", "reversed", "refunded", "cancelled"}
PAYMENT_METHODS = {"cash", "bank_transfer", "card", "pos", "ussd", "wallet", "manual", "paystack", "other"}
PAYMENT_PURPOSES = {"subscription_renewal", "manual_payment", "installation", "equipment", "other"}


class PaymentCreate(BaseModel):
    customer_id: str = Field(..., min_length=1, max_length=100)
    user_id: Optional[int] = None
    transaction_reference: str = Field(..., min_length=1, max_length=120)
    external_reference: Optional[str] = Field(default=None, max_length=255)
    amount: Decimal = Field(..., gt=Decimal("0"))
    currency: str = Field(default="NGN", min_length=3, max_length=3)
    payment_method: str = Field(..., min_length=1, max_length=50)
    payment_status: str = Field(default="paid", min_length=1, max_length=30)
    payment_purpose: str = Field(default="manual_payment", min_length=1, max_length=50)
    paid_at: Optional[datetime] = None
    created_by_staff_id: Optional[int] = None
    created_by_customer_id: Optional[str] = Field(default=None, max_length=100)
    created_by_principal_type: Optional[str] = Field(default=None, max_length=50)
    recorded_by_label: Optional[str] = Field(default=None, max_length=255)
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

    @field_validator("payment_purpose")
    @classmethod
    def validate_purpose(cls, value: str) -> str:
        normalized = value.lower()
        if normalized not in PAYMENT_PURPOSES:
            raise ValueError(f"payment_purpose must be one of: {', '.join(sorted(PAYMENT_PURPOSES))}")
        return normalized

    @field_validator("created_by_principal_type")
    @classmethod
    def validate_principal_type(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        normalized = value.lower()
        if normalized not in {"organization_staff", "customer", "system"}:
            raise ValueError("created_by_principal_type must be organization_staff, customer, or system")
        return normalized

    @model_validator(mode="after")
    def validate_single_creator_identity(self) -> "PaymentCreate":
        if self.created_by_staff_id is not None and self.created_by_customer_id is not None:
            raise ValueError("Payment cannot have both staff and customer creators")
        return self


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
    payment_purpose: str = "manual_payment"
    gateway: Optional[str] = None
    gateway_reference: Optional[str] = None
    authorization_url: Optional[str] = None
    access_code: Optional[str] = None
    initiated_at: Optional[datetime] = None
    verified_at: Optional[datetime] = None
    failed_at: Optional[datetime] = None
    renewal_processed_at: Optional[datetime] = None
    renewal_cycles: int = 1
    expected_amount: Optional[Decimal] = None
    expected_currency: Optional[str] = None
    old_expiration_date: Optional[datetime] = None
    new_expiration_date: Optional[datetime] = None
    paid_at: Optional[datetime] = None
    created_at: datetime
    updated_at: Optional[datetime] = None
    created_by_staff_id: Optional[int] = None
    created_by_customer_id: Optional[str] = None
    created_by_principal_type: Optional[str] = None
    recorded_by_label: Optional[str] = None
    created_by: Optional[str] = None
    notes: Optional[str] = None


class PaymentSummary(BaseModel):
    today_total: Decimal
    week_total: Decimal
    month_total: Decimal
    year_total: Decimal
    total_count: int


class CustomerPaymentInitializeRequest(BaseModel):
    quote_token: str = Field(..., min_length=32, max_length=4096)
    idempotency_key: Optional[str] = Field(default=None, min_length=8, max_length=120)


class CustomerPaymentInitializeResponse(BaseModel):
    payment_id: int
    transaction_reference: str
    authorization_url: str
    access_code: Optional[str] = None
    amount: Decimal
    currency: str
    status: str
    renewal_cycles: int


class CustomerServicePlanResponse(BaseModel):
    id: int
    name: str
    description: Optional[str] = None
    rate_limit: str
    billing_interval: str
    duration_days: int
    currency: str
    price_minor: int
    eligible: bool
    is_current: bool


class CustomerPaymentQuoteRequest(BaseModel):
    plan_id: int = Field(..., ge=1)
    service_id: Optional[int] = Field(default=None, ge=1)
    billing_periods: int = Field(default=1, ge=1, le=12)


class CustomerPaymentQuoteResponse(BaseModel):
    quote_token: str
    quote_reference: str
    expires_at: datetime
    service_id: int
    plan: CustomerServicePlanResponse
    billing_periods: int
    amount_minor: int
    amount: Decimal
    currency: str
    current_expiration_date: Optional[datetime] = None
    fulfillment_policy: str


class CustomerPaymentStatusResponse(BaseModel):
    payment_id: int
    transaction_reference: str
    amount: Decimal
    currency: str
    status: str
    gateway: Optional[str] = None
    gateway_reference: Optional[str] = None
    paid_at: Optional[datetime] = None
    verified_at: Optional[datetime] = None
    renewal_processed_at: Optional[datetime] = None
    old_expiration_date: Optional[datetime] = None
    new_expiration_date: Optional[datetime] = None


class CustomerPaymentVerifyRequest(BaseModel):
    reference: Optional[str] = Field(default=None, min_length=1, max_length=120)


class CustomerPaymentVerifyResponse(CustomerPaymentStatusResponse):
    verified: bool = False
    renewal_status: str = "pending"
