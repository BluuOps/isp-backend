from datetime import datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class BillingAccountCreate(BaseModel):
    user_id: int
    billing_status: str = Field(default="current", pattern="^(current|overdue|credit|paused)$")
    billing_cycle: str = Field(default="monthly", pattern="^(monthly|quarterly|yearly|custom)$")
    balance: Decimal = Field(default=Decimal("0.00"))
    next_due_date: Optional[datetime] = None
    last_payment_at: Optional[datetime] = None
    notes: Optional[str] = None


class BillingAccountUpdate(BaseModel):
    billing_status: Optional[str] = Field(default=None, pattern="^(current|overdue|credit|paused)$")
    billing_cycle: Optional[str] = Field(default=None, pattern="^(monthly|quarterly|yearly|custom)$")
    balance: Optional[Decimal] = None
    next_due_date: Optional[datetime] = None
    last_payment_at: Optional[datetime] = None
    notes: Optional[str] = None


class BillingAccountResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    billing_status: str
    billing_cycle: str
    balance: Decimal
    next_due_date: Optional[datetime] = None
    last_payment_at: Optional[datetime] = None
    notes: Optional[str] = None
