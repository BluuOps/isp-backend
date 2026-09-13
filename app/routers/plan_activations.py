from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.authorization import (
    AuthenticatedPrincipal,
    Permission,
    get_authenticated_principal,
    require_permission,
)
from app.database import get_db
from app.schemas.plan_activation import (
    DuplicateResolutionRequest,
    DuplicateResolutionResponse,
    PlanActivationRequest,
    PlanActivationSummary,
)
from app.services.plan_activation import (
    activate_plan_change,
    get_plan_activation,
    list_plan_activations,
    resolve_duplicate_plan_payments,
)


router = APIRouter(prefix="/organization/plan-activations", tags=["plan-activations"])
activation_permission = Depends(require_permission(Permission.PAYMENTS_PLAN_ACTIVATE))


@router.get("", response_model=list[PlanActivationSummary], dependencies=[activation_permission])
def list_pending_plan_activations(
    db: Session = Depends(get_db),
    principal: AuthenticatedPrincipal = Depends(get_authenticated_principal),
) -> list[PlanActivationSummary]:
    return list_plan_activations(db, principal)


@router.get("/{payment_id}", response_model=PlanActivationSummary, dependencies=[activation_permission])
def retrieve_plan_activation(
    payment_id: int,
    db: Session = Depends(get_db),
    principal: AuthenticatedPrincipal = Depends(get_authenticated_principal),
) -> PlanActivationSummary:
    return get_plan_activation(db, principal, payment_id)


@router.post("/{payment_id}/activate", response_model=PlanActivationSummary, dependencies=[activation_permission])
def activate_pending_plan_change(
    payment_id: int,
    payload: PlanActivationRequest,
    db: Session = Depends(get_db),
    principal: AuthenticatedPrincipal = Depends(get_authenticated_principal),
) -> PlanActivationSummary:
    try:
        result = activate_plan_change(db, principal, payment_id=payment_id, correlation_id=payload.correlation_id)
        db.commit()
        return result
    except HTTPException:
        db.commit()
        raise
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Plan activation conflicts with an existing activation") from exc
    except Exception:
        db.rollback()
        raise


@router.post("/{payment_id}/resolve-duplicates", response_model=DuplicateResolutionResponse, dependencies=[activation_permission])
def resolve_duplicate_plan_changes(
    payment_id: int,
    payload: DuplicateResolutionRequest,
    db: Session = Depends(get_db),
    principal: AuthenticatedPrincipal = Depends(get_authenticated_principal),
) -> DuplicateResolutionResponse:
    try:
        canonical, duplicates = resolve_duplicate_plan_payments(
            db,
            principal,
            payment_id=payment_id,
            canonical_payment_id=payload.canonical_payment_id,
            correlation_id=payload.correlation_id,
            reason=payload.reason,
        )
        db.commit()
        return DuplicateResolutionResponse(
            logical_period_key=canonical.activation_period_key,
            canonical_payment_id=canonical.id,
            duplicate_payment_ids=duplicates,
            resolution_status="canonical_selected",
        )
    except HTTPException:
        db.commit()
        raise
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Duplicate resolution conflicts with an existing decision") from exc
    except Exception:
        db.rollback()
        raise
