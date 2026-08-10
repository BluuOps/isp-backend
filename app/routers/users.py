from typing import List

import calendar
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.authorization import (
    AuthenticatedPrincipal,
    Permission,
    get_authenticated_principal,
    require_permission,
)
from app.core.tenant import OrganizationContext, get_organization_context
from app.core.errors import conflict
from app.database import get_db
from app.models import BillingAccount, Customer, RadCheck, RadReply, ServicePlan, User, Zone
from app.schemas import (
    UserActivateResponse,
    UserCreate,
    UserDeleteResponse,
    UserPendingResponse,
    UserPlanChange,
    UserRecharge,
    UserResponse,
    UserSuspendResponse,
    UserTerminateResponse,
    UserUpdate,
)
from app.services.audit import record_audit
from app.services.radius_authorization import synchronize_radius_authorization


router = APIRouter(prefix="/users", tags=["Users"])

STATUS_ACTIVE = "active"
STATUS_SUSPENDED = "suspended"
STATUS_PENDING = "pending"
STATUS_TERMINATED = "terminated"


def get_user_or_404(user_id: int, db: Session, organization_id: int) -> User:
    user = db.query(User).filter(
        User.id == user_id,
        User.organization_id == organization_id,
    ).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Subscriber not found")
    return user


def get_active_plan_or_400(plan_name: str, db: Session, organization_id: int) -> ServicePlan:
    plan = (
        db.query(ServicePlan)
        .filter(
            ServicePlan.name == plan_name,
            ServicePlan.organization_id == organization_id,
            ServicePlan.status == "active",
        )
        .first()
    )
    if not plan:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Selected service plan does not exist or is inactive",
        )
    return plan


def get_active_zone_or_400(zone_name: str, db: Session, organization_id: int) -> Zone:
    normalized_name = zone_name.strip()
    if not normalized_name or normalized_name.isdigit():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Select an active Zone by name, not by numeric ID",
        )

    zone = (
        db.query(Zone)
        .filter(
            Zone.name == normalized_name,
            Zone.organization_id == organization_id,
            Zone.status == "active",
        )
        .first()
    )
    if not zone:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Selected Zone does not exist or is inactive for this organization",
        )
    return zone


def ensure_username_available(username: str, db: Session, user_id: int | None = None) -> None:
    query = db.query(User).filter(User.username == username)
    if user_id is not None:
        query = query.filter(User.id != user_id)

    if query.first():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Username already exists",
        )


def add_calendar_months(value: datetime, months: int) -> datetime:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def sync_radius_username(db: Session, old_username: str, new_username: str) -> None:
    if old_username == new_username:
        return

    for row in db.query(RadCheck).filter(RadCheck.username == old_username).all():
        row.username = new_username
    for row in db.query(RadReply).filter(RadReply.username == old_username).all():
        row.username = new_username


def provision_active_radius(db: Session, user: User, plan: ServicePlan) -> None:
    synchronize_radius_authorization(db, user, plan)


def enforce_user_delete_policy(db: Session, user: User) -> None:
    if user.status != STATUS_TERMINATED:
        raise conflict(
            "user_not_terminated",
            "Terminate subscriber before deletion.",
        )

    if db.query(BillingAccount).filter(BillingAccount.user_id == user.id).first():
        raise conflict(
            "user_has_billing_account",
            "Remove or archive the subscriber billing account before deletion.",
        )

    radius_rows = (
        db.query(RadCheck).filter(RadCheck.username == user.username).first()
        or db.query(RadReply).filter(RadReply.username == user.username).first()
    )
    if radius_rows:
        raise conflict(
            "user_has_radius_records",
            "Remove dependent RADIUS provisioning records before deletion.",
        )


def apply_radius_lifecycle(
    db: Session,
    user: User,
    plan: ServicePlan | None = None,
) -> None:
    if user.status not in {STATUS_ACTIVE, STATUS_SUSPENDED, STATUS_PENDING, STATUS_TERMINATED}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid subscriber lifecycle status",
        )
    if plan is None and user.status in {STATUS_ACTIVE, STATUS_SUSPENDED}:
        plan = get_active_plan_or_400(user.service_plan, db, user.organization_id)
    synchronize_radius_authorization(db, user, plan)


