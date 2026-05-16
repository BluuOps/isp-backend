from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class ServicePlanCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    rate_limit: str = Field(..., min_length=1, max_length=100)
    description: Optional[str] = Field(default=None, max_length=255)
    status: str = Field(default="active", pattern="^(active|inactive)$")


class ServicePlanUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    rate_limit: Optional[str] = Field(default=None, min_length=1, max_length=100)
    description: Optional[str] = Field(default=None, max_length=255)
    status: Optional[str] = Field(default=None, pattern="^(active|inactive)$")


class ServicePlanResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    rate_limit: str
    description: Optional[str] = None
    status: str
