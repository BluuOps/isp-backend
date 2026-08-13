from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from sqlalchemy import and_, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.principal import bearer_payload
from app.core.tenant_host import resolve_tenant_from_request
from app.database import get_db
from app.models import (
    AuditLog,
    BillingAccount,
    Customer,
    CustomerPortalAccount,
    Organization,
    PaymentTransaction,
    RadAcct,
    ServicePlan,
    SupportTicket,
    TicketMessage,
    User,
)
from app.schemas.customer_portal import (
    CustomerPortalDashboard,
    CustomerPortalOrganizationBranding,
    CustomerPortalPaymentDetail,
    CustomerPortalPaymentSummary,
    CustomerPortalProfile,
    CustomerPortalProfileUpdate,
    CustomerPortalServiceSummary,
    CustomerPortalSubscription,
    CustomerPortalTicketCreate,
    CustomerPortalTicketMessageResponse,
    CustomerPortalTicketResponse,
)
from app.schemas.payment import (
    CustomerPaymentInitializeRequest,
    CustomerPaymentInitializeResponse,
    CustomerPaymentQuoteRequest,
    CustomerPaymentQuoteResponse,
    CustomerPaymentStatusResponse,
    CustomerPaymentVerifyRequest,
    CustomerPaymentVerifyResponse,
    CustomerServicePlanResponse,
)
from app.services.audit import record_audit
from app.integrations.base import GatewayVerifyResult, PaymentGatewayError
from app.integrations.paystack import PaystackGateway
from app.services.payment_service import initialize_customer_renewal, process_verified_payment
from app.services.payment_quote import create_quote, read_quote
from app.services.radius_session_freshness import fresh_active_session_conditions


router = APIRouter(prefix="/customer-portal", tags=["Customer Portal"])
CATALOG_CUSTOMER_STATUSES = {"active", "expired", "suspended"}
PURCHASING_CUSTOMER_STATUSES = {"active", "expired", "suspended"}


@dataclass(frozen=True)
class CustomerPortalContext:
    account: CustomerPortalAccount
    customer: Customer
    organization: Organization


def _safe_actor_label(context: CustomerPortalContext) -> str:
    return context.customer.name or context.account.email


def _record_customer_audit(
    db: Session,
    context: CustomerPortalContext,
    *,
    action: str,
    target_type: str,
    target_id: str | None,
    old_value: dict | None = None,
    new_value: dict | None = None,
    success: bool = True,
) -> None:
    record_audit(
        db,
        organization_id=context.organization.id,
        actor_type="customer",
        actor_id=str(context.account.id),
        actor_label=_safe_actor_label(context),
        actor=context.account.email,
        action=action,
        target_type=target_type,
        target_id=target_id,
        old_value=old_value,
        new_value=new_value,
        success=success,
    )


