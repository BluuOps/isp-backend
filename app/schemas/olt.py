from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.services.olt_security import ManagementAddressError, validate_management_address


class OltDeviceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=150)
    management_address: str
    credential_reference_id: int | None = Field(default=None, gt=0)
    adapter_key: Literal["null"] = "null"
    transport: Literal["disabled"] = "disabled"
    status: Literal["active", "disabled"] = "disabled"

    @field_validator("management_address")
    @classmethod
    def management_address_is_approved(cls, value: str) -> str:
        try:
            return validate_management_address(value)
        except ManagementAddressError as exc:
            raise ValueError(str(exc)) from exc


class OltDeviceUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=150)
    management_address: str | None = None
    credential_reference_id: int | None = Field(default=None, gt=0)
    status: Literal["active", "disabled"] | None = None

    @field_validator("management_address")
    @classmethod
    def management_address_is_approved(cls, value: str | None) -> str | None:
        if value is None:
            return value
        try:
            return validate_management_address(value)
        except ManagementAddressError as exc:
            raise ValueError(str(exc)) from exc


class FreshnessMetadata(BaseModel):
    observed_at: datetime | None
    last_success_at: datetime | None
    age_seconds: int | None
    is_stale: bool
    source_status: str


class OltDeviceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    organization_id: int
    name: str
    management_address: str
    adapter_key: str
    transport: str
    status: str
    vendor: str | None
    model: str | None
    hardware_version: str | None
    software_version: str | None
    serial_number: str | None
    cache_status: str
    observed_at: datetime | None
    last_poll_attempt_at: datetime | None
    last_poll_success_at: datetime | None
    created_at: datetime
    updated_at: datetime | None
    freshness: FreshnessMetadata


class OltDeviceListResponse(BaseModel):
    items: list[OltDeviceResponse]
    total: int
    limit: int
    offset: int


class OltOverviewResponse(BaseModel):
    organization_id: int
    device_count: int
    fresh_device_count: int
    stale_device_count: int
    onu_count: int
    active_association_count: int
    operational_integration_enabled: bool


class OltInventoryItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    organization_id: int
    olt_device_id: int
    vendor_key: str
    observed_at: datetime
    details: dict[str, Any] = Field(default_factory=dict)


class OltInventoryListResponse(BaseModel):
    items: list[dict[str, Any]]
    total: int
    limit: int
    offset: int


class OltServiceAssociationCreate(BaseModel):
    onu_id: int = Field(gt=0)
    customer_id: str = Field(min_length=1, max_length=100)
    user_id: int = Field(gt=0)
    match_source: Literal["manual", "imported", "verified"] = "manual"
    confidence: int = Field(default=100, ge=0, le=100)


class OltServiceAssociationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    organization_id: int
    onu_id: int
    customer_id: str
    user_id: int
    status: str
    match_source: str
    confidence: int
    verified_by: str | None
    verified_at: datetime | None
    created_at: datetime
    updated_at: datetime | None


class OltAssociationListResponse(BaseModel):
    items: list[OltServiceAssociationResponse]
    total: int
    limit: int
    offset: int


class OltDisabledOperationResponse(BaseModel):
    status: Literal["disabled"] = "disabled"
    error: Literal["olt_integration_disabled"] = "olt_integration_disabled"
    message: str = "Operational OLT integration is disabled. No network connection was attempted."
