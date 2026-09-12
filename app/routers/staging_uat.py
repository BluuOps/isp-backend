from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.core.authorization import AuthenticatedPrincipal
from app.core.platform_auth import require_platform_admin
from app.database import get_db
from app.services.staging_uat_fixtures import (
    cleanup_read_only_fixture,
    create_read_only_fixture,
    revoke_read_only_fixture,
)


router = APIRouter(prefix="/internal/staging/uat-fixtures", tags=["Staging UAT"])


class CreateFixtureRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ttl_minutes: int = Field(default=60, ge=5, le=120)


class CreateFixtureResponse(BaseModel):
    fixture_id: str
    email: str
    temporary_password: str
    expires_at: datetime
    organization_slug: str
    role: str


class FixtureActionResponse(BaseModel):
    fixture_id: str
    status: str


def _correlation_id(value: str | None) -> str:
    normalized = (value or "").strip()
    return normalized[:128] if normalized else uuid.uuid4().hex


@router.post("/read-only", response_model=CreateFixtureResponse, status_code=201)
def create_fixture(
    payload: CreateFixtureRequest,
    x_request_id: str | None = Header(default=None),
    db: Session = Depends(get_db),
    principal: AuthenticatedPrincipal = Depends(require_platform_admin),
) -> CreateFixtureResponse:
    fixture = create_read_only_fixture(
        db,
        ttl_minutes=payload.ttl_minutes,
        principal=principal,
        correlation_id=_correlation_id(x_request_id),
    )
    db.commit()
    return CreateFixtureResponse(**fixture.__dict__)


@router.post("/{fixture_id}/revoke", response_model=FixtureActionResponse)
def revoke_fixture(
    fixture_id: str,
    x_request_id: str | None = Header(default=None),
    db: Session = Depends(get_db),
    principal: AuthenticatedPrincipal = Depends(require_platform_admin),
) -> FixtureActionResponse:
    result = revoke_read_only_fixture(
        db,
        fixture_id=fixture_id,
        principal=principal,
        correlation_id=_correlation_id(x_request_id),
    )
    db.commit()
    return FixtureActionResponse(fixture_id=fixture_id, status=result)


@router.delete("/{fixture_id}", response_model=FixtureActionResponse)
def cleanup_fixture(
    fixture_id: str,
    x_request_id: str | None = Header(default=None),
    db: Session = Depends(get_db),
    principal: AuthenticatedPrincipal = Depends(require_platform_admin),
) -> FixtureActionResponse:
    result = cleanup_read_only_fixture(
        db,
        fixture_id=fixture_id,
        principal=principal,
        correlation_id=_correlation_id(x_request_id),
    )
    db.commit()
    return FixtureActionResponse(fixture_id=fixture_id, status=result)
