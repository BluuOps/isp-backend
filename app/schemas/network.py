from datetime import datetime
from ipaddress import ip_address

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ZoneCreate(BaseModel):
    name: str = Field(min_length=2, max_length=150)
    status: str = Field(default="active", pattern="^(active|disabled)$")


class ZoneUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=150)
    status: str | None = Field(default=None, pattern="^(active|disabled)$")


class ZoneResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    organization_id: int
    name: str
    status: str
    nas_count: int = 0
    user_count: int = 0
    active_session_count: int = 0
    created_at: datetime


class NasCreate(BaseModel):
    zone_id: int
    nas_ip_address: str = Field(min_length=3, max_length=45)
    display_name: str = Field(min_length=2, max_length=150)
    short_name: str | None = Field(default=None, max_length=100)
    device_type: str | None = Field(default=None, max_length=100)
    status: str = Field(default="active", pattern="^(active|disabled)$")
    description: str | None = Field(default=None, max_length=1000)

    @field_validator("nas_ip_address")
    @classmethod
    def validate_nas_ip(cls, value: str) -> str:
        return str(ip_address(value.strip()))


class NasUpdate(BaseModel):
    zone_id: int | None = None
    nas_ip_address: str | None = Field(default=None, min_length=3, max_length=45)
    display_name: str | None = Field(default=None, min_length=2, max_length=150)
    short_name: str | None = Field(default=None, max_length=100)
    device_type: str | None = Field(default=None, max_length=100)
    status: str | None = Field(default=None, pattern="^(active|disabled)$")
    description: str | None = Field(default=None, max_length=1000)

    @field_validator("nas_ip_address")
    @classmethod
    def validate_nas_ip(cls, value: str | None) -> str | None:
        return None if value is None else str(ip_address(value.strip()))


class NasStatusUpdate(BaseModel):
    status: str = Field(pattern="^(active|disabled)$")


class NasResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    organization_id: int
    zone_id: int
    zone_name: str
    nas_ip_address: str
    display_name: str
    short_name: str | None = None
    device_type: str | None = None
    status: str
    description: str | None = None
    active_session_count: int = 0
    accounting_record_count: int = 0
    created_at: datetime
    updated_at: datetime | None = None


class ZoneDetailResponse(ZoneResponse):
    nas_devices: list[NasResponse] = Field(default_factory=list)
