from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.authorization import AuthenticatedPrincipal
from app.core.platform_auth import require_platform_admin, require_recent_platform_admin
from app.core.principal import reject_customer_principal
from app.database import get_db
from app.models import (
    Customer,
    FeatureFlag,
    Organization,
    OrganizationAdminInvitation,
    Subscription,
    User,
)
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
from app.services.radius_session_freshness import fresh_active_session_conditions
from app.services.admin_invitations import (
    create_admin_invitation,
    database_now,
    revoke_admin_invitation,
)

router = APIRouter(
    prefix="/platform",
    tags=["Platform Administration"],
    dependencies=[Depends(require_platform_admin), Depends(reject_customer_principal)],
)


class AdminInvitationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=255)
    purpose: Literal["bootstrap", "invite", "recovery"]
    reason: str = Field(min_length=8, max_length=500)


class AdminInvitationCreated(BaseModel):
    id: int
    organization_id: int
    purpose: str
    expires_at: datetime
    invitation_token: str


class AdminInvitationStatus(BaseModel):
    id: int
    organization_id: int
    email: str
    purpose: str
    expires_at: datetime
    used_at: datetime | None
    revoked_at: datetime | None
    attempts_remaining: int
    correlation_id: str
    active: bool


def organization_or_404(db: Session, organization_id: int) -> Organization:
    organization = db.query(Organization).filter(Organization.id == organization_id).first()
    if not organization:
        raise HTTPException(status_code=404, detail="Organization not found")
    return organization


def _invitation_status(
    invitation: OrganizationAdminInvitation,
    *,
    now: datetime,
) -> AdminInvitationStatus:
    expires_at = invitation.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=now.tzinfo)
    return AdminInvitationStatus(
        id=invitation.id,
        organization_id=invitation.organization_id,
        email=invitation.email,
        purpose=invitation.purpose,
        expires_at=invitation.expires_at,
        used_at=invitation.used_at,
        revoked_at=invitation.revoked_at,
        attempts_remaining=max(0, invitation.max_attempts - invitation.attempt_count),
        correlation_id=invitation.correlation_id,
        active=(
            invitation.used_at is None
            and invitation.revoked_at is None
            and expires_at > now
            and invitation.attempt_count < invitation.max_attempts
        ),
    )


@router.post(
    "/organizations/{organization_id}/admin-invitations",
    response_model=AdminInvitationCreated,
    status_code=201,
)
def create_organization_admin_invitation(
    organization_id: int,
    payload: AdminInvitationCreate,
    response: Response,
    db: Session = Depends(get_db),
    principal: AuthenticatedPrincipal = Depends(require_recent_platform_admin),
) -> AdminInvitationCreated:
    try:
        result = create_admin_invitation(
            db,
            organization_id=organization_id,
            email=payload.email,
            purpose=payload.purpose,
            reason=payload.reason,
            principal=principal,
        )
        db.commit()
        db.refresh(result.invitation)
    except Exception:
        db.rollback()
        raise
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return AdminInvitationCreated(
        id=result.invitation.id,
        organization_id=result.invitation.organization_id,
        purpose=result.invitation.purpose,
        expires_at=result.invitation.expires_at,
        invitation_token=result.token,
    )


@router.get(
    "/organizations/{organization_id}/admin-invitations",
    response_model=list[AdminInvitationStatus],
)
def list_organization_admin_invitations(
    organization_id: int,
    db: Session = Depends(get_db),
    principal: AuthenticatedPrincipal = Depends(require_recent_platform_admin),
) -> list[AdminInvitationStatus]:
    del principal
    organization_or_404(db, organization_id)
    now = database_now(db)
    invitations = (
        db.query(OrganizationAdminInvitation)
        .filter(
            OrganizationAdminInvitation.organization_id == organization_id,
            OrganizationAdminInvitation.used_at.is_(None),
            OrganizationAdminInvitation.revoked_at.is_(None),
            OrganizationAdminInvitation.expires_at > now,
        )
        .order_by(OrganizationAdminInvitation.expires_at, OrganizationAdminInvitation.id)
        .all()
    )
    return [_invitation_status(item, now=now) for item in invitations]


@router.get(
    "/organizations/{organization_id}/admin-invitations/{invitation_id}",
    response_model=AdminInvitationStatus,
)
def get_organization_admin_invitation(
    organization_id: int,
    invitation_id: int,
    db: Session = Depends(get_db),
    principal: AuthenticatedPrincipal = Depends(require_recent_platform_admin),
) -> AdminInvitationStatus:
    del principal
    invitation = db.query(OrganizationAdminInvitation).filter(
        OrganizationAdminInvitation.id == invitation_id,
        OrganizationAdminInvitation.organization_id == organization_id,
    ).first()
    if not invitation:
        raise HTTPException(status_code=404, detail="Invitation not found")
    return _invitation_status(invitation, now=database_now(db))


@router.delete(
    "/organizations/{organization_id}/admin-invitations/{invitation_id}",
)
def revoke_organization_admin_invitation(
    organization_id: int,
    invitation_id: int,
    db: Session = Depends(get_db),
    principal: AuthenticatedPrincipal = Depends(require_recent_platform_admin),
) -> dict[str, str]:
    invitation = (
        db.query(OrganizationAdminInvitation)
        .filter(
            OrganizationAdminInvitation.id == invitation_id,
            OrganizationAdminInvitation.organization_id == organization_id,
        )
        .with_for_update()
        .first()
    )
    if not invitation:
        raise HTTPException(status_code=404, detail="Invitation not found")
    changed = revoke_admin_invitation(db, invitation=invitation, principal=principal)
    db.commit()
    return {"status": "revoked" if changed else "already_revoked"}


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
        "online_sessions": db.query(RadAcct).filter(*fresh_active_session_conditions()).count(),
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