def get_customer_portal_context(
    request: Request,
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> CustomerPortalContext:
    payload = bearer_payload(authorization)
    if not payload:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing customer token")
    if payload.get("principal_type") != "customer":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Customer portal access requires a customer token")

    try:
        account_id = int(payload["sub"])
        organization_id = int(payload["organization_id"])
        customer_id = str(payload["customer_id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid customer token") from exc

    account = (
        db.query(CustomerPortalAccount)
        .filter(
            CustomerPortalAccount.id == account_id,
            CustomerPortalAccount.organization_id == organization_id,
            CustomerPortalAccount.customer_id == customer_id,
        )
        .first()
    )
    if not account or account.status != "active":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid customer token")

    organization = db.query(Organization).filter(Organization.id == organization_id).first()
    customer = (
        db.query(Customer)
        .filter(Customer.id == customer_id, Customer.organization_id == organization_id)
        .first()
    )
    if not organization or not customer:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid customer token")
    tenant_context = resolve_tenant_from_request(request, db)
    if tenant_context.organization.id != organization_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer portal not found")
    return CustomerPortalContext(account=account, customer=customer, organization=organization)


def _branding(organization: Organization) -> CustomerPortalOrganizationBranding:
    return CustomerPortalOrganizationBranding(
        id=organization.id,
        name=organization.name,
        slug=organization.slug,
        logo=organization.logo,
        currency=organization.currency,
        timezone=organization.timezone,
    )


def _mask_username(username: str) -> str:
    if len(username) <= 4:
        return username[0:1] + "***"
    return f"{username[:2]}***{username[-2:]}"


def _select_catalog_service(services: list[User], service_id: int | None) -> User | None:
    if service_id is not None:
        selected_service = next((service for service in services if service.id == service_id), None)
        if not selected_service:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service not found")
        return selected_service
    if len(services) > 1:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Select the linked service before viewing service plans",
        )
    return services[0] if services else None


def _latest_session(db: Session, username: str) -> RadAcct | None:
    return (
        db.query(RadAcct)
        .filter(RadAcct.username == username)
        .order_by(RadAcct.acctstarttime.desc().nullslast())
        .first()
    )


def _online(db: Session, username: str) -> bool:
    return (
        db.query(RadAcct)
        .filter(RadAcct.username == username, *fresh_active_session_conditions())
        .first()
        is not None
    )


def _pending_plan_activation(db: Session, *, organization_id: int, customer_id: str, service_id: int):
    return (
        db.query(PaymentTransaction)
        .filter(
            PaymentTransaction.organization_id == organization_id,
            PaymentTransaction.customer_id == customer_id,
            PaymentTransaction.user_id == service_id,
            PaymentTransaction.payment_status.in_(("successful", "paid")),
            PaymentTransaction.activation_status.in_(("pending_activation", "blocked_duplicate")),
        )
        .order_by(PaymentTransaction.created_at.asc())
        .first()
    )


def _service_response(db: Session, service: User, organization_id: int) -> CustomerPortalServiceSummary:
    plan = (
        db.query(ServicePlan)
        .filter(ServicePlan.organization_id == organization_id, ServicePlan.name == service.service_plan)
        .first()
    )
    session = _latest_session(db, service.username)
    pending_activation = _pending_plan_activation(
        db,
        organization_id=organization_id,
        customer_id=service.customer_id,
        service_id=service.id,
    )
    return CustomerPortalServiceSummary(
        id=service.id,
        username=service.username,
        masked_username=_mask_username(service.username),
        service_plan=service.service_plan,
        rate_limit=plan.rate_limit if plan else None,
        status=service.status,
        expiration_date=service.expiration_date,
        online=_online(db, service.username),
        last_session_started_at=session.acctstarttime if session else None,
        last_session_updated_at=session.acctupdatetime if session else None,
        framed_ip_address=str(session.framedipaddress) if session and session.framedipaddress else None,
        pending_plan_activation=pending_activation is not None,
        pending_activation_payment_id=pending_activation.id if pending_activation else None,
        pending_activation_plan=pending_activation.resulting_plan_name if pending_activation else None,
    )


def _payment_summary(payment: PaymentTransaction) -> CustomerPortalPaymentSummary:
    return CustomerPortalPaymentSummary(
        id=payment.id,
        transaction_reference=payment.transaction_reference,
        amount=payment.amount,
        currency=payment.currency,
        payment_method=payment.payment_method,
        payment_status=payment.payment_status,
        paid_at=payment.paid_at,
        created_at=payment.created_at,
        selected_plan_id=payment.selected_plan_id,
        purchased_plan=payment.resulting_plan_name,
        fulfillment_status=payment.fulfillment_status,
        resulting_expiration_date=payment.new_expiration_date,
    )


def _payment_status_response(payment: PaymentTransaction) -> CustomerPaymentStatusResponse:
    return CustomerPaymentStatusResponse(
        payment_id=payment.id,
        transaction_reference=payment.transaction_reference,
        amount=payment.amount,
        currency=payment.currency,
        status=payment.payment_status,
        gateway=payment.gateway,
        gateway_reference=payment.gateway_reference,
        paid_at=payment.paid_at,
        verified_at=payment.verified_at,
        renewal_processed_at=payment.renewal_processed_at,
        old_expiration_date=payment.old_expiration_date,
        new_expiration_date=payment.new_expiration_date,
    )


def _payment_verify_response(payment: PaymentTransaction) -> CustomerPaymentVerifyResponse:
    metadata = payment.gateway_metadata or {}
    renewal_status = metadata.get("renewal_status")
    if not renewal_status:
        renewal_status = (
            "completed"
            if payment.renewal_processed_at
            else ("pending" if payment.payment_status in {"initiated", "pending"} else payment.payment_status)
        )
    return CustomerPaymentVerifyResponse(
        **_payment_status_response(payment).model_dump(),
        verified=payment.payment_status in {"successful", "paid"},
        renewal_status=renewal_status,
    )


def _ticket_response(db: Session, ticket: SupportTicket, include_messages: bool = False) -> CustomerPortalTicketResponse:
    messages: list[CustomerPortalTicketMessageResponse] = []
    if include_messages:
        rows = (
            db.query(TicketMessage)
            .filter(TicketMessage.ticket_id == ticket.id)
            .order_by(TicketMessage.created_at.asc(), TicketMessage.id.asc())
            .all()
        )
        messages = [CustomerPortalTicketMessageResponse.model_validate(row) for row in rows]
    return CustomerPortalTicketResponse(
        id=ticket.id,
        customer_id=ticket.customer_id,
        user_id=ticket.user_id,
        subject=ticket.subject,
        category=ticket.category,
        priority=ticket.priority,
        status=ticket.status,
        created_at=ticket.created_at,
        updated_at=ticket.updated_at,
        resolved_at=ticket.resolved_at,
        closed_at=ticket.closed_at,
        messages=messages,
    )


def _customer_services(db: Session, context: CustomerPortalContext) -> list[User]:
    return (
        db.query(User)
        .filter(User.organization_id == context.organization.id, User.customer_id == context.customer.id)
        .order_by(User.id.asc())
        .all()
    )


def _customer_plan_response(plan: ServicePlan, current_plan: str | None) -> CustomerServicePlanResponse:
    return CustomerServicePlanResponse(
        id=plan.id,
        name=plan.name,
        description=plan.description,
        rate_limit=plan.rate_limit,
        billing_interval=plan.billing_interval,
        duration_days=plan.duration_days,
        currency=plan.currency,
        price_minor=int(plan.price_minor),
        eligible=True,
        is_current=plan.name == current_plan,
    )


@router.get("/dashboard", response_model=CustomerPortalDashboard)
def dashboard(
    db: Session = Depends(get_db),
    context: CustomerPortalContext = Depends(get_customer_portal_context),
) -> CustomerPortalDashboard:
    services = _customer_services(db, context)
    service_items = [_service_response(db, service, context.organization.id) for service in services]
    latest_service = services[0] if services else None
    recent_payments = (
        db.query(PaymentTransaction)
        .filter(PaymentTransaction.organization_id == context.organization.id, PaymentTransaction.customer_id == context.customer.id)
        .order_by(PaymentTransaction.created_at.desc())
        .limit(5)
        .all()
    )
    recent_tickets = (
        db.query(SupportTicket)
        .filter(SupportTicket.organization_id == context.organization.id, SupportTicket.customer_id == context.customer.id)
        .order_by(SupportTicket.created_at.desc(), SupportTicket.id.desc())
        .limit(5)
        .all()
    )
    open_ticket_count = (
        db.query(SupportTicket)
        .filter(
            SupportTicket.organization_id == context.organization.id,
            SupportTicket.customer_id == context.customer.id,
            SupportTicket.status.in_(["open", "in_progress", "waiting_customer"]),
        )
        .count()
    )
    empty_states = {}
    if not services:
        empty_states["services"] = "No linked PPPoE service accounts are available."
    if not recent_payments:
        empty_states["payments"] = "No payments are available yet."
    if not recent_tickets:
        empty_states["tickets"] = "No support tickets are open."

    return CustomerPortalDashboard(
        customer_id=context.customer.id,
        customer_name=context.customer.name,
        account_status=context.customer.account_status,
        organization=_branding(context.organization),
        current_plan=latest_service.service_plan if latest_service else None,
        expiration_date=latest_service.expiration_date if latest_service else None,
        subscription_status=context.organization.subscription_status,
        service_status=latest_service.status if latest_service else "no_service",
        online=any(item.online for item in service_items),
        services=service_items,
        recent_payments=[_payment_summary(payment) for payment in recent_payments],
        open_ticket_count=open_ticket_count,
        recent_tickets=[_ticket_response(db, ticket) for ticket in recent_tickets],
        renewal_eligible=bool(latest_service and latest_service.status in {"active", "suspended", "pending"}),
        empty_states=empty_states,
    )


@router.get("/profile", response_model=CustomerPortalProfile)
def get_profile(context: CustomerPortalContext = Depends(get_customer_portal_context)) -> CustomerPortalProfile:
    return CustomerPortalProfile(
        customer_id=context.customer.id,
        name=context.customer.name,
        email=context.customer.email,
        phone=context.customer.phone,
        address=context.customer.address,
        customer_type=context.customer.customer_type,
        account_status=context.customer.account_status,
        organization=_branding(context.organization),
    )


@router.put("/profile", response_model=CustomerPortalProfile)
def update_profile(
    payload: CustomerPortalProfileUpdate,
    db: Session = Depends(get_db),
    context: CustomerPortalContext = Depends(get_customer_portal_context),
) -> CustomerPortalProfile:
    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No safe profile fields were provided")

    if "email" in updates:
        duplicate_customer = (
            db.query(Customer)
            .filter(Customer.organization_id == context.organization.id, Customer.email == updates["email"], Customer.id != context.customer.id)
            .first()
        )
        duplicate_account = (
            db.query(CustomerPortalAccount)
            .filter(CustomerPortalAccount.organization_id == context.organization.id, CustomerPortalAccount.email == updates["email"], CustomerPortalAccount.id != context.account.id)
            .first()
        )
        if duplicate_customer or duplicate_account:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email is already in use")

    old = {"email": context.customer.email, "phone": context.customer.phone, "address": context.customer.address}
    if "email" in updates:
        context.customer.email = updates["email"]
        context.account.email = updates["email"].strip().lower()
    if "phone" in updates:
        context.customer.phone = updates["phone"]
        context.account.phone = updates["phone"].strip().lower()
    if "address" in updates:
        context.customer.address = updates["address"]
    _record_customer_audit(
        db,
        context,
        action="customer.profile.updated",
        target_type="customer",
        target_id=context.customer.id,
        old_value=old,
        new_value=updates,
    )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Profile update conflicts with an existing record") from exc
    return get_profile(context)


@router.get("/services", response_model=list[CustomerPortalServiceSummary])
def list_services(
    db: Session = Depends(get_db),
    context: CustomerPortalContext = Depends(get_customer_portal_context),
) -> list[CustomerPortalServiceSummary]:
    return [_service_response(db, service, context.organization.id) for service in _customer_services(db, context)]


@router.get("/services/{service_id}", response_model=CustomerPortalServiceSummary)
def get_service(
    service_id: int,
    db: Session = Depends(get_db),
    context: CustomerPortalContext = Depends(get_customer_portal_context),
) -> CustomerPortalServiceSummary:
    service = (
        db.query(User)
        .filter(User.id == service_id, User.organization_id == context.organization.id, User.customer_id == context.customer.id)
        .first()
    )
    if not service:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service not found")
    return _service_response(db, service, context.organization.id)


@router.get("/subscription", response_model=CustomerPortalSubscription)
def get_subscription(
    db: Session = Depends(get_db),
    context: CustomerPortalContext = Depends(get_customer_portal_context),
) -> CustomerPortalSubscription:
    services = _customer_services(db, context)
    latest_service = services[0] if services else None
    last_payment = (
        db.query(PaymentTransaction)
        .filter(PaymentTransaction.organization_id == context.organization.id, PaymentTransaction.customer_id == context.customer.id)
        .order_by(PaymentTransaction.created_at.desc())
        .first()
    )
    account = None
    if latest_service:
        account = (
            db.query(BillingAccount)
            .filter(BillingAccount.organization_id == context.organization.id, BillingAccount.user_id == latest_service.id)
            .first()
        )
    return CustomerPortalSubscription(
        current_plan=latest_service.service_plan if latest_service else None,
        service_status=latest_service.status if latest_service else None,
        expiration_date=latest_service.expiration_date if latest_service else None,
        renewal_status="eligible" if latest_service and latest_service.status in {"active", "suspended", "pending"} else "not_available",
        renewal_eligible=bool(latest_service and latest_service.status in {"active", "suspended", "pending"}),
        outstanding_balance=Decimal(str(account.balance)) if account and account.balance is not None else Decimal("0"),
        last_payment=_payment_summary(last_payment) if last_payment else None,
    )


@router.get("/payments", response_model=list[CustomerPortalPaymentSummary])
def list_payments(
    status_filter: str | None = Query(default=None, alias="status", max_length=30),
    method: str | None = Query(default=None, max_length=50),
    date_from: datetime | None = Query(default=None),
    date_to: datetime | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    context: CustomerPortalContext = Depends(get_customer_portal_context),
) -> list[CustomerPortalPaymentSummary]:
    query = db.query(PaymentTransaction).filter(
        PaymentTransaction.organization_id == context.organization.id,
        PaymentTransaction.customer_id == context.customer.id,
    )
    if status_filter:
        query = query.filter(PaymentTransaction.payment_status == status_filter.lower())
    if method:
        query = query.filter(PaymentTransaction.payment_method == method.lower())
    if date_from:
        query = query.filter(PaymentTransaction.created_at >= date_from)
    if date_to:
        query = query.filter(PaymentTransaction.created_at <= date_to)
    rows = query.order_by(PaymentTransaction.created_at.desc()).offset(offset).limit(limit).all()
    return [_payment_summary(row) for row in rows]


@router.get("/service-plans", response_model=list[CustomerServicePlanResponse])
def list_customer_service_plans(
    service_id: int | None = Query(default=None, ge=1),
    db: Session = Depends(get_db),
    context: CustomerPortalContext = Depends(get_customer_portal_context),
) -> list[CustomerServicePlanResponse]:
    if (
        context.organization.status != "active"
        or context.customer.account_status not in CATALOG_CUSTOMER_STATUSES
    ):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Customer plan catalogue is unavailable")
    services = _customer_services(db, context)
    selected_service = _select_catalog_service(services, service_id)
    if not selected_service:
        return []
    current_plan = selected_service.service_plan if selected_service else None
    plans = (
        db.query(ServicePlan)
        .filter(
            ServicePlan.organization_id == context.organization.id,
            ServicePlan.status == "active",
            ServicePlan.customer_visible.is_(True),
            ServicePlan.price_minor.isnot(None),
            ServicePlan.price_minor > 0,
        )
        .order_by(ServicePlan.name.asc())
        .all()
    )
    return [_customer_plan_response(plan, current_plan) for plan in plans]


@router.post("/payments/quote", response_model=CustomerPaymentQuoteResponse)
def quote_payment(
    payload: CustomerPaymentQuoteRequest,
    db: Session = Depends(get_db),
    context: CustomerPortalContext = Depends(get_customer_portal_context),
) -> CustomerPaymentQuoteResponse:
    if (
        context.organization.status != "active"
        or context.customer.account_status not in PURCHASING_CUSTOMER_STATUSES
    ):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Customer purchasing is unavailable")
    services = _customer_services(db, context)
    if payload.service_id is None:
        if not services:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="No linked service is available")
        if len(services) > 1:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Select the linked service to renew",
            )
        service = services[0]
    else:
        service = next((item for item in services if item.id == payload.service_id), None)
    if not service:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service not found")
    if service.status in {"terminated", "cancelled", "deleted"}:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Service is not eligible for renewal")
    pending_activation = _pending_plan_activation(
        db,
        organization_id=context.organization.id,
        customer_id=context.customer.id,
        service_id=service.id,
    )
    if pending_activation:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "pending_plan_activation",
                "message": "A verified plan change for this service is awaiting staff activation",
            },
        )
    plan = (
        db.query(ServicePlan)
        .filter(
            ServicePlan.id == payload.plan_id,
            ServicePlan.organization_id == context.organization.id,
            ServicePlan.status == "active",
            ServicePlan.customer_visible.is_(True),
            ServicePlan.price_minor.isnot(None),
            ServicePlan.price_minor > 0,
        )
        .first()
    )
    if not plan:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service plan not found")
    amount_minor = int(plan.price_minor) * payload.billing_periods
    token, quote = create_quote(
        organization_id=context.organization.id,
        customer_id=context.customer.id,
        service_id=service.id,
        plan_id=plan.id,
        billing_periods=payload.billing_periods,
        amount_minor=amount_minor,
        currency=plan.currency,
    )
    return CustomerPaymentQuoteResponse(
        quote_token=token,
        quote_reference=quote.reference,
        expires_at=datetime.fromtimestamp(quote.expires_at, tz=timezone.utc),
        service_id=service.id,
        plan=_customer_plan_response(plan, service.service_plan),
        billing_periods=payload.billing_periods,
        amount_minor=amount_minor,
        amount=Decimal(amount_minor) / Decimal("100"),
        currency=plan.currency,
        current_expiration_date=service.expiration_date,
        fulfillment_policy="renewal" if plan.name == service.service_plan else "pending_activation",
    )


