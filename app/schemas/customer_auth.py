from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class CustomerAuthLoginRequest(BaseModel):
    identifier: str = Field(..., min_length=1, max_length=150)
    password: str = Field(..., min_length=1)
    organization_slug: str | None = Field(default=None, min_length=1, max_length=100)
    tenant_id: str | None = Field(default=None, min_length=1, max_length=100)


class CustomerAuthUser(BaseModel):
    id: int
    principal_type: str = "customer"
    organization_id: int
    organization_slug: str
    customer_id: str
    email: str
    phone: str | None = None
    status: str


class CustomerAuthResponse(BaseModel):
    token: str
    user: CustomerAuthUser


class CustomerPasswordChangeRequest(BaseModel):
    current_password: str = Field(..., min_length=1)
    new_password: str = Field(..., min_length=8, max_length=255)


class CustomerPortalAccountResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    organization_id: int
    customer_id: str
    email: str
    phone: str | None = None
    status: str
    last_login_at: datetime | None = None
    created_at: datetime
