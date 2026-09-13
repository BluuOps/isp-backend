from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from app.core.authorization import (
    ASSIGNABLE_ORGANIZATION_ROLES,
    Permission,
    require_permission,
)
from app.core.tenant import OrganizationContext, get_organization_context
from app.database import get_db
from app.models import (
    AuditLog,
    FeatureFlag,
    NotificationSetting,
    Organization,
    OrganizationStaff,
    Subscription,
)
from app.schemas import AuditLogListResponse
from app.schemas.management import (
    NotificationSettingsUpdate,
    OrganizationResponse,
    OrganizationUpdate,
    StaffCreate,
    StaffResponse,
    StaffUpdate,
    SubscriptionResponse,
)
from app.services.audit import record_audit
from app.services.limits import enforce_limit
from app.services.security import generate_temporary_password, hash_password

router = APIRouter(prefix="/organization", tags=["Organization Management"])


def current_organization(db: Session, context: OrganizationContext) -> Organization:
    organization = db.query(Organization).filter(Organization.id == context.id).first()
    if not organization:
        raise HTTPException(status_code=404, detail="Organization not found")
    return organization


@router.get(
    "/profile",
    response_model=OrganizationResponse,
    dependencies=[Depends(require_permission(Permission.ORGANIZATION_PROFILE_READ))],
)
def get_profile(db: Session = Depends(get_db), context: OrganizationContext = Depends(get_organization_context)):
    return current_organization(db, context)


@router.put(
    "/profile",
    response_model=OrganizationResponse,
    dependencies=[Depends(require_permission(Permission.ORGANIZATION_PROFILE_UPDATE))],
)
def update_profile(payload: OrganizationUpdate, db: Session = Depends(get_db), context: OrganizationContext = Depends(get_organization_context)):
    organization = current_organization(db, context)
    allowed = {"name", "company_email", "company_phone", "website", "country", "timezone", "currency", "logo"}
    changes = {k: v for k, v in payload.model_dump(exclude_unset=True).items() if k in allowed}
    old = {key: getattr(organization, key) for key in changes}
    for field, value in changes.items():
        setattr(organization, field, value)
    record_audit(db, organization_id=organization.id, actor="organization-admin", action="organization.profile_updated", target_type="organization", target_id=str(organization.id), old_value=old, new_value=changes)
    db.commit()
    db.refresh(organization)
    return organization


@router.get(
    "/settings",
    dependencies=[Depends(require_permission(Permission.ORGANIZATION_SETTINGS_READ))],
)
def get_settings(db: Session = Depends(get_db), context: OrganizationContext = Depends(get_organization_context)):
    settings = db.query(NotificationSetting).filter(NotificationSetting.organization_id == context.id).first()
    if not settings:
        raise HTTPException(status_code=404, detail="Organization settings not found")
    return settings


