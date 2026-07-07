from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.platform_auth import require_platform_admin
from app.database import get_db
from app.models import Customer, FeatureFlag, Organization, Subscription, User
from app.models.radacct import RadAcct
from app.schemas.management import (
    FeatureFlagUpdate,
    OnboardingResponse,
    OrganizationCreate,
    OrganizationResponse,
    OrganizationUpdate,
    StaffResponse,
    SubscriptionResponse,
    SubscriptionUpdate,
)
from app.services.audit import record_audit
from app.services.onboarding import onboard_organization

router = APIRouter(
    prefix="/platform",
    tags=["Platform Administration"],
    dependencies=[Depends(require_platform_admin)],
)


def organization_or_404(db: Session, organization_id: int) -> Organization:
    organization = db.query(Organization).filter(Organization.id == organization_id).first()
    if not organization:
        raise HTTPException(status_code=404, detail="Organization not found")
    return organization


@router.get("/dashboard")
def dashboard(db: Session = Depends(get_db)) -> dict[str, int]:
    status_counts = dict(db.query(Organization.status, func.count(Organization.id)).group_by(Organization.status).all())
    return {
        "organizations": db.query(Organization).count(),
        "active_organizations": status_counts.get("active", 0),
        "trial_organizations": status_counts.get("trial", 0),
        "expired_organizations": status_counts.get("expired", 0),
        "subscriptions": db.query(Subscription).count(),
        "customers": db.query(Customer).count(),
        "pppoe_accounts": db.query(User).count(),
        "online_sessions": db.query(RadAcct).filter(RadAcct.acctstoptime.is_(None)).count(),
        "revenue_placeholder": 0,
    }


@router.get("/organizations", response_model=list[OrganizationResponse])
def list_organizations(db: Session = Depends(get_db)):
    return db.query(Organization).order_by(Organization.id).all()


@router.post("/organizations", response_model=OnboardingResponse, status_code=201)
def create_organization(payload: OrganizationCreate, db: Session = Depends(get_db)):
    try:
        organization, admin, temporary_password = onboard_organization(db, payload)
        db.commit()
        db.refresh(organization)
        db.refresh(admin)
    except Exception:
        db.rollback()
        raise
    return OnboardingResponse(
        organization=OrganizationResponse.model_validate(organization),
        admin=StaffResponse.model_validate(admin),
        temporary_password=temporary_password,
        status="onboarded",
    )


@router.get("/organizations/{organization_id}", response_model=OrganizationResponse)
def get_organization(organization_id: int, db: Session = Depends(get_db)):
    return organization_or_404(db, organization_id)


@router.put("/organizations/{organization_id}", response_model=OrganizationResponse)
def update_organization(organization_id: int, payload: OrganizationUpdate, db: Session = Depends(get_db)):
    organization = organization_or_404(db, organization_id)
    old = {"name": organization.name, "status": organization.status}
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(organization, field, value)
    record_audit(
        db, organization_id=organization.id, action="organization.updated",
        target_type="organization", target_id=str(organization.id),
        old_value=old, new_value=payload.model_dump(exclude_unset=True),
    )
    db.commit()
    db.refresh(organization)
    return organization


@router.delete("/organizations/{organization_id}", status_code=204)
def delete_organization(organization_id: int, db: Session = Depends(get_db)):
    organization = organization_or_404(db, organization_id)
    if organization.slug == "smart-fiber":
        raise HTTPException(status_code=409, detail="Default organization cannot be cancelled")
    organization.status = "cancelled"
    organization.subscription_status = "cancelled"
    db.query(Subscription).filter(Subscription.organization_id == organization.id).update({"status": "cancelled"})
    record_audit(
        db, organization_id=organization.id, action="organization.cancelled",
        target_type="organization", target_id=str(organization.id),
    )
    db.commit()
    return Response(status_code=204)


@router.get("/subscriptions", response_model=list[SubscriptionResponse])
def list_subscriptions(db: Session = Depends(get_db)):
    return db.query(Subscription).order_by(Subscription.id).all()


@router.put("/subscriptions/{subscription_id}", response_model=SubscriptionResponse)
def update_subscription(subscription_id: int, payload: SubscriptionUpdate, db: Session = Depends(get_db)):
    subscription = db.query(Subscription).filter(Subscription.id == subscription_id).first()
    if not subscription:
        raise HTTPException(status_code=404, detail="Subscription not found")
    old = {"plan": subscription.plan, "status": subscription.status}
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(subscription, field, value)
    organization = organization_or_404(db, subscription.organization_id)
    organization.subscription_plan = subscription.plan
    organization.subscription_status = subscription.status
    organization.subscription_expires_at = subscription.expires_at
    record_audit(
        db, organization_id=organization.id, action="subscription.changed",
        target_type="subscription", target_id=str(subscription.id),
        old_value=old, new_value=payload.model_dump(exclude_unset=True),
    )
    db.commit()
    db.refresh(subscription)
    return subscription


@router.get("/health")
def platform_health(db: Session = Depends(get_db)):
    return {"status": "ok", "organizations": db.query(Organization).count()}


@router.get("/feature-flags")
def list_feature_flags(db: Session = Depends(get_db)):
    return db.query(FeatureFlag).order_by(FeatureFlag.organization_id, FeatureFlag.key).all()


@router.put("/feature-flags/{flag_id}")
def update_feature_flag(flag_id: int, payload: FeatureFlagUpdate, db: Session = Depends(get_db)):
    flag = db.query(FeatureFlag).filter(FeatureFlag.id == flag_id).first()
    if not flag:
        raise HTTPException(status_code=404, detail="Feature flag not found")
    old = {"enabled": flag.enabled, "configuration": flag.configuration}
    flag.enabled = payload.enabled
    flag.configuration = payload.configuration
    record_audit(
        db, organization_id=flag.organization_id, action="feature_flag.changed",
        target_type="feature_flag", target_id=str(flag.id),
        old_value=old, new_value=payload.model_dump(),
    )
    db.commit()
    return {"id": flag.id, "key": flag.key, "enabled": flag.enabled, "configuration": flag.configuration}
