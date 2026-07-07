from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class OrganizationCreate(BaseModel):
    name: str = Field(min_length=2, max_length=200)
    email: str = Field(min_length=3, max_length=255)
    subscription_plan: str = Field(min_length=2, max_length=100)


class OrganizationUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=200)
    company_email: str | None = Field(default=None, max_length=255)
    company_phone: str | None = Field(default=None, max_length=50)
    website: str | None = Field(default=None, max_length=255)
    country: str | None = Field(default=None, min_length=2, max_length=2)
    timezone: str | None = Field(default=None, max_length=100)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    logo: str | None = Field(default=None, max_length=500)
    status: str | None = Field(default=None, pattern="^(trial|active|suspended|expired|cancelled)$")


class OrganizationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    platform_id: int
    name: str
    slug: str
    status: str
    company_email: str | None
    company_phone: str | None
    website: str | None
    country: str
    timezone: str
    currency: str
    logo: str | None
    subscription_plan: str | None
    subscription_status: str
    subscription_expires_at: datetime | None
    customer_limit: int | None
    staff_limit: int | None
    nas_limit: int | None
    olt_limit: int | None


class StaffCreate(BaseModel):
    name: str = Field(min_length=2, max_length=200)
    email: str = Field(min_length=3, max_length=255)
    role: str = Field(min_length=2, max_length=100)


class StaffUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=200)
    role: str | None = Field(default=None, min_length=2, max_length=100)
    status: str | None = Field(default=None, pattern="^(active|suspended)$")


class StaffResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    organization_id: int
    name: str
    email: str
    role: str
    status: str
    is_temporary_password: bool


class SubscriptionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    organization_id: int
    plan: str
    status: str
    starts_at: datetime | None
    expires_at: datetime | None
    next_billing_date: datetime | None
    auto_renew: bool


class SubscriptionUpdate(BaseModel):
    plan: str | None = Field(default=None, max_length=100)
    status: str | None = Field(default=None, pattern="^(trial|active|suspended|expired|cancelled)$")
    expires_at: datetime | None = None
    next_billing_date: datetime | None = None
    auto_renew: bool | None = None


class FeatureFlagUpdate(BaseModel):
    enabled: bool
    configuration: dict[str, Any] | None = None


class NotificationSettingsUpdate(BaseModel):
    email_enabled: bool | None = None
    sms_enabled: bool | None = None
    expiration_alerts: bool | None = None
    payment_alerts: bool | None = None


class OnboardingResponse(BaseModel):
    organization: OrganizationResponse
    admin: StaffResponse
    temporary_password: str
    status: str