@router.get(
    "/",
    response_model=List[UserResponse],
    dependencies=[Depends(require_permission(Permission.SUBSCRIBERS_READ))],
)
def list_users(
    db: Session = Depends(get_db),
    organization: OrganizationContext = Depends(get_organization_context),
) -> list[User]:
    return db.query(User).filter(
        User.organization_id == organization.id
    ).order_by(User.id.asc()).all()


@router.post(
    "/",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission(Permission.SUBSCRIBERS_CREATE))],
)
def create_user(
    payload: UserCreate,
    db: Session = Depends(get_db),
    organization: OrganizationContext = Depends(get_organization_context),
) -> User:
    ensure_username_available(payload.username, db)
    plan = get_active_plan_or_400(payload.service_plan, db, organization.id)
    customer = db.query(Customer).filter(
        Customer.id == payload.customer_id,
        Customer.organization_id == organization.id,
    ).first()
    if not customer:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Selected CRM customer does not exist",
        )

    zone_name = get_active_zone_or_400(payload.zone, db, organization.id).name if payload.zone else None
    user = User(
        organization_id=organization.id,
        username=payload.username,
        password=payload.password,
        customer_id=customer.id,
        service_plan=plan.name,
        zone=zone_name,
        status=payload.status,
    )

    try:
        db.add(user)
        apply_radius_lifecycle(db, user, plan)
        record_audit(
            db,
            organization_id=organization.id,
            actor="internal-admin",
            action="pppoe.created",
            target_type="user",
            target_id=user.username,
            new_value={"customer_id": customer.id, "service_plan": plan.name, "status": user.status},
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Provisioning failed because a duplicate record already exists",
        ) from exc
    except Exception:
        db.rollback()
        raise

    db.refresh(user)
    return user


@router.put(
    "/{user_id}",
    response_model=UserResponse,
    dependencies=[Depends(require_permission(Permission.SUBSCRIBERS_UPDATE))],
)
def update_user(
    user_id: int,
    payload: UserUpdate,
    db: Session = Depends(get_db),
    organization: OrganizationContext = Depends(get_organization_context),
    principal: AuthenticatedPrincipal = Depends(get_authenticated_principal),
) -> User:
    user = get_user_or_404(user_id, db, organization.id)
    update_data = payload.model_dump(exclude_unset=True)
    if (
        "password" in update_data
        and not principal.has_permission(Permission.SUBSCRIBERS_PASSWORD_CHANGE)
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Insufficient permission to change subscriber credentials",
        )
    if (
        "service_plan" in update_data
        and not principal.has_permission(Permission.SUBSCRIBERS_PLAN_CHANGE)
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Insufficient permission to change subscriber plan",
        )
    requested_status = update_data.get("status")
    status_permission = {
        STATUS_SUSPENDED: Permission.SUBSCRIBERS_SUSPEND,
        STATUS_ACTIVE: Permission.SUBSCRIBERS_RECONNECT,
        STATUS_PENDING: Permission.SUBSCRIBERS_UPDATE,
        STATUS_TERMINATED: Permission.SUBSCRIBERS_DELETE,
    }.get(requested_status)
    if status_permission and not principal.has_permission(status_permission):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Insufficient permission for subscriber lifecycle change",
        )
    old_username = user.username
    old = {"username": user.username, "service_plan": user.service_plan, "status": user.status, "zone": user.zone}

    if "username" in update_data:
        ensure_username_available(update_data["username"], db, user_id=user.id)
        user.username = update_data["username"]

    if "password" in update_data:
        user.password = update_data["password"]

    if "service_plan" in update_data:
        plan = get_active_plan_or_400(update_data["service_plan"], db, organization.id)
        user.service_plan = plan.name
    else:
        plan = get_active_plan_or_400(user.service_plan, db, organization.id)

    if "zone" in update_data:
        if update_data["zone"] is None:
            user.zone = None
        else:
            user.zone = get_active_zone_or_400(update_data["zone"], db, organization.id).name

    if "expiration_date" in update_data:
        if update_data["expiration_date"] is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Expiration date cannot be empty",
            )
        user.expiration_date = update_data["expiration_date"]

    if "status" in update_data:
        user.status = update_data["status"]

    try:
        sync_radius_username(db, old_username, user.username)

        apply_radius_lifecycle(db, user, plan)
        record_audit(
            db,
            organization_id=organization.id,
            actor="internal-admin",
            action="pppoe.updated",
            target_type="user",
            target_id=str(user.id),
            old_value=old,
            new_value={"username": user.username, "service_plan": user.service_plan, "status": user.status, "zone": user.zone},
        )

        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Subscriber update failed because a duplicate record already exists",
        ) from exc
    except Exception:
        db.rollback()
        raise

    db.refresh(user)
    return user


