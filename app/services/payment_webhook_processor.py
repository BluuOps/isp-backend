from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.integrations.base import PaymentGatewayError
from app.integrations.paystack import PaystackGateway
from app.models import PaymentTransaction
from app.services.audit import record_audit
from app.services.payment_service import process_verified_payment
from app.services.webhook_inbox import (
    claim_event_for_processing,
    mark_failed_or_dead_lettered,
    mark_ignored,
    mark_processed,
    mark_retry_pending,
)


RETRYABLE_GATEWAY_CODES = {"gateway_network_error", "gateway_http_error", "gateway_verify_failed"}


def process_payment_webhook_event_by_id(db: Session, event_id: int) -> str:
    event = claim_event_for_processing(db, event_id)
    if event is None:
        return "ignored"

    if event.provider != "paystack":
        mark_ignored(event, "unsupported_provider")
        return "ignored"
    if event.event_type != "charge.success":
        mark_ignored(event, f"unsupported_event:{event.event_type}")
        return "ignored"
    if not event.transaction_reference:
        mark_failed_or_dead_lettered(event, "missing_transaction_reference")
        return "failed"
    payment = (
        db.query(PaymentTransaction)
        .filter(
            PaymentTransaction.gateway == "paystack",
            PaymentTransaction.transaction_reference == event.transaction_reference,
        )
        .with_for_update()
        .first()
    )
    if not payment:
        mark_failed_or_dead_lettered(event, "unknown_payment_reference")
        record_audit(
            db,
            organization_id=None,
            actor="paystack",
            actor_type="system",
            action="payment.unknown_reference",
            target_type="payment_webhook_event",
            target_id=str(event.id),
            success=False,
            new_value={"reference": event.transaction_reference},
        )
        return "failed"

    event.payment_id = payment.id
    event.organization_id = payment.organization_id

    if payment.payment_status in {"successful", "paid"} and payment.renewal_processed_at:
        mark_processed(event)
        return "processed"

    gateway = PaystackGateway()
    try:
        verification = gateway.verify_transaction(event.transaction_reference)
        process_verified_payment(db, payment=payment, verification=verification)
        mark_processed(event)
        record_audit(
            db,
            organization_id=payment.organization_id,
            actor="paystack",
            actor_type="system",
            action="payment.webhook_processed",
            target_type="payment_webhook_event",
            target_id=str(event.id),
            new_value={"payment_id": payment.id, "reference": payment.transaction_reference},
        )
        return "processed"
    except PaymentGatewayError as exc:
        if exc.code in RETRYABLE_GATEWAY_CODES:
            mark_retry_pending(event, exc.code)
            return "retry_pending"
        mark_failed_or_dead_lettered(event, exc.code)
        return "failed"
    except HTTPException as exc:
        mark_failed_or_dead_lettered(event, str(exc.detail))
        return "failed"
