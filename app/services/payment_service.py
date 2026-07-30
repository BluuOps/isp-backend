from __future__ import annotations

import re
import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from urllib.parse import urlsplit

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.core.config import settings
from app.integrations.base import GatewayVerifyResult, PaymentGatewayError
from app.integrations.paystack import PaystackGateway
from app.models import Customer, CustomerPortalAccount, FeatureFlag, Organization, PaymentTransaction, ServicePlan, User
from app.services.audit import record_audit
from app.services.subscription_renewal import process_subscription_renewal


SUPPORTED_CURRENCY = "NGN"
IDEMPOTENCY_RETRY_WINDOW = timedelta(minutes=5)


@dataclass(frozen=True)
class PaymentProviderConfig:
    provider: str
    enabled: bool
    currency: str
    callback_base_url: str


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _provider_paid_at(value: str | None) -> datetime:
    if not value:
        return _utc_now()
    try:
        return _as_aware_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return _utc_now()


def effective_feature_enabled(db: Session, organization_id: int, key: str) -> bool:
    organization_flag = (
        db.query(FeatureFlag)
        .filter(FeatureFlag.organization_id == organization_id, FeatureFlag.key == key)
        .first()
    )
    if organization_flag is not None:
        return bool(organization_flag.enabled)
    global_flag = (
        db.query(FeatureFlag)
        .filter(FeatureFlag.organization_id.is_(None), FeatureFlag.key == key)
        .first()
    )
    return bool(global_flag.enabled) if global_flag is not None else False


def provider_config_for_organization(db: Session, organization_id: int) -> PaymentProviderConfig:
    enabled = settings.paystack_enabled and settings.payment_gateway == "paystack"
    if enabled:
        settings.require_paystack()
        expected_prefix = {"test": "sk_test_", "live": "sk_live_"}.get(settings.paystack_mode)
        if not expected_prefix or not str(settings.paystack_secret_key or "").startswith(expected_prefix):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Paystack mode does not match the configured credential",
            )
    organization = db.query(Organization).filter(Organization.id == organization_id).first()
    if not organization:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found")
    callback_base_url = _callback_base_url_for_organization(organization.slug)
    return PaymentProviderConfig(
        provider="paystack",
        enabled=enabled and effective_feature_enabled(db, organization_id, "payment_gateway"),
        currency=settings.payment_currency,
        callback_base_url=callback_base_url,
    )


def _callback_base_url_for_organization(organization_slug: str) -> str:
    callback_base_url = settings.paystack_callback_base_url or ""
    for entry in settings.paystack_callback_base_urls:
        if "=" not in entry:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Payment callback configuration is invalid",
            )
        configured_slug, configured_url = (part.strip() for part in entry.split("=", 1))
        if configured_slug.lower() == organization_slug.lower():
            callback_base_url = configured_url
            break
    parsed = urlsplit(callback_base_url)
    hostname = (parsed.hostname or "").lower()
    trusted_hostname = any(
        hostname == domain or hostname.endswith(f".{domain}")
        for domain in settings.tenant_allowed_domains
    )
    if (
        parsed.scheme not in {"http", "https"}
        or not hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or not trusted_hostname
        or (settings.paystack_mode == "live" and parsed.scheme != "https")
    ):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Payment callback configuration is invalid",
        )
    return callback_base_url.rstrip("/")


