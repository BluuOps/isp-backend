from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import PaymentWebhookEvent


RETRYABLE_STATUS = "retry_pending"
FINAL_STATUSES = {"processed", "ignored", "dead_lettered"}
SAFE_HEADER_NAMES = {
    "content-type",
    "user-agent",
    "x-forwarded-proto",
}


class WebhookInboxDuplicate(Exception):
    def __init__(self, event: PaymentWebhookEvent) -> None:
        self.event = event


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def payload_hash(raw_body: bytes) -> str:
    return hashlib.sha256(raw_body).hexdigest()


def sanitize_headers(headers: dict[str, str]) -> dict[str, str]:
    return {
        key.lower(): value
        for key, value in headers.items()
        if key.lower() in SAFE_HEADER_NAMES
    }


def extract_gateway_reference(payload: dict[str, Any]) -> str | None:
    data = payload.get("data") or {}
    gateway_id = data.get("id")
    return str(gateway_id) if gateway_id is not None else None


def extract_provider_event_id(payload: dict[str, Any]) -> str | None:
    data = payload.get("data") or {}
    for key in ("event_id", "id"):
        value = payload.get(key) or data.get(key)
        if value is not None:
            return str(value)
    return None


def build_deduplication_key(
    *,
    provider: str,
    event_type: str,
    provider_event_id: str | None,
    transaction_reference: str | None,
    payload_digest: str,
) -> str:
    parts = [
        provider.lower(),
        event_type.lower(),
        provider_event_id or "no-provider-event-id",
        transaction_reference or "no-transaction-reference",
        payload_digest,
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def create_payment_webhook_event(
    db: Session,
    *,
    provider: str,
    event_type: str,
    raw_payload: dict[str, Any],
    raw_body: bytes,
    request_headers: dict[str, str],
    signature_valid: bool,
    transaction_reference: str | None,
) -> tuple[PaymentWebhookEvent, bool]:
    digest = payload_hash(raw_body)
    provider_event_id = extract_provider_event_id(raw_payload)
    deduplication_key = build_deduplication_key(
        provider=provider,
        event_type=event_type,
        provider_event_id=provider_event_id,
        transaction_reference=transaction_reference,
        payload_digest=digest,
    )
    existing = (
        db.query(PaymentWebhookEvent)
        .filter(
            PaymentWebhookEvent.provider == provider,
            PaymentWebhookEvent.deduplication_key == deduplication_key,
        )
        .first()
    )
    if existing:
        return existing, True

    event = PaymentWebhookEvent(
        provider=provider,
        event_type=event_type,
        provider_event_id=provider_event_id,
        transaction_reference=transaction_reference,
        gateway_reference=extract_gateway_reference(raw_payload),
        signature_valid=signature_valid,
        payload_hash=digest,
        deduplication_key=deduplication_key,
        raw_payload=raw_payload,
        request_headers=sanitize_headers(request_headers),
        processing_status="received",
        processing_attempts=0,
    )
    db.add(event)
    db.flush()
    return event, False


def mark_queued(event: PaymentWebhookEvent) -> None:
    event.processing_status = "queued"
    event.last_error = None


def mark_processing(event: PaymentWebhookEvent) -> None:
    event.processing_status = "processing"
    event.processing_attempts = int(event.processing_attempts or 0) + 1
    event.processing_started_at = utc_now()
    event.last_error = None


def mark_processed(event: PaymentWebhookEvent) -> None:
    event.processing_status = "processed"
    event.processed_at = utc_now()
    event.last_error = None


def mark_ignored(event: PaymentWebhookEvent, reason: str) -> None:
    event.processing_status = "ignored"
    event.processed_at = utc_now()
    event.last_error = reason[:1000]


def mark_retry_pending(event: PaymentWebhookEvent, error: str, *, delay_seconds: int = 300) -> None:
    event.processing_status = RETRYABLE_STATUS
    event.next_retry_at = utc_now() + timedelta(seconds=delay_seconds)
    event.last_error = error[:1000]


def mark_failed_or_dead_lettered(event: PaymentWebhookEvent, error: str) -> None:
    if int(event.processing_attempts or 0) >= max(1, settings.webhook_max_processing_attempts):
        event.processing_status = "dead_lettered"
        event.dead_lettered_at = utc_now()
    else:
        event.processing_status = "failed"
    event.last_error = error[:1000]


def claim_event_for_processing(db: Session, event_id: int) -> PaymentWebhookEvent | None:
    event = (
        db.query(PaymentWebhookEvent)
        .filter(PaymentWebhookEvent.id == event_id)
        .with_for_update()
        .first()
    )
    if not event or event.processing_status in FINAL_STATUSES:
        return None
    mark_processing(event)
    return event