@router.post(
    "/{user_id}/recharge",
    response_model=UserResponse,
    dependencies=[Depends(require_permission(Permission.SUBSCRIBERS_RECHARGE))],
)
def recharge_user(
    user_id: int,
    payload: UserRecharge,
    db: Session = Depends(get_db),
    organization: OrganizationContext = Depends(get_organization_context),
) -> User:
    user = get_user_or_404(user_id, db, organization.id)
    if user.status == STATUS_TERMINATED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Terminated PPPoE accounts cannot be recharged",
        )
    if not user.customer_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="PPPoE account must be linked to a CRM customer before recharge",
        )
    if not db.query(Customer).filter(
        Customer.id == user.customer_id,
        Customer.organization_id == organization.id,
    ).first():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Linked CRM customer does not exist",
        )

    plan = (
        db.query(ServicePlan)
        .filter(
            ServicePlan.id == payload.plan_id,
            ServicePlan.organization_id == organization.id,
            ServicePlan.status == "active",
        )
        .first()
    )
    if not plan:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Selected service plan does not exist or is inactive",
        )

    now = datetime.now(timezone.utc)
    old = {"service_plan": user.service_plan, "expiration_date": user.expiration_date.isoformat() if user.expiration_date else None, "status": user.status}
    extension_base = (
        user.expiration_date
        if user.expiration_date and user.expiration_date > now
        else now
    )
    try:
        user.service_plan = plan.name
        user.expiration_date = add_calendar_months(extension_base, payload.quantity)
        user.status = STATUS_ACTIVE
        provision_active_radius(db, user, plan)
        record_audit(
            db,
            organization_id=organization.id,
            actor="internal-admin",
            action="pppoe.recharged",
            target_type="user",
            target_id=str(user.id),
            old_value=old,
            new_value={"service_plan": user.service_plan, "expiration_date": user.expiration_date.isoformat(), "quantity": payload.quantity},
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(user)
    return user


@router.put(
    "/{user_id}/suspend",
    response_model=UserSuspendResponse,
    dependencies=[Depends(require_permission(Permission.SUBSCRIBERS_SUSPEND))],
)
def suspend_user(user_id: int, db: Session = Depends(get_db), organization: OrganizationContext = Depends(get_organization_context)) -> UserSuspendResponse:
    user = get_user_or_404(user_id, db, organization.id)
    plan = get_active_plan_or_400(user.service_plan, db, organization.id)

    try:
        old_status = user.status
        user.status = STATUS_SUSPENDED
        apply_radius_lifecycle(db, user, plan)
        record_audit(
            db,
            organization_id=organization.id,
            actor="internal-admin",
            action="pppoe.suspended",
            target_type="user",
            target_id=str(user.id),
            old_value={"status": old_status},
            new_value={"status": user.status},
        )
        db.commit()
    except Exception:
        db.rollback()
        raise

    return UserSuspendResponse(
        id=user.id,
        username=user.username,
        status=user.status,
        service_plan=user.service_plan,
        message="Subscriber suspended; PPPoE authentication is blocked",
    )


@router.put(
    "/{user_id}/activate",
    response_model=UserActivateResponse,
    dependencies=[Depends(require_permission(Permission.SUBSCRIBERS_RECONNECT))],
)
def activate_user(user_id: int, db: Session = Depends(get_db), organization: OrganizationContext = Depends(get_organization_context)) -> UserActivateResponse:
    user = get_user_or_404(user_id, db, organization.id)
    plan = get_active_plan_or_400(user.service_plan, db, organization.id)

    try:
        old_status = user.status
        user.status = STATUS_ACTIVE
        apply_radius_lifecycle(db, user, plan)
        record_audit(
            db,
            organization_id=organization.id,
            actor="internal-admin",
            action="pppoe.reconnected",
            target_type="user",
            target_id=str(user.id),
            old_value={"status": old_status},
            new_value={"status": user.status},
        )
        db.commit()
    except Exception:
        db.rollback()
        raise

    return UserActivateResponse(
        id=user.id,
        username=user.username,
        status=user.status,
        service_plan=user.service_plan,
        message="Subscriber activated; PPPoE authentication is restored",
    )


@router.put(
    "/{user_id}/pending",
    response_model=UserPendingResponse,
    dependencies=[Depends(require_permission(Permission.SUBSCRIBERS_UPDATE))],
)
def mark_user_pending(user_id: int, db: Session = Depends(get_db), organization: OrganizationContext = Depends(get_organization_context)) -> UserPendingResponse:
    user = get_user_or_404(user_id, db, organization.id)
    try:
        old_status = user.status
        user.status = STATUS_PENDING
        apply_radius_lifecycle(db, user)
        record_audit(
            db,
            organization_id=organization.id,
            actor="internal-admin",
            action="pppoe.pending",
            target_type="user",
            target_id=str(user.id),
            old_value={"status": old_status},
            new_value={"status": user.status},
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    return UserPendingResponse(
        id=user.id,
        username=user.username,
        status=user.status,
        service_plan=user.service_plan,
        message="Subscriber marked pending; PPPoE authentication is blocked",
    )


@router.put(
    "/{user_id}/terminate",
    response_model=UserTerminateResponse,
    dependencies=[Depends(require_permission(Permission.SUBSCRIBERS_DELETE))],
)
def terminate_user(user_id: int, db: Session = Depends(get_db), organization: OrganizationContext = Depends(get_organization_context)) -> UserTerminateResponse:
    user = get_user_or_404(user_id, db, organization.id)
    try:
        old_status = user.status
        user.status = STATUS_TERMINATED
        apply_radius_lifecycle(db, user)
        record_audit(
            db,
            organization_id=organization.id,
            actor="internal-admin",
            action="pppoe.terminated",
            target_type="user",
            target_id=str(user.id),
            old_value={"status": old_status},
            new_value={"status": user.status},
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    return UserTerminateResponse(
        id=user.id,
        username=user.username,
        status=user.status,
        service_plan=user.service_plan,
        message="Subscriber terminated; PPPoE authentication is blocked",
    )


@router.put(
    "/{user_id}/plan",
    response_model=UserResponse,
    dependencies=[Depends(require_permission(Permission.SUBSCRIBERS_PLAN_CHANGE))],
)
def change_user_plan(user_id: int, payload: UserPlanChange, db: Session = Depends(get_db), organization: OrganizationContext = Depends(get_organization_context)) -> User:
    user = get_user_or_404(user_id, db, organization.id)
    plan = get_active_plan_or_400(payload.service_plan, db, organization.id)

    try:
        old_plan = user.service_plan
        user.service_plan = plan.name
        apply_radius_lifecycle(db, user, plan)
        record_audit(
            db,
            organization_id=organization.id,
            actor="internal-admin",
            action="pppoe.plan_changed",
            target_type="user",
            target_id=str(user.id),
            old_value={"service_plan": old_plan},
            new_value={"service_plan": user.service_plan},
        )
        db.commit()
    except Exception:
        db.rollback()
        raise

    db.refresh(user)
    return user


@router.delete(
    "/{user_id}",
    response_model=UserDeleteResponse,
    dependencies=[Depends(require_permission(Permission.SUBSCRIBERS_DELETE))],
)
def delete_user(user_id: int, db: Session = Depends(get_db), organization: OrganizationContext = Depends(get_organization_context)) -> UserDeleteResponse:
    user = get_user_or_404(user_id, db, organization.id)
    enforce_user_delete_policy(db, user)
    response = UserDeleteResponse(
        id=user.id,
        username=user.username,
        message="Subscriber deleted",
    )

    try:
        record_audit(
            db,
            organization_id=organization.id,
            actor="internal-admin",
            action="pppoe.deleted",
            target_type="user",
            target_id=str(user.id),
            old_value={"username": user.username, "customer_id": user.customer_id, "service_plan": user.service_plan},
        )
        db.delete(user)
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise conflict(
            "user_has_linked_records",
            "Terminate subscriber and remove dependent records before deletion.",
        ) from exc

    return response