def parse_plan_price(plan: ServicePlan) -> Decimal:
    if not plan.price:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Selected plan does not have a renewal price")
    normalized_price = re.sub(r"^(ngn|ngâ‚¦|n|â‚¦)\s*", "", str(plan.price).strip(), flags=re.IGNORECASE)
    normalized_price = normalized_price.replace(",", "").replace(" ", "")
    try:
        amount = Decimal(normalized_price)
    except (InvalidOperation, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Selected plan price is invalid") from exc
    if amount <= 0:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Selected plan price must be greater than zero")
    return amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def amount_to_kobo(amount: Decimal) -> int:
    return int((amount * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def generate_reference(organization_id: int) -> str:
    return f"RF-{organization_id}-{int(_utc_now().timestamp())}-{secrets.token_urlsafe(8).replace('-', '').replace('_', '')[:10].upper()}"


def _stored_idempotency_key(
    *,
    organization_id: int,
    customer_id: str,
    service_id: int,
    plan_id: int,
    renewal_cycles: int,
    quote_reference: str | None,
    key: str,
) -> str:
    scope = "|".join(
        (
            str(organization_id),
            customer_id,
            str(service_id),
            str(plan_id),
            str(renewal_cycles),
            quote_reference or "legacy",
            key.strip(),
        )
    )
    return f"renewal:{hashlib.sha256(scope.encode('utf-8')).hexdigest()}"


def _payment_quote_consumed_error() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "error": "payment_quote_consumed",
            "message": "Payment quotation can no longer be used",
        },
    )


def _gateway_initialization_error(category: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail={
            "error": "payment_gateway_initialization_failed",
            "message": "Unable to initialize payment",
            "category": category,
        },
    )


def abandon_stale_pending_payments(
    db: Session,
    *,
    organization_id: int,
    customer_id: str,
    service_id: int,
    now: datetime | None = None,
) -> int:
    current_time = now or _utc_now()
    cutoff = current_time - timedelta(minutes=max(1, int(settings.payment_pending_timeout_minutes)))
    stale_rows = (
        db.query(PaymentTransaction)
        .filter(
            PaymentTransaction.organization_id == organization_id,
            PaymentTransaction.customer_id == customer_id,
            PaymentTransaction.user_id == service_id,
            PaymentTransaction.gateway == "paystack",
            PaymentTransaction.payment_status.in_(("initiated", "pending")),
            PaymentTransaction.created_at < cutoff,
        )
        .all()
    )
    for stale in stale_rows:
        stale.payment_status = "abandoned"
        stale.abandoned_at = current_time
        stale.raw_gateway_status = "abandoned_after_timeout"
        record_audit(
            db,
            organization_id=stale.organization_id,
            actor_type="system",
            actor="payment_lifecycle",
            action="payment.abandoned",
            target_type="payment",
            target_id=stale.transaction_reference,
            new_value={
                "payment_id": stale.id,
                "customer_id": stale.customer_id,
                "service_id": stale.user_id,
                "timeout_minutes": settings.payment_pending_timeout_minutes,
            },
        )
    return len(stale_rows)


def initialize_customer_renewal(
    db: Session,
    *,
    account: CustomerPortalAccount,
    customer: Customer,
    service: User,
    renewal_cycles: int,
    idempotency_key: str | None = None,
    selected_plan: ServicePlan | None = None,
    quote_reference: str | None = None,
    quoted_amount_minor: int | None = None,
) -> PaymentTransaction:
    if service.organization_id != account.organization_id or service.customer_id != customer.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service not found")
    if service.status in {"terminated", "cancelled", "deleted"}:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This service cannot be renewed automatically")

    config = provider_config_for_organization(db, account.organization_id)
    if not config.enabled:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Payment gateway is not enabled for this organization")
    if config.currency != SUPPORTED_CURRENCY:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Unsupported payment currency")

    plan = selected_plan or (
        db.query(ServicePlan)
        .filter(ServicePlan.organization_id == account.organization_id, ServicePlan.name == service.service_plan)
        .first()
    )
    if not plan:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Current service plan was not found")
    if plan.organization_id != account.organization_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service plan not found")
    if selected_plan and (plan.status != "active" or not plan.customer_visible or not plan.price_minor):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Service plan is not available for purchase")
    if str(plan.currency or "").upper() != config.currency:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Service plan currency is not supported")
    if selected_plan:
        authoritative_minor = int(plan.price_minor) * int(renewal_cycles)
        if quoted_amount_minor != authoritative_minor:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Payment quote amount is stale")
        amount = (Decimal(authoritative_minor) / Decimal("100")).quantize(Decimal("0.01"))
    else:
        authoritative_minor = amount_to_kobo(parse_plan_price(plan) * Decimal(renewal_cycles))
        amount = (Decimal(authoritative_minor) / Decimal("100")).quantize(Decimal("0.01"))
    reference = generate_reference(account.organization_id)
    now = _utc_now()
    abandon_stale_pending_payments(
        db,
        organization_id=account.organization_id,
        customer_id=customer.id,
        service_id=service.id,
        now=now,
    )
    normalized_idempotency_key = (idempotency_key or "").strip()
    stored_idempotency_key = (
        _stored_idempotency_key(
            organization_id=account.organization_id,
            customer_id=customer.id,
            service_id=service.id,
            plan_id=plan.id,
            renewal_cycles=renewal_cycles,
            quote_reference=quote_reference,
            key=normalized_idempotency_key,
        )
        if normalized_idempotency_key
        else _stored_idempotency_key(
            organization_id=account.organization_id,
            customer_id=customer.id,
            service_id=service.id,
            plan_id=plan.id,
            renewal_cycles=renewal_cycles,
            quote_reference=quote_reference,
            key=reference,
        )
    )
    if normalized_idempotency_key:
        existing = (
            db.query(PaymentTransaction)
            .filter(
                PaymentTransaction.organization_id == account.organization_id,
                PaymentTransaction.idempotency_key == stored_idempotency_key,
            )
            .first()
        )
        if (
            existing
            and existing.payment_status in {"initiated", "pending"}
            and existing.authorization_url
            and existing.created_at
            and _as_aware_utc(existing.created_at) >= now - IDEMPOTENCY_RETRY_WINDOW
        ):
            return existing
        if existing:
            stored_idempotency_key = _stored_idempotency_key(
                organization_id=account.organization_id,
                customer_id=customer.id,
                service_id=service.id,
                plan_id=plan.id,
                renewal_cycles=renewal_cycles,
                quote_reference=quote_reference,
                key=f"{normalized_idempotency_key}:{int(now.timestamp())}",
            )

    if quote_reference:
        existing_quote_payment = (
            db.query(PaymentTransaction)
            .filter(PaymentTransaction.quote_reference == quote_reference)
            .first()
        )
        if existing_quote_payment:
            if existing_quote_payment.payment_status in {"initiated", "pending"} and existing_quote_payment.authorization_url:
                return existing_quote_payment
            raise _payment_quote_consumed_error()

    payment = PaymentTransaction(
        organization_id=account.organization_id,
        customer_id=customer.id,
        user_id=service.id,
        transaction_reference=reference,
        amount=amount,
        expected_amount=amount,
        currency=config.currency,
        expected_currency=config.currency,
        payment_method="paystack",
        payment_status="initiated",
        payment_purpose="subscription_renewal",
        gateway="paystack",
        idempotency_key=stored_idempotency_key,
        initiated_at=now,
        renewal_cycles=renewal_cycles,
        billing_periods=renewal_cycles,
        selected_plan_id=plan.id,
        quote_reference=quote_reference,
        fulfillment_status="pending_payment",
        previous_plan_name=service.service_plan,
        resulting_plan_name=plan.name,
        created_by_customer_id=customer.id,
        created_by_principal_type="customer",
        recorded_by_label=account.email,
        created_by=account.email,
        notes="Customer portal subscription renewal",
    )
    db.add(payment)
    db.flush()
    # Persist the internal intent before contacting Paystack. If the provider
    # succeeds but the process exits before the response is saved, the
    # transaction remains reconcilable by its RadiusFiber reference.
    db.commit()
    db.refresh(payment)
    callback_url = f"{config.callback_base_url.rstrip('/')}/customer/payments/return?payment_id={payment.id}&reference={reference}"
    gateway = PaystackGateway()

    try:
        result = gateway.initialize_transaction(
            email=account.email,
            amount_kobo=amount_to_kobo(amount),
            reference=reference,
            callback_url=callback_url,
            metadata={
                "payment_id": payment.id,
                "organization_id": account.organization_id,
                "customer_id": customer.id,
                "service_id": service.id,
                "purpose": "subscription_renewal",
                "plan_id": plan.id,
                "plan_name": plan.name,
                "billing_periods": renewal_cycles,
                "amount_minor": authoritative_minor,
                "currency": config.currency,
                "quote_reference": quote_reference,
            },
        )
    except PaymentGatewayError as exc:
        payment.payment_status = "failed"
        payment.failed_at = _utc_now()
        payment.raw_gateway_status = exc.code
        record_audit(
            db,
            organization_id=account.organization_id,
            actor_type="customer",
            actor_id=str(account.id),
            actor_label=account.email,
            actor=account.email,
            action="payment.initialize_failed",
            target_type="payment",
            target_id=reference,
            success=False,
            new_value={"gateway": "paystack", "code": exc.code},
        )
        db.commit()
        raise _gateway_initialization_error(exc.code) from exc

    payment.authorization_url = result.authorization_url
    payment.access_code = result.access_code
    payment.gateway_reference = result.gateway_reference
    payment.raw_gateway_status = result.raw_status
    payment.gateway_metadata = result.metadata
    payment.payment_status = "pending"
    record_audit(
        db,
        organization_id=account.organization_id,
        actor_type="customer",
        actor_id=str(account.id),
        actor_label=account.email,
        actor=account.email,
        action="payment.initialized",
        target_type="payment",
        target_id=reference,
        new_value={
            "amount": str(amount),
            "currency": config.currency,
            "gateway": "paystack",
            "service_id": service.id,
            "plan_id": plan.id,
            "billing_periods": renewal_cycles,
        },
    )
    return payment


def process_verified_payment(
    db: Session,
    *,
    payment: PaymentTransaction,
    verification: GatewayVerifyResult,
) -> PaymentTransaction:
    if payment.payment_status in {"successful", "paid"}:
        if (
            payment.payment_purpose == "subscription_renewal"
            and payment.user_id
            and not payment.renewal_processed_at
        ):
            service = (
                db.query(User)
                .filter(
                    User.id == payment.user_id,
                    User.organization_id == payment.organization_id,
                    User.customer_id == payment.customer_id,
                )
                .with_for_update()
                .first()
            )
            if not service:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Renewal service not found")
            process_subscription_renewal(db, payment=payment, service=service)
        record_audit(
            db,
            organization_id=payment.organization_id,
            actor_type=payment.created_by_principal_type or "customer",
            actor_id=str(payment.created_by_customer_id or payment.customer_id),
            actor_label=payment.created_by,
            actor=payment.created_by,
            action="payment.duplicate_completion_ignored",
            target_type="payment",
            target_id=payment.transaction_reference,
            new_value={"status": payment.payment_status, "renewal_processed_at": payment.renewal_processed_at.isoformat() if payment.renewal_processed_at else None},
        )
        return payment
    if verification.reference != payment.transaction_reference:
        _reject_payment(db, payment, "renewal.rejected", "reference_mismatch")
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Payment reference mismatch")
    if verification.status in {"pending", "ongoing", "processing"}:
        payment.raw_gateway_status = verification.raw_status or verification.status
        payment.gateway_metadata = {**(payment.gateway_metadata or {}), "verification": verification.metadata or {}}
        record_audit(
            db,
            organization_id=payment.organization_id,
            actor_type=payment.created_by_principal_type or "customer",
            actor_id=str(payment.created_by_customer_id or payment.customer_id),
            actor_label=payment.created_by,
            actor=payment.created_by,
            action="payment.verification_pending",
            target_type="payment",
            target_id=payment.transaction_reference,
            new_value={"gateway_status": verification.status},
        )
        return payment
    if verification.status != "success":
        payment.payment_status = "failed"
        payment.failed_at = _utc_now()
        payment.raw_gateway_status = verification.raw_status or verification.status
        record_audit(
            db,
            organization_id=payment.organization_id,
            action="payment.failed",
            target_type="payment",
            target_id=payment.transaction_reference,
            success=False,
            new_value={"gateway_status": verification.status},
        )
        return payment
    if amount_to_kobo(Decimal(verification.amount)) != amount_to_kobo(Decimal(payment.expected_amount or payment.amount)):
        _reject_payment(db, payment, "renewal.rejected", "amount_mismatch")
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Payment amount mismatch")
    if verification.currency != (payment.expected_currency or payment.currency):
        _reject_payment(db, payment, "renewal.rejected", "currency_mismatch")
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Payment currency mismatch")
    provider_metadata = (verification.metadata or {}).get("transaction_metadata") or {}
    expected_metadata = {
        "payment_id": payment.id,
        "organization_id": payment.organization_id,
        "customer_id": payment.customer_id,
        "service_id": payment.user_id,
        "plan_id": payment.selected_plan_id,
        "billing_periods": payment.billing_periods,
        "amount_minor": amount_to_kobo(Decimal(payment.expected_amount or payment.amount)),
        "currency": payment.expected_currency or payment.currency,
    }
    for key, expected in expected_metadata.items():
        if expected is not None and str(provider_metadata.get(key)) != str(expected):
            _reject_payment(db, payment, "renewal.rejected", f"{key}_mismatch")
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Payment metadata mismatch")

    payment.payment_status = "successful"
    payment.paid_at = payment.paid_at or _provider_paid_at(verification.paid_at)
    payment.verified_at = _utc_now()
    payment.gateway_reference = payment.gateway_reference or verification.gateway_reference
    payment.raw_gateway_status = verification.raw_status or verification.status
    payment.gateway_metadata = {**(payment.gateway_metadata or {}), "verification": verification.metadata or {}}

    if payment.payment_purpose == "subscription_renewal" and payment.user_id:
        service = (
            db.query(User)
            .filter(User.id == payment.user_id, User.organization_id == payment.organization_id, User.customer_id == payment.customer_id)
            .with_for_update()
            .first()
        )
        if not service:
            _reject_payment(db, payment, "renewal.rejected", "service_not_found")
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Renewal service not found")
        try:
            process_subscription_renewal(db, payment=payment, service=service)
        except HTTPException as exc:
            payment.gateway_metadata = {
                **(payment.gateway_metadata or {}),
                "renewal_status": "needs_staff_attention",
                "renewal_rejection_reason": exc.detail,
            }
            record_audit(
                db,
                organization_id=payment.organization_id,
                actor_type=payment.created_by_principal_type or "customer",
                actor_id=str(payment.created_by_customer_id or payment.customer_id),
                actor_label=payment.created_by,
                actor=payment.created_by,
                action="renewal.rejected",
                target_type="payment",
                target_id=payment.transaction_reference,
                success=False,
                new_value={
                    "payment_id": payment.id,
                    "transaction_reference": payment.transaction_reference,
                    "customer_id": payment.customer_id,
                    "organization_id": payment.organization_id,
                    "service_id": service.id,
                    "renewal_status": "needs_staff_attention",
                    "payment_status": payment.payment_status,
                    "reason": exc.detail,
                },
            )

    record_audit(
        db,
        organization_id=payment.organization_id,
        actor_type=payment.created_by_principal_type or "customer",
        actor_id=str(payment.created_by_customer_id or payment.customer_id),
        actor_label=payment.created_by,
        actor=payment.created_by,
        action="payment.verified",
        target_type="payment",
        target_id=payment.transaction_reference,
        new_value={"gateway": "paystack", "status": payment.payment_status},
    )
    return payment


def _reject_payment(db: Session, payment: PaymentTransaction, action: str, reason: str) -> None:
    payment.raw_gateway_status = reason
    record_audit(
        db,
        organization_id=payment.organization_id,
        action=action,
        target_type="payment",
        target_id=payment.transaction_reference,
        success=False,
        new_value={"reason": reason},
    )