@router.post("/payments/initialize", response_model=CustomerPaymentInitializeResponse)
def initialize_payment(
    payload: CustomerPaymentInitializeRequest,
    db: Session = Depends(get_db),
    context: CustomerPortalContext = Depends(get_customer_portal_context),
) -> CustomerPaymentInitializeResponse:
    if (
        context.organization.status != "active"
        or context.customer.account_status not in PURCHASING_CUSTOMER_STATUSES
    ):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Customer purchasing is unavailable")
    quote = read_quote(payload.quote_token)
    service = (
        db.query(User)
        .filter(
            User.id == quote.service_id,
            User.organization_id == context.organization.id,
            User.customer_id == context.customer.id,
        )
        .first()
    )
    if not service:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service not found")
    if service.status in {"terminated", "cancelled", "deleted"}:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Service is not eligible for renewal")

    if quote.organization_id != context.organization.id or quote.customer_id != context.customer.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Payment quote not found")
    selected_plan = (
        db.query(ServicePlan)
        .filter(
            ServicePlan.id == quote.plan_id,
            ServicePlan.organization_id == context.organization.id,
            ServicePlan.status == "active",
            ServicePlan.customer_visible.is_(True),
            ServicePlan.price_minor.isnot(None),
            ServicePlan.price_minor > 0,
        )
        .first()
    )
    if not selected_plan:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service plan not found")
    if selected_plan.currency != quote.currency:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Payment quote currency is stale")

    try:
        payment = initialize_customer_renewal(
            db,
            account=context.account,
            customer=context.customer,
            service=service,
            renewal_cycles=quote.billing_periods,
            idempotency_key=payload.idempotency_key,
            selected_plan=selected_plan,
            quote_reference=quote.reference,
            quoted_amount_minor=quote.amount_minor,
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(payment)
    return CustomerPaymentInitializeResponse(
        payment_id=payment.id,
        transaction_reference=payment.transaction_reference,
        authorization_url=payment.authorization_url or "",
        access_code=payment.access_code,
        amount=payment.amount,
        currency=payment.currency,
        status=payment.payment_status,
        renewal_cycles=payment.renewal_cycles,
    )


@router.get("/payments/{payment_id}", response_model=CustomerPortalPaymentDetail)
def get_payment(
    payment_id: int,
    db: Session = Depends(get_db),
    context: CustomerPortalContext = Depends(get_customer_portal_context),
) -> CustomerPortalPaymentDetail:
    payment = (
        db.query(PaymentTransaction)
        .filter(
            PaymentTransaction.id == payment_id,
            PaymentTransaction.organization_id == context.organization.id,
            PaymentTransaction.customer_id == context.customer.id,
        )
        .first()
    )
    if not payment:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Payment not found")
    return CustomerPortalPaymentDetail(
        **_payment_summary(payment).model_dump(),
        external_reference=payment.external_reference,
        user_id=payment.user_id,
        notes=payment.notes,
    )


@router.get("/payments/{payment_id}/status", response_model=CustomerPaymentStatusResponse)
def get_payment_status(
    payment_id: int,
    db: Session = Depends(get_db),
    context: CustomerPortalContext = Depends(get_customer_portal_context),
) -> CustomerPaymentStatusResponse:
    payment = (
        db.query(PaymentTransaction)
        .filter(
            PaymentTransaction.id == payment_id,
            PaymentTransaction.organization_id == context.organization.id,
            PaymentTransaction.customer_id == context.customer.id,
        )
        .first()
    )
    if not payment:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Payment not found")
    return _payment_status_response(payment)


@router.post("/payments/{payment_id}/verify", response_model=CustomerPaymentVerifyResponse)
def verify_payment(
    payment_id: int,
    payload: CustomerPaymentVerifyRequest | None = None,
    db: Session = Depends(get_db),
    context: CustomerPortalContext = Depends(get_customer_portal_context),
) -> CustomerPaymentVerifyResponse:
    payment = (
        db.query(PaymentTransaction)
        .filter(
            PaymentTransaction.id == payment_id,
            PaymentTransaction.organization_id == context.organization.id,
            PaymentTransaction.customer_id == context.customer.id,
        )
        .with_for_update()
        .first()
    )
    if not payment:
        _record_customer_audit(
            db,
            context,
            action="payment.verification_denied",
            target_type="payment",
            target_id=str(payment_id),
            success=False,
        )
        db.commit()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Payment not found")
    if payment.gateway != "paystack":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Only Paystack payments can be verified here")
    if payload and payload.reference and payload.reference != payment.transaction_reference:
        _record_customer_audit(
            db,
            context,
            action="payment.verification_failed",
            target_type="payment",
            target_id=payment.transaction_reference,
            success=False,
            new_value={"reason": "reference_mismatch"},
        )
        db.commit()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Payment reference mismatch")

    _record_customer_audit(
        db,
        context,
        action="payment.verification_requested",
        target_type="payment",
        target_id=payment.transaction_reference,
        new_value={"gateway": "paystack"},
    )

    if payment.payment_status in {"successful", "paid"}:
        process_verified_payment(
            db,
            payment=payment,
            verification=GatewayVerifyResult(
                reference=payment.transaction_reference,
                status="success",
                amount=payment.expected_amount or payment.amount,
                currency=payment.expected_currency or payment.currency,
                gateway_reference=payment.gateway_reference,
                raw_status=payment.raw_gateway_status or "success",
                metadata={},
            ),
        )
        db.commit()
        db.refresh(payment)
        return _payment_verify_response(payment)

    gateway = PaystackGateway()
    try:
        verification = gateway.verify_transaction(payment.transaction_reference)
        process_verified_payment(db, payment=payment, verification=verification)
        db.commit()
    except PaymentGatewayError as exc:
        _record_customer_audit(
            db,
            context,
            action="payment.verification_failed",
            target_type="payment",
            target_id=payment.transaction_reference,
            success=False,
            new_value={"code": exc.code},
        )
        db.commit()
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Payment verification failed") from exc
    except HTTPException:
        db.commit()
        raise
    except Exception:
        db.rollback()
        raise

    db.refresh(payment)
    return _payment_verify_response(payment)


@router.get("/tickets", response_model=list[CustomerPortalTicketResponse])
def list_tickets(
    status_filter: str | None = Query(default=None, alias="status", max_length=50),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    context: CustomerPortalContext = Depends(get_customer_portal_context),
) -> list[CustomerPortalTicketResponse]:
    query = db.query(SupportTicket).filter(
        SupportTicket.organization_id == context.organization.id,
        SupportTicket.customer_id == context.customer.id,
    )
    if status_filter:
        query = query.filter(SupportTicket.status == status_filter)
    rows = query.order_by(SupportTicket.created_at.desc(), SupportTicket.id.desc()).offset(offset).limit(limit).all()
    return [_ticket_response(db, row) for row in rows]


@router.post("/tickets", response_model=CustomerPortalTicketResponse, status_code=status.HTTP_201_CREATED)
def create_ticket(
    payload: CustomerPortalTicketCreate,
    db: Session = Depends(get_db),
    context: CustomerPortalContext = Depends(get_customer_portal_context),
) -> CustomerPortalTicketResponse:
    if payload.user_id is not None:
        linked_service = (
            db.query(User)
            .filter(User.id == payload.user_id, User.organization_id == context.organization.id, User.customer_id == context.customer.id)
            .first()
        )
        if not linked_service:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Selected service does not belong to this customer")
    ticket = SupportTicket(
        organization_id=context.organization.id,
        customer_id=context.customer.id,
        user_id=payload.user_id,
        subject=payload.subject,
        category=payload.category,
        priority=payload.priority,
        status="open",
    )
    db.add(ticket)
    db.flush()
    message = TicketMessage(
        ticket_id=ticket.id,
        sender_type="customer",
        sender_id=str(context.account.id),
        body=payload.body,
    )
    db.add(message)
    _record_customer_audit(
        db,
        context,
        action="customer.ticket.created",
        target_type="support_ticket",
        target_id=str(ticket.id),
        new_value={"subject": ticket.subject, "category": ticket.category, "priority": ticket.priority},
    )
    db.commit()
    db.refresh(ticket)
    return _ticket_response(db, ticket, include_messages=True)

@router.get("/tickets/{ticket_id}", response_model=CustomerPortalTicketResponse)
def get_ticket(
    ticket_id: int,
    db: Session = Depends(get_db),
    context: CustomerPortalContext = Depends(get_customer_portal_context),
) -> CustomerPortalTicketResponse:
    ticket = (
        db.query(SupportTicket)
        .filter(
            SupportTicket.id == ticket_id,
            SupportTicket.organization_id == context.organization.id,
            SupportTicket.customer_id == context.customer.id,
        )
        .first()
    )
    if not ticket:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ticket not found")
    return _ticket_response(db, ticket, include_messages=True)
