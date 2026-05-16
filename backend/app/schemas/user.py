from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class UserCreate(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=253)
    service_plan: str = Field(..., min_length=1, max_length=100)
    zone: Optional[str] = Field(default=None, max_length=100)
    status: str = Field(default="active", pattern="^(active|suspended|inactive)$")


class UserUpdate(BaseModel):
    username: Optional[str] = Field(default=None, min_length=1, max_length=64)
    password: Optional[str] = Field(default=None, min_length=1, max_length=253)
    service_plan: Optional[str] = Field(default=None, min_length=1, max_length=100)
    zone: Optional[str] = Field(default=None, max_length=100)
    status: Optional[str] = Field(default=None, pattern="^(active|suspended|inactive)$")


class UserPlanChange(BaseModel):
    service_plan: str = Field(..., min_length=1, max_length=100)


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
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


class UserDeleteResponse(BaseModel):
    id: int
    username: str
    message: str
