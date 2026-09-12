from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.core.authorization import AuthenticatedPrincipal, PrincipalType
from app.core.config import settings
from app.models import Organization, OrganizationStaff
from app.services.audit import record_audit
from app.services.security import hash_password


TARGET_ORGANIZATION_SLUG = "smart-fiber"
FIXTURE_ROLE = "Read Only"
FIXTURE_EMAIL_DOMAIN = "smartfiber.test"
MIN_TTL_MINUTES = 5
MAX_TTL_MINUTES = 120
STAGING_DATABASE_NAME = "isp_db_stage"


@dataclass(frozen=True)
class CreatedFixture:
    fixture_id: str
    email: str
    temporary_password: str
    expires_at: datetime
    organization_slug: str
    role: str


def require_staging_fixture_capability() -> None:
    if settings.deployment_environment != "staging":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    if not settings.staging_uat_fixtures_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    if make_url(settings.database_url).database != STAGING_DATABASE_NAME:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")


def _require_platform_authority(principal: AuthenticatedPrincipal) -> None:
    if principal.principal_type != PrincipalType.PLATFORM_ADMIN or not principal.platform_authority:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Platform administrator access is required")


def _database_now(db: Session) -> datetime:
    return db.execute(select(func.current_timestamp())).scalar_one()


def _target_organization(db: Session) -> Organization:
    organization = db.execute(
        select(Organization).where(
            Organization.slug == TARGET_ORGANIZATION_SLUG,
            Organization.status == "active",
        )
    ).scalar_one_or_none()
    if not organization:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Smart Fiber staging organization is unavailable",
        )
    return organization


def create_read_only_fixture(
    db: Session,
    *,
    ttl_minutes: int,
    principal: AuthenticatedPrincipal,
    correlation_id: str,
) -> CreatedFixture:
    require_staging_fixture_capability()
    _require_platform_authority(principal)
    if not MIN_TTL_MINUTES <= ttl_minutes <= MAX_TTL_MINUTES:
        raise HTTPException(status_code=422, detail="Invalid fixture lifetime")
    organization = _target_organization(db)
    now = _database_now(db)
    existing = db.execute(
        select(OrganizationStaff.id).where(
            OrganizationStaff.organization_id == organization.id,
            OrganizationStaff.is_uat_fixture.is_(True),
            OrganizationStaff.status == "active",
            OrganizationStaff.uat_expires_at > func.current_timestamp(),
        )
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail="An active staging UAT fixture already exists")

    fixture_id = uuid.uuid4().hex
    password = secrets.token_urlsafe(32)
    expires_at = now + timedelta(minutes=ttl_minutes)
    staff = OrganizationStaff(
        organization_id=organization.id,
        name="Temporary Read Only OLT UAT",
        email=f"uat-read-only-{fixture_id[:12]}@{FIXTURE_EMAIL_DOMAIN}",
        password_hash=hash_password(password),
        role=FIXTURE_ROLE,
        status="active",
        is_temporary_password=True,
        is_uat_fixture=True,
        uat_fixture_id=fixture_id,
        uat_expires_at=expires_at,
    )
    db.add(staff)
    db.flush()
    record_audit(
        db,
        organization_id=organization.id,
        actor=principal.actor_label,
        actor_type="platform_admin",
        actor_id=principal.subject_id,
        actor_label=principal.actor_label,
        action="uat.read_only_fixture.created",
        target_type="organization_staff",
        target_id=str(staff.id),
        new_value={
            "fixture_id": fixture_id,
            "organization_slug": TARGET_ORGANIZATION_SLUG,
            "role": FIXTURE_ROLE,
            "created_at": now.isoformat(),
            "expires_at": expires_at.isoformat(),
            "correlation_id": correlation_id,
        },
    )
    return CreatedFixture(
        fixture_id=fixture_id,
        email=staff.email,
        temporary_password=password,
        expires_at=expires_at,
        organization_slug=TARGET_ORGANIZATION_SLUG,
        role=FIXTURE_ROLE,
    )


def revoke_read_only_fixture(
    db: Session,
    *,
    fixture_id: str,
    principal: AuthenticatedPrincipal,
    correlation_id: str,
) -> str:
    require_staging_fixture_capability()
    _require_platform_authority(principal)
    organization = _target_organization(db)
    staff = db.execute(
        select(OrganizationStaff).where(
            OrganizationStaff.organization_id == organization.id,
            OrganizationStaff.is_uat_fixture.is_(True),
            OrganizationStaff.uat_fixture_id == fixture_id,
            OrganizationStaff.role == FIXTURE_ROLE,
        )
    ).scalar_one_or_none()
    if not staff:
        return "already_absent"
    if staff.status != "active" or staff.uat_revoked_at is not None:
        return "already_revoked"
    now = _database_now(db)
    staff.status = "inactive"
    staff.uat_revoked_at = now
    record_audit(
        db,
        organization_id=organization.id,
        actor=principal.actor_label,
        actor_type="platform_admin",
        actor_id=principal.subject_id,
        actor_label=principal.actor_label,
        action="uat.read_only_fixture.revoked",
        target_type="organization_staff",
        target_id=str(staff.id),
        old_value={"status": "active"},
        new_value={
            "status": "inactive",
            "fixture_id": fixture_id,
            "revoked_at": now.isoformat(),
            "correlation_id": correlation_id,
            "active_tokens_invalidated_by": "staff_status",
        },
    )
    return "revoked"


def cleanup_read_only_fixture(
    db: Session,
    *,
    fixture_id: str,
    principal: AuthenticatedPrincipal,
    correlation_id: str,
) -> str:
    require_staging_fixture_capability()
    _require_platform_authority(principal)
    organization = _target_organization(db)
    staff = db.execute(
        select(OrganizationStaff).where(
            OrganizationStaff.organization_id == organization.id,
            OrganizationStaff.is_uat_fixture.is_(True),
            OrganizationStaff.uat_fixture_id == fixture_id,
            OrganizationStaff.role == FIXTURE_ROLE,
        )
    ).scalar_one_or_none()
    if not staff:
        return "already_absent"
    staff_id = staff.id
    db.delete(staff)
    record_audit(
        db,
        organization_id=organization.id,
        actor=principal.actor_label,
        actor_type="platform_admin",
        actor_id=principal.subject_id,
        actor_label=principal.actor_label,
        action="uat.read_only_fixture.cleaned",
        target_type="organization_staff",
        target_id=str(staff_id),
        old_value={"fixture_id": fixture_id, "role": FIXTURE_ROLE},
        new_value={
            "outcome": "removed",
            "correlation_id": correlation_id,
            "sanitized_lifecycle_audit_retained": True,
        },
    )
    return "removed"
