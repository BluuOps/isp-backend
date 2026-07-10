from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


class AuditLogResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    organization_id: Optional[int] = None
    actor: str
    action: str
    target_type: str
    target_id: Optional[str] = None
    old_value: Optional[dict[str, Any]] = None
    new_value: Optional[dict[str, Any]] = None
    success: bool
    ip_address: Optional[str] = None
    created_at: datetime


class AuditLogListResponse(BaseModel):
    items: list[AuditLogResponse]
    total: int
    limit: int = Field(ge=1, le=500)
    offset: int = Field(ge=0)
