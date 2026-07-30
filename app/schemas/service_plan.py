from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class ServicePlanCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    rate_limit: str = Field(..., min_length=1, max_length=100)
    price: Optional[str] = Field(default=None, max_length=50)
    description: Optional[str] = Field(default=None, max_length=255)
    status: str = Field(default="active", pattern="^(active|inactive)$")
    price_minor: Optional[int] = Field(default=None, ge=1)
    currency: str = Field(default="NGN", pattern="^[A-Z]{3}$")
    duration_days: int = Field(default=30, ge=1, le=366)
    billing_interval: str = Field(default="monthly", pattern="^(daily|weekly|monthly|quarterly|yearly)$")
    customer_visible: bool = False


class ServicePlanUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    rate_limit: Optional[str] = Field(default=None, min_length=1, max_length=100)
    price: Optional[str] = Field(default=None, max_length=50)
    description: Optional[str] = Field(default=None, max_length=255)
    status: Optional[str] = Field(default=None, pattern="^(active|inactive)$")
    price_minor: Optional[int] = Field(default=None, ge=1)
    currency: Optional[str] = Field(default=None, pattern="^[A-Z]{3}$")
    duration_days: Optional[int] = Field(default=None, ge=1, le=366)
    billing_interval: Optional[str] = Field(default=None, pattern="^(daily|weekly|monthly|quarterly|yearly)$")
    customer_visible: Optional[bool] = None


class ServicePlanResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    rate_limit: str
    price: Optional[str] = None
    description: Optional[str] = None
    status: str
    price_minor: Optional[int] = None
    currency: str = "NGN"
    duration_days: int = 30
    billing_interval: str = "monthly"
    customer_visible: bool = False
