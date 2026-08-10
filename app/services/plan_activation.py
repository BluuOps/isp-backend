from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.core.authorization import AuthenticatedPrincipal
from app.models import PaymentTransaction, ServicePlan, User
from app.schemas.plan_activation import PlanActivationSummary
from app.services.audit import record_audit


BLOCKED_SERVICE_STATUSES = {"disabled", "deleted", "terminated", "cancelled"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _staff_id(principal: AuthenticatedPrincipal) -> int:
    try:
        return int(principal.subject_id.split(":", 1)[1])
    except (IndexError, ValueError) as exc:
        raise HTTPException(status_code=401, detail="Invalid organization staff identity") from exc


def _raise_activation_conflict(
    db: Session,
    payment: PaymentTransaction,
    principal: AuthenticatedPrincipal,
    correlation_id: str,
    reason: str,
) -> None:
    record_audit(
        db,
        organization_id=payment.organization_id,
        actor_type="organization_staff",
        actor_id=str(_staff_id(principal)),
        actor_label=principal.actor_label,
        actor=principal.actor_label,
        action="plan_activation.conflict",
        target_type="payment",
        target_id=str(payment.id),
        success=False,
        new_value={
            "payment_id": payment.id,
            "customer_id": payment.customer_id,
            "service_id": payment.user_id,
            "correlation_id": correlation_id,
            "reason": reason,
        },
    )
    raise HTTPException(status_code=409, detail=reason)


def _reference_suffix(payment: PaymentTransaction) -> str:
    value = payment.gateway_reference or payment.transaction_reference
    return "***" + value[-6:] if value else "unavailable"


def _competitors(db: Session, payment: PaymentTransaction, *, lock: bool = False) -> list[PaymentTransaction]:
    if not payment.activation_period_key:
        return [payment]
    query = db.query(PaymentTransaction).filter(
        PaymentTransaction.organization_id == payment.organization_id,
        PaymentTransaction.activation_period_key == payment.activation_period_key,
        PaymentTransaction.payment_status.in_(("successful", "paid")),
        PaymentTransaction.activation_status.in_(("pending_activation", "blocked_duplicate", "activated")),
    ).order_by(PaymentTransaction.id.asc())
    if lock:
        query = query.with_for_update()
    return query.all()


def _eligibility(payment: PaymentTransaction, service: User, competitors: list[PaymentTransaction]) -> tuple[bool, str | None]:
    if payment.activation_status == "activated":
        return False, "already_activated"
    if payment.payment_status not in {"successful", "paid"} or not payment.verified_at:
        return False, "payment_not_verified_successful"
    if payment.fulfillment_status != "pending_activation" or payment.activation_status not in {"pending_activation", "blocked_duplicate"}:
        return False, "payment_not_pending_activation"
    if service.status in BLOCKED_SERVICE_STATUSES:
        return False, "service_status_not_activatable"
    if len(competitors) > 1:
        canonical = [item for item in competitors if item.resolution_status == "canonical"]
        if len(canonical) != 1:
            return False, "duplicate_payment_ambiguity"
        if canonical[0].id != payment.id:
            return False, "non_canonical_duplicate"
    return True, None


def _proposed_expiration(payment: PaymentTransaction, service: User, at: datetime) -> datetime:
    duration = payment.purchased_duration_days
    if not duration or duration < 1:
        raise HTTPException(status_code=409, detail="Purchased plan duration is unavailable")
    current = _aware(service.expiration_date)
    base = current if current and current > at else at
    return base + timedelta(days=duration * max(1, int(payment.billing_periods or 1)))


def _summary(db: Session, payment: PaymentTransaction, *, at: datetime | None = None) -> PlanActivationSummary:
    service = db.query(User).filter(
        User.id == payment.user_id,
        User.organization_id == payment.organization_id,
        User.customer_id == payment.customer_id,
    ).first()
    plan = db.query(ServicePlan).filter(
        ServicePlan.id == payment.selected_plan_id,
        ServicePlan.organization_id == payment.organization_id,
    ).first()
    if not service or not plan:
        raise HTTPException(status_code=409, detail="Plan activation evidence is incomplete")
    competitors = _competitors(db, payment)
    eligible, reason = _eligibility(payment, service, competitors)
    proposed = payment.new_expiration_date if payment.activation_status == "activated" else _proposed_expiration(payment, service, at or _now())
    return PlanActivationSummary(
        payment_id=payment.id,
        service_id=service.id,
        customer_id=payment.customer_id,
        current_plan=service.service_plan,
        purchased_plan=plan.name,
        amount=payment.amount,
        currency=payment.currency,
        payment_reference_suffix=_reference_suffix(payment),
        current_expiration=service.expiration_date,
        proposed_expiration=proposed,
        activation_status=payment.activation_status,
        resolution_status=payment.resolution_status,
        duplicate_count=len(competitors),
        duplicate_warning=len(competitors) > 1,
        eligible=eligible,
        eligibility_reason=reason,
        activated_at=payment.activated_at,
        activation_correlation_id=payment.activation_correlation_id,
    )


def list_plan_activations(db: Session, principal: AuthenticatedPrincipal) -> list[PlanActivationSummary]:
    payments = db.query(PaymentTransaction).filter(
        PaymentTransaction.organization_id == principal.organization_id,
        PaymentTransaction.activation_status.in_(("pending_activation", "blocked_duplicate", "activated")),
    ).order_by(PaymentTransaction.created_at.asc()).all()
    return [_summary(db, payment) for payment in payments]


def get_plan_activation(db: Session, principal: AuthenticatedPrincipal, payment_id: int) -> PlanActivationSummary:
    payment = db.query(PaymentTransaction).filter(
        PaymentTransaction.id == payment_id,
        PaymentTransaction.organization_id == principal.organization_id,
    ).first()
    if not payment or payment.activation_status == "not_applicable":
        raise HTTPException(status_code=404, detail="Plan activation not found")
    return _summary(db, payment)


def resolve_duplicate_plan_payments(
    db: Session,
    principal: AuthenticatedPrincipal,
    *,
    payment_id: int,
    canonical_payment_id: int,
    correlation_id: str,
    reason: str,
) -> tuple[PaymentTransaction, list[int]]:
    selected = db.query(PaymentTransaction).filter(
        PaymentTransaction.id == payment_id,
        PaymentTransaction.organization_id == principal.organization_id,
    ).with_for_update().first()
    if not selected:
        raise HTTPException(status_code=404, detail="Plan activation not found")
    competitors = _competitors(db, selected, lock=True)
    if len(competitors) < 2 or canonical_payment_id not in {item.id for item in competitors}:
        raise HTTPException(status_code=409, detail="Duplicate payment resolution is not applicable")
    canonical = next(item for item in competitors if item.id == canonical_payment_id)
    existing = [item for item in competitors if item.resolution_status == "canonical"]
    if existing:
        if existing[0].id == canonical.id and all(item.canonical_payment_id == canonical.id for item in competitors):
            return canonical, [item.id for item in competitors if item.id != canonical.id]
        raise HTTPException(status_code=409, detail="A different canonical payment was already selected")
    now = _now()
    staff_id = _staff_id(principal)
    for item in competitors:
        item.canonical_payment_id = canonical.id
        item.resolved_at = now
        item.resolved_by_staff_id = staff_id
        item.resolution_reason = reason
        if item.id == canonical.id:
            item.resolution_status = "canonical"
            item.activation_status = "pending_activation"
        else:
            item.resolution_status = "duplicate"
            item.activation_status = "blocked_duplicate"
        record_audit(
            db,
            organization_id=item.organization_id,
            actor_type="organization_staff",
            actor_id=str(staff_id),
            actor_label=principal.actor_label,
            actor=principal.actor_label,
            action="plan_activation.canonical_selected" if item.id == canonical.id else "plan_activation.duplicate_recorded",
            target_type="payment",
            target_id=str(item.id),
            new_value={
                "payment_id": item.id,
                "customer_id": item.customer_id,
                "service_id": item.user_id,
                "canonical_payment_id": canonical.id,
                "correlation_id": correlation_id,
                "result": item.resolution_status,
            },
        )
    return canonical, [item.id for item in competitors if item.id != canonical.id]


def activate_plan_change(
    db: Session,
    principal: AuthenticatedPrincipal,
    *,
    payment_id: int,
    correlation_id: str,
) -> PlanActivationSummary:
    payment = db.query(PaymentTransaction).filter(
        PaymentTransaction.id == payment_id,
        PaymentTransaction.organization_id == principal.organization_id,
    ).with_for_update().first()
    if not payment:
        raise HTTPException(status_code=404, detail="Plan activation not found")
    record_audit(
        db,
        organization_id=payment.organization_id,
        actor_type="organization_staff",
        actor_id=str(_staff_id(principal)),
        actor_label=principal.actor_label,
        actor=principal.actor_label,
        action="plan_activation.attempted",
        target_type="payment",
        target_id=str(payment.id),
        new_value={
            "payment_id": payment.id,
            "customer_id": payment.customer_id,
            "service_id": payment.user_id,
            "correlation_id": correlation_id,
        },
    )
    if payment.activation_status == "activated":
        if payment.activation_correlation_id == correlation_id:
            record_audit(db, organization_id=payment.organization_id, actor_type="organization_staff", actor_id=str(_staff_id(principal)), actor_label=principal.actor_label, actor=principal.actor_label, action="plan_activation.idempotent_replay", target_type="payment", target_id=str(payment.id), new_value={"payment_id": payment.id, "customer_id": payment.customer_id, "service_id": payment.user_id, "correlation_id": correlation_id})
            return _summary(db, payment)
        _raise_activation_conflict(
            db, payment, principal, correlation_id,
            "Plan activation was already completed with another correlation ID",
        )

    service = db.query(User).filter(
        User.id == payment.user_id,
        User.organization_id == payment.organization_id,
        User.customer_id == payment.customer_id,
    ).with_for_update().first()
    if not service:
        _raise_activation_conflict(db, payment, principal, correlation_id, "Activation service not found")
    competitors = _competitors(db, payment, lock=True)
    eligible, reason = _eligibility(payment, service, competitors)
    if not eligible:
        record_audit(db, organization_id=payment.organization_id, actor_type="organization_staff", actor_id=str(_staff_id(principal)), actor_label=principal.actor_label, actor=principal.actor_label, action="plan_activation.refused", target_type="payment", target_id=str(payment.id), success=False, new_value={"payment_id": payment.id, "customer_id": payment.customer_id, "service_id": service.id, "correlation_id": correlation_id, "reason": reason})
        raise HTTPException(status_code=409, detail=reason)
    plan = db.query(ServicePlan).filter(
        ServicePlan.id == payment.selected_plan_id,
        ServicePlan.organization_id == payment.organization_id,
        ServicePlan.status == "active",
    ).first()
    if not plan or payment.previous_plan_name != service.service_plan:
        _raise_activation_conflict(
            db, payment, principal, correlation_id,
            "Service or purchased plan has changed since payment verification",
        )
    if Decimal(payment.expected_amount or payment.amount) != Decimal(payment.amount) or (payment.expected_currency or payment.currency) != payment.currency:
        _raise_activation_conflict(db, payment, principal, correlation_id, "Payment authority evidence does not match")

    now = _now()
    new_expiration = _proposed_expiration(payment, service, now)
    old_plan = service.service_plan
    old_expiration = service.expiration_date
    service.service_plan = plan.name
    service.expiration_date = new_expiration
    payment.activation_status = "activated"
    payment.fulfillment_status = "completed"
    payment.activated_at = now
    payment.activated_by_staff_id = _staff_id(principal)
    payment.activation_correlation_id = correlation_id
    payment.new_expiration_date = new_expiration
    payment.canonical_payment_id = payment.id
    payment.resolution_status = "canonical"
    payment.resolved_at = payment.resolved_at or now
    payment.resolved_by_staff_id = payment.resolved_by_staff_id or _staff_id(principal)
    record_audit(
        db,
        organization_id=payment.organization_id,
        actor_type="organization_staff",
        actor_id=str(_staff_id(principal)),
        actor_label=principal.actor_label,
        actor=principal.actor_label,
        action="plan_activation.completed",
        target_type="payment",
        target_id=str(payment.id),
        old_value={"payment_id": payment.id, "customer_id": payment.customer_id, "service_id": service.id, "plan": old_plan, "expiration_date": old_expiration.isoformat() if old_expiration else None},
        new_value={"payment_id": payment.id, "customer_id": payment.customer_id, "service_id": service.id, "plan": plan.name, "expiration_date": new_expiration.isoformat(), "correlation_id": correlation_id, "result": "activated"},
    )
    db.flush()
    return _summary(db, payment, at=now)
