from typing import List

import calendar
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Customer, RadCheck, RadReply, ServicePlan, User
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

router = APIRouter(prefix="/users", tags=["Users"])

MIKROTIK_RATE_LIMIT_ATTRIBUTE = "Mikrotik-Rate-Limit"
PASSWORD_ATTRIBUTE = "Cleartext-Password"
REJECT_ATTRIBUTE = "Auth-Type"
REJECT_VALUE = "Reject"

STATUS_ACTIVE = "active"
STATUS_SUSPENDED = "suspended"
STATUS_PENDING = "pending"
STATUS_TERMINATED = "terminated"


def get_user_or_404(user_id: int, db: Session) -> User:
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Subscriber not found")
    return user


def get_active_plan_or_400(plan_name: str, db: Session) -> ServicePlan:
    plan = (
        db.query(ServicePlan)
        .filter(ServicePlan.name == plan_name, ServicePlan.status == "active")
        .first()
    )
    if not plan:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Selected service plan does not exist or is inactive",
        )
    return plan


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


def upsert_radcheck(db: Session, username: str, attribute: str, value: str, op: str = ":=") -> RadCheck:
    row = db.query(RadCheck).filter(
        RadCheck.username == username,
        RadCheck.attribute == attribute,
    ).first()

    if row:
        row.op = op
        row.value = value
        return row

    row = RadCheck(username=username, attribute=attribute, op=op, value=value)
    db.add(row)
    return row


def upsert_radreply(db: Session, username: str, attribute: str, value: str, op: str = ":=") -> RadReply:
    row = db.query(RadReply).filter(
        RadReply.username == username,
        RadReply.attribute == attribute,
    ).first()

    if row:
        row.op = op
        row.value = value
        return row

    row = RadReply(username=username, attribute=attribute, op=op, value=value)
    db.add(row)
    return row


def remove_radcheck_attribute(db: Session, username: str, attribute: str) -> None:
    rows = db.query(RadCheck).filter(
        RadCheck.username == username,
        RadCheck.attribute == attribute,
    ).all()

    for row in rows:
        db.delete(row)


def sync_radius_username(db: Session, old_username: str, new_username: str) -> None:
    if old_username == new_username:
        return

    for row in db.query(RadCheck).filter(RadCheck.username == old_username).all():
        row.username = new_username

    for row in db.query(RadReply).filter(RadReply.username == old_username).all():
        row.username = new_username


def provision_active_radius(db: Session, user: User, plan: ServicePlan) -> None:
    remove_radcheck_attribute(db, user.username, REJECT_ATTRIBUTE)
    upsert_radcheck(db, user.username, PASSWORD_ATTRIBUTE, user.password)
    upsert_radreply(db, user.username, MIKROTIK_RATE_LIMIT_ATTRIBUTE, plan.rate_limit)


def block_radius_authentication(db: Session, username: str) -> None:
    upsert_radcheck(db, username, REJECT_ATTRIBUTE, REJECT_VALUE)


def delete_radius_provisioning(db: Session, username: str) -> None:
    for row in db.query(RadCheck).filter(RadCheck.username == username).all():
        db.delete(row)

    for row in db.query(RadReply).filter(RadReply.username == username).all():
        db.delete(row)


def apply_radius_lifecycle(db: Session, user: User, plan: ServicePlan | None = None) -> None:
    if user.status == STATUS_ACTIVE:
        if plan is None:
            plan = get_active_plan_or_400(user.service_plan, db)
        provision_active_radius(db, user, plan)
        return

    if user.status == STATUS_SUSPENDED:
        if plan is None:
            plan = get_active_plan_or_400(user.service_plan, db)
        provision_active_radius(db, user, plan)
        block_radius_authentication(db, user.username)
        return

    if user.status in {STATUS_PENDING, STATUS_TERMINATED}:
        delete_radius_provisioning(db, user.username)
        block_radius_authentication(db, user.username)
        return

    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="Invalid subscriber lifecycle status",
    )


@router.get("/", response_model=List[UserResponse])
def list_users(db: Session = Depends(get_db)) -> list[User]:
    return db.query(User).order_by(User.id.asc()).all()


@router.post("/", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
def create_user(payload: UserCreate, db: Session = Depends(get_db)) -> User:
    ensure_username_available(payload.username, db)
    plan = get_active_plan_or_400(payload.service_plan, db)

    customer = db.query(Customer).filter(
        Customer.id == payload.customer_id
    ).first()

    if not customer:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Selected CRM customer does not exist",
        )

    user = User(
        username=payload.username,
        password=payload.password,
        customer_id=customer.id,
        service_plan=plan.name,
        zone=payload.zone,
        status=payload.status,
    )

    try:
        db.add(user)
        apply_radius_lifecycle(db, user, plan)
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


