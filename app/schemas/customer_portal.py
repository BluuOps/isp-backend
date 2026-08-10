from datetime import datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class CustomerPortalOrganizationBranding(BaseModel):
    id: int
    name: str
    slug: str
    logo: Optional[str] = None
    currency: str
    timezone: str


class CustomerPortalProfile(BaseModel):
    customer_id: str
    name: str
    email: str
    phone: str
    address: str
    customer_type: str
    account_status: str
    organization: CustomerPortalOrganizationBranding


class CustomerPortalProfileUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: Optional[str] = Field(default=None, min_length=3, max_length=150)
    phone: Optional[str] = Field(default=None, min_length=3, max_length=50)
    address: Optional[str] = Field(default=None, min_length=1, max_length=255)


class CustomerPortalServiceSummary(BaseModel):
    id: int
    username: str
    masked_username: str
    service_plan: str
    rate_limit: Optional[str] = None
    status: str
    expiration_date: Optional[datetime] = None
    online: bool
    last_session_started_at: Optional[datetime] = None
    last_session_updated_at: Optional[datetime] = None
    framed_ip_address: Optional[str] = None
    pending_plan_activation: bool = False
    pending_activation_payment_id: Optional[int] = None
    pending_activation_plan: Optional[str] = None


class CustomerPortalPaymentSummary(BaseModel):
    id: int
    transaction_reference: str
    amount: Decimal
    currency: str
    payment_method: str
    payment_status: str
    paid_at: Optional[datetime] = None
    created_at: datetime
    selected_plan_id: Optional[int] = None
    purchased_plan: Optional[str] = None
    fulfillment_status: str = "not_applicable"
    resulting_expiration_date: Optional[datetime] = None


class CustomerPortalPaymentDetail(CustomerPortalPaymentSummary):
    external_reference: Optional[str] = None
    user_id: Optional[int] = None
    notes: Optional[str] = None


class CustomerPortalTicketMessageResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    ticket_id: int
    sender_type: str
    sender_id: str
    body: str
    created_at: datetime


class CustomerPortalTicketResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    customer_id: str
    user_id: Optional[int] = None
    subject: str
    category: str
    priority: str
    status: str
    created_at: datetime
    updated_at: Optional[datetime] = None
    resolved_at: Optional[datetime] = None
    closed_at: Optional[datetime] = None
    messages: list[CustomerPortalTicketMessageResponse] = []


class CustomerPortalTicketCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: Optional[int] = None
    subject: str = Field(..., min_length=3, max_length=200)
    category: str = Field(default="general", min_length=1, max_length=100)
    priority: str = Field(default="normal", pattern="^(low|normal|high|urgent)$")
    body: str = Field(..., min_length=1, max_length=5000)


class CustomerPortalSubscription(BaseModel):
    current_plan: Optional[str] = None
    service_status: Optional[str] = None
    expiration_date: Optional[datetime] = None
    renewal_status: str
    renewal_eligible: bool
    outstanding_balance: Decimal = Decimal("0")
    last_payment: Optional[CustomerPortalPaymentSummary] = None


class CustomerPortalDashboard(BaseModel):
    customer_id: str
    customer_name: str
    account_status: str
    organization: CustomerPortalOrganizationBranding
    current_plan: Optional[str] = None
    expiration_date: Optional[datetime] = None
    subscription_status: str
    service_status: str
    online: bool
    services: list[CustomerPortalServiceSummary]
    recent_payments: list[CustomerPortalPaymentSummary]
    open_ticket_count: int
    recent_tickets: list[CustomerPortalTicketResponse]
    renewal_eligible: bool
    empty_states: dict[str, str] = {}
