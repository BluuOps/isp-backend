from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import conflict
from app.core.tenant import OrganizationContext, get_organization_context
from app.database import get_db
from app.models import ServicePlan, User
from app.schemas import ServicePlanCreate, ServicePlanResponse, ServicePlanUpdate


router = APIRouter(prefix="/plans", tags=["Service Plans"])


@router.get("", response_model=List[ServicePlanResponse])
def list_plans(
    db: Session = Depends(get_db),
    organization: OrganizationContext = Depends(get_organization_context),
) -> list[ServicePlan]:
    return (
        db.query(ServicePlan)
        .filter(ServicePlan.organization_id == organization.id)
        .order_by(ServicePlan.name.asc())
        .all()
    )


@router.post("", response_model=ServicePlanResponse, status_code=status.HTTP_201_CREATED)
def create_plan(
    payload: ServicePlanCreate,
    db: Session = Depends(get_db),
    organization: OrganizationContext = Depends(get_organization_context),
) -> ServicePlan:
    existing = (
        db.query(ServicePlan)
        .filter(
            ServicePlan.name == payload.name,
            ServicePlan.organization_id == organization.id,
        )
        .first()
    )
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Service plan already exists",
        )

    plan = ServicePlan(**payload.model_dump(), organization_id=organization.id)
    db.add(plan)

    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Service plan already exists",
        ) from exc

    db.refresh(plan)
    return plan


@router.put("/{plan_id}", response_model=ServicePlanResponse)
def update_plan(
    plan_id: int,
    payload: ServicePlanUpdate,
    db: Session = Depends(get_db),
    organization: OrganizationContext = Depends(get_organization_context),
) -> ServicePlan:
    plan = (
        db.query(ServicePlan)
        .filter(ServicePlan.id == plan_id, ServicePlan.organization_id == organization.id)
        .first()
    )
    if not plan:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service plan not found")

    update_data = payload.model_dump(exclude_unset=True)

    if "name" in update_data and update_data["name"] != plan.name:
        duplicate = (
            db.query(ServicePlan)
            .filter(
                ServicePlan.name == update_data["name"],
                ServicePlan.organization_id == organization.id,
            )
            .first()
        )
        if duplicate:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Service plan already exists",
            )

    for field, value in update_data.items():
        setattr(plan, field, value)

    db.commit()
    db.refresh(plan)
    return plan


@router.delete("/{plan_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_plan(
    plan_id: int,
    db: Session = Depends(get_db),
    organization: OrganizationContext = Depends(get_organization_context),
) -> None:
    plan = (
        db.query(ServicePlan)
        .filter(ServicePlan.id == plan_id, ServicePlan.organization_id == organization.id)
        .first()
    )
    if not plan:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service plan not found")

    users_on_plan = (
        db.query(User)
        .filter(
            User.service_plan == plan.name,
            User.organization_id == organization.id,
            User.status != "terminated",
        )
        .first()
    )
    if users_on_plan:
        raise conflict(
            "service_plan_has_active_subscribers",
            "Cannot delete a service plan assigned to non-terminated subscribers.",
        )

    try:
        db.delete(plan)
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise conflict(
            "service_plan_has_linked_records",
            "Cannot delete a service plan while linked subscriber records exist.",
        ) from exc
