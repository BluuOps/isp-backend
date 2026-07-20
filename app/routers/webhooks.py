from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.core.config import settings
from app.database import get_db
from app.integrations.base import PaymentGatewayError
from app.integrations.paystack import PaystackGateway
from app.services.audit import record_audit
from app.services.webhook_inbox import (
    create_payment_webhook_event,
    mark_ignored,
    mark_retry_pending,
)
from app.services.payment_webhook_processor import process_payment_webhook_event_by_id
from app.models import PaymentWebhookEvent


router = APIRouter(prefix="/webhooks", tags=["Payment Webhooks"])


@router.post("/payments/paystack")
@router.post("/payments/paystack/{integration_key}")
async def paystack_webhook(
    request: Request,
    integration_key: str | None = None,
    x_paystack_signature: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    if settings.paystack_webhook_route_token and integration_key != settings.paystack_webhook_route_token:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Webhook endpoint not found")
    raw_body = await request.body()
    if len(raw_body) > settings.webhook_max_payload_bytes:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="Webhook payload is too large")

    gateway = PaystackGateway()
    try:
        event = gateway.validate_webhook(raw_body, x_paystack_signature)
    except PaymentGatewayError as exc:
        record_audit(
            db,
            organization_id=None,
            actor="paystack",
            actor_type="system",
            action="webhook.invalid_signature" if exc.code == "invalid_signature" else "webhook.invalid",
            target_type="payment_webhook",
            target_id="paystack",
            success=False,
            new_value={"code": exc.code},
        )
        db.commit()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid webhook") from exc

    inbox_event, duplicate = create_payment_webhook_event(
        db,
        provider="paystack",
        event_type=event.event,
        raw_payload=event.raw,
        raw_body=raw_body,
        request_headers=dict(request.headers),
        signature_valid=True,
        transaction_reference=event.reference,
    )
    record_audit(
        db,
        organization_id=None,
        actor="paystack",
        actor_type="system",
        action="webhook.received",
        target_type="payment_webhook",
        target_id=str(inbox_event.id),
        new_value={"event": event.event, "gateway": "paystack", "reference": event.reference, "duplicate": duplicate},
    )

    if duplicate:
        db.commit()
        return {"status": "ok"}

    if event.event != "charge.success":
        mark_ignored(inbox_event, f"unsupported_event:{event.event}")
        db.commit()
        return {"status": "ignored"}
    if not event.reference:
        mark_ignored(inbox_event, "missing_reference")
        db.commit()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing reference")

    db.commit()

    try:
        process_payment_webhook_event_by_id(db, inbox_event.id)
        db.commit()
    except Exception as exc:
        db.rollback()
        persisted_event = db.get(PaymentWebhookEvent, inbox_event.id)
        if persisted_event is not None:
            mark_retry_pending(persisted_event, exc.__class__.__name__)
            db.commit()

    return {"status": "ok"}
