from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.models import PaymentTransaction, ServicePlan, User
from app.services.audit import record_audit
from app.services.plan_activation_identity import logical_plan_purchase_key


RENEWABLE_STATUSES = {"active", "pending", "expired", "suspended"}
BLOCKED_STATUSES = {"terminated", "cancelled", "deleted"}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def process_subscription_renewal(
    db: Session,
    *,
    payment: PaymentTransaction,
    service: User,
) -> None:
    if payment.renewal_processed_at:
        return
    if payment.payment_status not in {"successful", "paid"}:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Payment is not verified successful")
    if payment.payment_purpose != "subscription_renewal":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Payment is not a subscription renewal")
    if service.organization_id != payment.organization_id or service.customer_id != payment.customer_id:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Payment does not match service")
    if service.status in BLOCKED_STATUSES:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Subscriber status cannot be renewed automatically")
    if service.status not in RENEWABLE_STATUSES:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Subscriber status is not renewable")

    old_expiration = service.expiration_date
    old_status = service.status
    now = _utc_now()
    selected_plan = None
    if payment.selected_plan_id:
        selected_plan = (
            db.query(ServicePlan)
            .filter(
                ServicePlan.id == payment.selected_plan_id,
                ServicePlan.organization_id == payment.organization_id,
            )
            .first()
        )
        if not selected_plan:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Purchased plan is unavailable")

    purchased_plan_name = selected_plan.name if selected_plan else service.service_plan
    payment.previous_plan_name = payment.previous_plan_name or service.service_plan
    payment.resulting_plan_name = payment.resulting_plan_name or purchased_plan_name

    if purchased_plan_name != service.service_plan:
        periods = int(payment.billing_periods or payment.renewal_cycles or 1)
        payment.purchased_duration_days = int(selected_plan.duration_days)
        payment.activation_period_key = logical_plan_purchase_key(
            organization_id=payment.organization_id,
            customer_id=payment.customer_id,
            service_id=service.id,
            source_plan_name=service.service_plan,
            selected_plan_id=selected_plan.id,
            source_expiration=old_expiration,
            billing_periods=periods,
        )
        payment.activation_status = "pending_activation"
        payment.resolution_status = "unresolved"
        payment.old_expiration_date = old_expiration
        payment.new_expiration_date = old_expiration
        payment.renewal_processed_at = now
        payment.fulfillment_status = "pending_activation"
        payment.gateway_metadata = {
            **(payment.gateway_metadata or {}),
            "renewal_status": "pending_activation",
            "activation_reason": "plan_change_requires_staff_activation",
        }
        record_audit(
            db,
            organization_id=payment.organization_id,
            actor_type=payment.created_by_principal_type or "customer",
            actor_id=str(payment.created_by_customer_id or payment.customer_id),
            actor_label=payment.created_by or payment.customer_id,
            actor=payment.created_by or payment.customer_id,
            action="plan_change.pending_activation",
            target_type="payment",
            target_id=payment.transaction_reference,
            old_value={"plan": service.service_plan, "expiration_date": old_expiration.isoformat() if old_expiration else None},
            new_value={"plan": purchased_plan_name, "fulfillment_status": "pending_activation"},
        )
        return

    verified_at = _as_aware_utc(payment.paid_at) if payment.paid_at else now
    normalized_old_expiration = _as_aware_utc(old_expiration) if old_expiration else None
    base = (
        normalized_old_expiration
        if normalized_old_expiration and normalized_old_expiration > verified_at
        else verified_at
    )
    periods = int(payment.billing_periods or payment.renewal_cycles or 1)
    duration_days = int(selected_plan.duration_days if selected_plan else 30)
    new_expiration = base + _duration_delta(duration_days, periods)

    payment.old_expiration_date = old_expiration
    payment.new_expiration_date = new_expiration
    payment.renewal_processed_at = now
    payment.fulfillment_status = "completed"
    service.expiration_date = new_expiration
    if service.status in {"expired", "pending"}:
        service.status = "active"

    record_audit(
        db,
        organization_id=payment.organization_id,
        actor_type=payment.created_by_principal_type or "customer",
        actor_id=str(payment.created_by_customer_id or payment.customer_id),
        actor_label=payment.created_by or payment.customer_id,
        actor=payment.created_by or payment.customer_id,
        action="renewal.completed",
        target_type="payment",
        target_id=payment.transaction_reference,
        old_value={"expiration_date": old_expiration.isoformat() if old_expiration else None, "status": old_status},
        new_value={
            "payment_id": payment.id,
            "transaction_reference": payment.transaction_reference,
            "customer_id": payment.customer_id,
            "organization_id": payment.organization_id,
            "service_id": service.id,
            "renewal_status": "completed",
            "payment_status": payment.payment_status,
            "expiration_date": new_expiration.isoformat(),
        },
    )


def _duration_delta(duration_days: int, periods: int):
    from datetime import timedelta

    return timedelta(days=max(1, duration_days) * max(1, periods))
