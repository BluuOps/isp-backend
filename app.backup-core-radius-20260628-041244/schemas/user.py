from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


from typing import Optional
from pydantic import BaseModel, Field


class UserCreate(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)

    password: str = Field(
        ...,
        min_length=1,
        max_length=253
    )

    customer_id: str = Field(..., min_length=1, max_length=100)

    service_plan: str = Field(
        ...,
        min_length=1,
        max_length=100
    )

    zone: Optional[str] = Field(
        default=None,
        max_length=100
    )

    status: str = Field(
        default="pending",
        pattern="^(active|suspended|pending|terminated)$"
    )


class UserUpdate(BaseModel):
    username: Optional[str] = Field(default=None, min_length=1, max_length=64)
    password: Optional[str] = Field(default=None, min_length=1, max_length=253)
    customer_id: Optional[str] = Field(default=None, min_length=1, max_length=100)
    service_plan: Optional[str] = Field(default=None, min_length=1, max_length=100)
    zone: Optional[str] = Field(default=None, max_length=100)
    status: Optional[str] = Field(default=None, pattern="^(active|suspended|pending|terminated)$")


class UserPlanChange(BaseModel):
    service_plan: str = Field(..., min_length=1, max_length=100)


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    customer_id: Optional[str] = None

    id: int
    username: str
    service_plan: str
    zone: Optional[str] = None
    status: str

class Config:
        orm_mode = True


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
