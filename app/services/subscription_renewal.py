from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.models import PaymentTransaction, User
from app.services.audit import record_audit


RENEWABLE_STATUSES = {"active", "pending", "expired", "suspended"}
BLOCKED_STATUSES = {"terminated", "cancelled", "deleted"}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


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
    base = old_expiration if old_expiration and old_expiration > now else now
    cycles = int(payment.renewal_cycles or 1)
    new_expiration = base + _month_delta(cycles)

    payment.old_expiration_date = old_expiration
    payment.new_expiration_date = new_expiration
    payment.renewal_processed_at = now
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


def _month_delta(cycles: int):
    # First MVP uses 30-day billing cycles; future plan metadata can replace this.
    from datetime import timedelta

    return timedelta(days=30 * max(1, cycles))