@router.put("/{user_id}", response_model=UserResponse)
def update_user(user_id: int, payload: UserUpdate, db: Session = Depends(get_db)) -> User:
    user = get_user_or_404(user_id, db)
    update_data = payload.model_dump(exclude_unset=True)
    old_username = user.username

    if "username" in update_data:
        ensure_username_available(update_data["username"], db, user_id=user.id)
        user.username = update_data["username"]

    if "password" in update_data:
        user.password = update_data["password"]

    if "service_plan" in update_data:
        plan = get_active_plan_or_400(update_data["service_plan"], db)
        user.service_plan = plan.name
    else:
        plan = get_active_plan_or_400(user.service_plan, db)

    if "zone" in update_data:
        user.zone = update_data["zone"]

    if "status" in update_data:
        user.status = update_data["status"]

    try:
        sync_radius_username(db, old_username, user.username)
        apply_radius_lifecycle(db, user, plan)
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


@router.post("/{user_id}/recharge", response_model=UserResponse)
def recharge_user(
    user_id: int,
    payload: UserRecharge,
    db: Session = Depends(get_db),
) -> User:
    user = get_user_or_404(user_id, db)

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

    customer = db.query(Customer).filter(Customer.id == user.customer_id).first()
    if not customer:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Linked CRM customer does not exist",
        )

    plan = (
        db.query(ServicePlan)
        .filter(
            ServicePlan.id == payload.plan_id,
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
    extension_base = (
        user.expiration_date
        if user.expiration_date and user.expiration_date > now
        else now
    )

    try:
        user.service_plan = plan.name
        user.expiration_date = add_calendar_months(
            extension_base,
            payload.quantity,
        )
        user.status = STATUS_ACTIVE
        provision_active_radius(db, user, plan)
        db.commit()
    except Exception:
        db.rollback()
        raise

    db.refresh(user)
    return user

@router.put("/{user_id}/suspend", response_model=UserSuspendResponse)
def suspend_user(user_id: int, db: Session = Depends(get_db)) -> UserSuspendResponse:
    user = get_user_or_404(user_id, db)
    plan = get_active_plan_or_400(user.service_plan, db)

    try:
        user.status = STATUS_SUSPENDED
        apply_radius_lifecycle(db, user, plan)
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


@router.put("/{user_id}/activate", response_model=UserActivateResponse)
def activate_user(user_id: int, db: Session = Depends(get_db)) -> UserActivateResponse:
    user = get_user_or_404(user_id, db)
    plan = get_active_plan_or_400(user.service_plan, db)

    try:
        user.status = STATUS_ACTIVE
        apply_radius_lifecycle(db, user, plan)
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


@router.put("/{user_id}/pending", response_model=UserPendingResponse)
def mark_user_pending(user_id: int, db: Session = Depends(get_db)) -> UserPendingResponse:
    user = get_user_or_404(user_id, db)

    try:
        user.status = STATUS_PENDING
        apply_radius_lifecycle(db, user)
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


@router.put("/{user_id}/terminate", response_model=UserTerminateResponse)
def terminate_user(user_id: int, db: Session = Depends(get_db)) -> UserTerminateResponse:
    user = get_user_or_404(user_id, db)

    try:
        user.status = STATUS_TERMINATED
        apply_radius_lifecycle(db, user)
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


@router.put("/{user_id}/plan", response_model=UserResponse)
def change_user_plan(user_id: int, payload: UserPlanChange, db: Session = Depends(get_db)) -> User:
    user = get_user_or_404(user_id, db)
    plan = get_active_plan_or_400(payload.service_plan, db)

    try:
        user.service_plan = plan.name
        apply_radius_lifecycle(db, user, plan)
        db.commit()
    except Exception:
        db.rollback()
        raise

    db.refresh(user)
    return user


@router.delete("/{user_id}", response_model=UserDeleteResponse)
def delete_user(user_id: int, db: Session = Depends(get_db)) -> UserDeleteResponse:
    user = get_user_or_404(user_id, db)
    response = UserDeleteResponse(
        id=user.id,
        username=user.username,
        message="Subscriber and active RADIUS provisioning rows deleted",
    )

    try:
        delete_radius_provisioning(db, user.username)
        db.delete(user)
        db.commit()
    except Exception:
        db.rollback()
        raise

    return response