@router.put(
    "/settings",
    dependencies=[Depends(require_permission(Permission.ORGANIZATION_SETTINGS_UPDATE))],
)
def update_settings(payload: NotificationSettingsUpdate, db: Session = Depends(get_db), context: OrganizationContext = Depends(get_organization_context)):
    settings = db.query(NotificationSetting).filter(NotificationSetting.organization_id == context.id).first()
    if not settings:
        raise HTTPException(status_code=404, detail="Organization settings not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(settings, field, value)
    db.commit()
    return settings


@router.get(
    "/staff",
    response_model=list[StaffResponse],
    dependencies=[Depends(require_permission(Permission.ORGANIZATION_STAFF_READ))],
)
def list_staff(db: Session = Depends(get_db), context: OrganizationContext = Depends(get_organization_context)):
    return db.query(OrganizationStaff).filter(OrganizationStaff.organization_id == context.id).order_by(OrganizationStaff.id).all()


@router.post(
    "/staff",
    status_code=201,
    dependencies=[Depends(require_permission(Permission.ORGANIZATION_STAFF_MANAGE))],
)
def create_staff(payload: StaffCreate, db: Session = Depends(get_db), context: OrganizationContext = Depends(get_organization_context)):
    if payload.role not in ASSIGNABLE_ORGANIZATION_ROLES:
        raise HTTPException(status_code=400, detail="Invalid organization role")
    organization = current_organization(db, context)
    enforce_limit(db, organization, "staff")
    duplicate = db.query(OrganizationStaff).filter(OrganizationStaff.organization_id == context.id, OrganizationStaff.email == payload.email).first()
    if duplicate:
        raise HTTPException(status_code=409, detail="Staff email already exists")
    temporary = generate_temporary_password()
    staff = OrganizationStaff(
        organization_id=context.id, name=payload.name, email=payload.email,
        password_hash=hash_password(temporary), role=payload.role,
        status="active", is_temporary_password=True,
    )
    db.add(staff)
    db.flush()
    record_audit(db, organization_id=context.id, actor="organization-admin", action="staff.added", target_type="staff", target_id=str(staff.id), new_value={"name": staff.name, "email": staff.email, "role": staff.role})
    db.commit()
    db.refresh(staff)
    return {"staff": StaffResponse.model_validate(staff), "temporary_password": temporary}


@router.put(
    "/staff/{staff_id}",
    response_model=StaffResponse,
    dependencies=[Depends(require_permission(Permission.ORGANIZATION_STAFF_MANAGE))],
)
def update_staff(staff_id: int, payload: StaffUpdate, db: Session = Depends(get_db), context: OrganizationContext = Depends(get_organization_context)):
    staff = db.query(OrganizationStaff).filter(OrganizationStaff.id == staff_id, OrganizationStaff.organization_id == context.id).first()
    if not staff:
        raise HTTPException(status_code=404, detail="Staff member not found")
    update_data = payload.model_dump(exclude_unset=True)
    if "role" in update_data and update_data["role"] not in ASSIGNABLE_ORGANIZATION_ROLES:
        raise HTTPException(status_code=400, detail="Invalid organization role")
    for field, value in update_data.items():
        setattr(staff, field, value)
    db.commit()
    db.refresh(staff)
    return staff


@router.delete(
    "/staff/{staff_id}",
    status_code=204,
    dependencies=[Depends(require_permission(Permission.ORGANIZATION_STAFF_MANAGE))],
)
def delete_staff(staff_id: int, db: Session = Depends(get_db), context: OrganizationContext = Depends(get_organization_context)):
    staff = db.query(OrganizationStaff).filter(OrganizationStaff.id == staff_id, OrganizationStaff.organization_id == context.id).first()
    if not staff:
        raise HTTPException(status_code=404, detail="Staff member not found")
    if staff.role == "Organization Admin" and db.query(OrganizationStaff).filter(OrganizationStaff.organization_id == context.id, OrganizationStaff.role == "Organization Admin", OrganizationStaff.status == "active").count() <= 1:
        raise HTTPException(status_code=409, detail="Cannot remove the last organization admin")
    record_audit(db, organization_id=context.id, actor="organization-admin", action="staff.removed", target_type="staff", target_id=str(staff.id), old_value={"name": staff.name, "email": staff.email, "role": staff.role})
    db.delete(staff)
    db.commit()
    return Response(status_code=204)


@router.get(
    "/subscription",
    response_model=SubscriptionResponse,
    dependencies=[Depends(require_permission(Permission.ORGANIZATION_SUBSCRIPTION_READ))],
)
def get_subscription(db: Session = Depends(get_db), context: OrganizationContext = Depends(get_organization_context)):
    subscription = db.query(Subscription).filter(Subscription.organization_id == context.id).order_by(Subscription.id.desc()).first()
    if not subscription:
        raise HTTPException(status_code=404, detail="Subscription not found")
    return subscription


@router.get(
    "/feature-flags",
    dependencies=[Depends(require_permission(Permission.ORGANIZATION_FEATURE_FLAGS_READ))],
)
def get_feature_flags(db: Session = Depends(get_db), context: OrganizationContext = Depends(get_organization_context)):
    global_flags = {flag.key: flag for flag in db.query(FeatureFlag).filter(FeatureFlag.organization_id.is_(None)).all()}
    overrides = {flag.key: flag for flag in db.query(FeatureFlag).filter(FeatureFlag.organization_id == context.id).all()}
    keys = sorted(set(global_flags) | set(overrides))
    return [{"key": key, "enabled": (overrides.get(key) or global_flags[key]).enabled, "configuration": (overrides.get(key) or global_flags[key]).configuration} for key in keys]


@router.get(
    "/audit-logs",
    response_model=AuditLogListResponse,
    dependencies=[Depends(require_permission(Permission.ORGANIZATION_AUDIT_LOGS_READ))],
)
def list_audit_logs(
    date_from: datetime | None = Query(default=None),
    date_to: datetime | None = Query(default=None),
    action: str | None = Query(default=None, max_length=100),
    actor: str | None = Query(default=None, max_length=255),
    actor_type: str | None = Query(default=None, max_length=50),
    actor_id: str | None = Query(default=None, max_length=255),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    context: OrganizationContext = Depends(get_organization_context),
) -> AuditLogListResponse:
    query = db.query(AuditLog).filter(AuditLog.organization_id == context.id)
    if date_from:
        query = query.filter(AuditLog.created_at >= date_from)
    if date_to:
        query = query.filter(AuditLog.created_at <= date_to)
    if action:
        query = query.filter(AuditLog.action == action)
    if actor:
        query = query.filter(AuditLog.actor == actor)
    if actor_type:
        query = query.filter(AuditLog.actor_type == actor_type)
    if actor_id:
        query = query.filter(AuditLog.actor_id == actor_id)

    total = query.count()
    items = query.order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).offset(offset).limit(limit).all()
    return AuditLogListResponse(items=items, total=total, limit=limit, offset=offset)
