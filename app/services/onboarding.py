import re
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.models import (
    AuditLog,
    FeatureFlag,
    NotificationSetting,
    Organization,
    OrganizationBillingProfile,
    OrganizationRole,
    OrganizationStaff,
    Platform,
    ServicePlan,
    Subscription,
    Zone,
)
from app.schemas.management import OrganizationCreate
from app.services.audit import record_audit
from app.services.security import generate_temporary_password, hash_password

DEFAULT_FLAGS = (
    "customer_portal", "payment_gateway", "organization_billing", "notifications",
    "gis", "olt_management", "inventory", "ai_assistant", "enforce_limits",
)
DEFAULT_ROLES = (
    "Organization Admin", "NOC", "Billing", "Support",
    "Field Engineer", "Read Only", "Customer",
)
DEFAULT_PLANS = (
    ("Basic", "10M/10M", "0"),
    ("Standard", "20M/20M", "0"),
    ("Premium", "50M/50M", "0"),
)


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    if not slug:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid organization name")
    return slug[:100]


def onboard_organization(db: Session, payload: OrganizationCreate):
    slug = slugify(payload.name)
    if db.query(Organization).filter(Organization.slug == slug).first():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Organization slug already exists")
    platform = db.query(Platform).order_by(Platform.id).first()
    if not platform:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Platform is not configured")

    now = datetime.now(timezone.utc)
    expires = now + timedelta(days=14)
    organization = Organization(
        platform_id=platform.id, name=payload.name, slug=slug, status="trial",
        company_email=payload.email, subscription_plan=payload.subscription_plan,
        subscription_status="trial", subscription_started_at=now,
        subscription_expires_at=expires, trial_expires_at=expires,
        country="NG", timezone="Africa/Lagos", currency="NGN",
        customer_limit=500, staff_limit=10, nas_limit=5, olt_limit=2,
    )
    db.add(organization)
    db.flush()

    subscription = Subscription(
        organization_id=organization.id, plan=payload.subscription_plan,
        status="trial", starts_at=now, expires_at=expires,
        next_billing_date=expires, auto_renew=False,
    )
    temporary_password = generate_temporary_password()
    admin = OrganizationStaff(
        organization_id=organization.id, name=f"{payload.name} Admin",
        email=payload.email, password_hash=hash_password(temporary_password),
        role="Organization Admin", status="active", is_temporary_password=True,
    )
    db.add_all([subscription, admin])
    db.flush()
    record_audit(
        db, organization_id=organization.id, action="staff.added",
        target_type="staff", target_id=str(admin.id),
        new_value={"name": admin.name, "email": admin.email, "role": admin.role},
    )
    db.add_all(OrganizationRole(organization_id=organization.id, name=name) for name in DEFAULT_ROLES)
    db.add_all(FeatureFlag(organization_id=organization.id, key=key, enabled=False) for key in DEFAULT_FLAGS)
    db.add(OrganizationBillingProfile(
        organization_id=organization.id, billing_email=payload.email,
        currency=organization.currency, status="active",
    ))
    db.add(NotificationSetting(organization_id=organization.id))
    db.add(Zone(organization_id=organization.id, name="Default", status="active"))
    db.add_all(ServicePlan(
        organization_id=organization.id, name=name, rate_limit=rate,
        price=price, description=f"Default {name} plan", status="active",
    ) for name, rate, price in DEFAULT_PLANS)
    record_audit(
        db, organization_id=organization.id, action="organization.created",
        target_type="organization", target_id=str(organization.id),
        new_value={"name": organization.name, "slug": slug, "subscription_plan": payload.subscription_plan},
    )
    db.flush()
    return organization, admin, temporary_password
