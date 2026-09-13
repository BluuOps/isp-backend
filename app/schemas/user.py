from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class UserCreate(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=253)
    customer_id: str = Field(..., min_length=1, max_length=100)
    service_plan: str = Field(..., min_length=1, max_length=100)
    expiration_date: Optional[datetime] = None
    zone: Optional[str] = Field(default=None, max_length=100)
    status: str = Field(default="pending", pattern="^(active|suspended|pending|terminated)$")


class UserUpdate(BaseModel):
    username: Optional[str] = Field(default=None, min_length=1, max_length=64)
    password: Optional[str] = Field(default=None, min_length=1, max_length=253)
    customer_id: Optional[str] = Field(default=None, min_length=1, max_length=100)
    service_plan: Optional[str] = Field(default=None, min_length=1, max_length=100)
    expiration_date: Optional[datetime] = None
    zone: Optional[str] = Field(default=None, max_length=100)
    status: Optional[str] = Field(default=None, pattern="^(active|suspended|pending|terminated)$")


class UserPlanChange(BaseModel):
    service_plan: str = Field(..., min_length=1, max_length=100)


class UserRecharge(BaseModel):
    plan_id: int = Field(..., gt=0)
    quantity: int = Field(default=1, ge=1, le=24)


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    customer_id: Optional[str] = None
    expiration_date: Optional[datetime] = None
    service_plan: str
    zone: Optional[str] = None
    status: str


class UserLifecycleResponse(BaseModel):
    id: int
    username: str
    status: str
    service_plan: str
    message: str


class UserSuspendResponse(UserLifecycleResponse):
    pass


class UserActivateResponse(UserLifecycleResponse):
    pass


class UserPendingResponse(UserLifecycleResponse):
    pass


class UserTerminateResponse(UserLifecycleResponse):
    pass


class UserDeleteResponse(BaseModel):
    id: int
    username: str
    message: str
