from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field


class PlanActivationSummary(BaseModel):
    payment_id: int
    service_id: int
    customer_id: str
    current_plan: str
    purchased_plan: str
    amount: Decimal
    currency: str
    payment_reference_suffix: str
    current_expiration: datetime | None
    proposed_expiration: datetime
    activation_status: str
    resolution_status: str | None
    duplicate_count: int
    duplicate_warning: bool
    eligible: bool
    eligibility_reason: str | None = None
    activated_at: datetime | None = None
    activation_correlation_id: str | None = None


class PlanActivationRequest(BaseModel):
    correlation_id: str = Field(..., min_length=8, max_length=120)


class DuplicateResolutionRequest(BaseModel):
    canonical_payment_id: int = Field(..., ge=1)
    correlation_id: str = Field(..., min_length=8, max_length=120)
    reason: str = Field(..., min_length=8, max_length=500)


class DuplicateResolutionResponse(BaseModel):
    logical_period_key: str
    canonical_payment_id: int
    duplicate_payment_ids: list[int]
    resolution_status: str
