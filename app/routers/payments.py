from __future__ import annotations

import csv
import io
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import conflict
from app.core.principal import reject_customer_principal
from app.core.tenant import OrganizationContext, get_organization_context
from app.database import get_db
from app.models import BillingAccount, Customer, OrganizationStaff, PaymentTransaction, User
from app.schemas import PaymentCreate, PaymentResponse, PaymentSummary
from app.services.audit import record_audit


router = APIRouter(prefix="/payments", tags=["Payments"], dependencies=[Depends(reject_customer_principal)])


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _start_of_day(value: datetime) -> datetime:
    return datetime.combine(value.date(), time.min, tzinfo=timezone.utc)


def _start_of_week(value: datetime) -> datetime:
    day_start = _start_of_day(value)
    return day_start - timedelta(days=day_start.weekday())


def _start_of_month(value: datetime) -> datetime:
    return datetime(value.year, value.month, 1, tzinfo=timezone.utc)


def _start_of_year(value: datetime) -> datetime:
    return datetime(value.year, 1, 1, tzinfo=timezone.utc)


def _paid_total(db: Session, organization_id: int, start_at: datetime) -> Decimal:
    value = (
        db.query(func.coalesce(func.sum(PaymentTransaction.amount), 0))
        .filter(
            PaymentTransaction.organization_id == organization_id,
            PaymentTransaction.payment_status == "paid",
            PaymentTransaction.paid_at >= start_at,
        )
        .scalar()
    )
    return Decimal(value or 0)


def _payment_or_404(payment_id: int, db: Session, organization_id: int) -> PaymentTransaction:
    payment = (
        db.query(PaymentTransaction)
        .filter(PaymentTransaction.id == payment_id, PaymentTransaction.organization_id == organization_id)
        .first()
    )
    if not payment:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Payment transaction not found")
    return payment


def _validate_customer_and_user(payload: PaymentCreate, db: Session, organization_id: int) -> None:
    customer = (
        db.query(Customer)
        .filter(Customer.id == payload.customer_id, Customer.organization_id == organization_id)
        .first()
    )
    if not customer:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Selected CRM customer does not exist")

    if payload.user_id is None:
        return

    user = (
        db.query(User)
        .filter(User.id == payload.user_id, User.organization_id == organization_id)
        .first()
    )
    if not user:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Selected PPPoE user does not exist")
    if user.customer_id and user.customer_id != customer.id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Selected PPPoE user is linked to a different customer")

    if payload.created_by_staff_id and payload.created_by_customer_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Payment cannot have both staff and customer creators")

    if payload.created_by_staff_id:
        staff = (
            db.query(OrganizationStaff)
            .filter(OrganizationStaff.id == payload.created_by_staff_id, OrganizationStaff.organization_id == organization_id)
            .first()
        )
        if not staff:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Recording staff member does not exist")

    if payload.created_by_customer_id:
        creator_customer = (
            db.query(Customer)
            .filter(Customer.id == payload.created_by_customer_id, Customer.organization_id == organization_id)
            .first()
        )
        if not creator_customer:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Recording customer does not exist")


def _payment_query(db: Session, organization_id: int):
    return db.query(PaymentTransaction).filter(PaymentTransaction.organization_id == organization_id)


@router.get("", response_model=list[PaymentResponse])
def list_payments(
    search: Optional[str] = Query(default=None, max_length=120),
    status_filter: Optional[str] = Query(default=None, alias="status", max_length=30),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    organization: OrganizationContext = Depends(get_organization_context),
) -> list[PaymentTransaction]:
    query = _payment_query(db, organization.id)
    if status_filter:
        query = query.filter(PaymentTransaction.payment_status == status_filter.lower())
    if search:
        term = f"%{search.strip()}%"
        query = query.filter(
            or_(
                PaymentTransaction.transaction_reference.ilike(term),
                PaymentTransaction.external_reference.ilike(term),
                PaymentTransaction.customer_id.ilike(term),
                PaymentTransaction.created_by.ilike(term),
            )
        )
    return query.order_by(PaymentTransaction.created_at.desc()).offset(offset).limit(limit).all()


@router.get("/summary", response_model=PaymentSummary)
def payment_summary(
    db: Session = Depends(get_db),
    organization: OrganizationContext = Depends(get_organization_context),
) -> PaymentSummary:
    now = _utc_now()
    total_count = _payment_query(db, organization.id).count()
    return PaymentSummary(
        today_total=_paid_total(db, organization.id, _start_of_day(now)),
        week_total=_paid_total(db, organization.id, _start_of_week(now)),
        month_total=_paid_total(db, organization.id, _start_of_month(now)),
        year_total=_paid_total(db, organization.id, _start_of_year(now)),
        total_count=total_count,
    )


@router.get("/export")
def export_payments(
    db: Session = Depends(get_db),
    organization: OrganizationContext = Depends(get_organization_context),
) -> Response:
    rows = _payment_query(db, organization.id).order_by(PaymentTransaction.created_at.desc()).all()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "id",
        "transaction_reference",
        "external_reference",
        "customer_id",
        "user_id",
        "amount",
        "currency",
        "payment_method",
        "payment_status",
        "paid_at",
        "created_at",
        "created_by_staff_id",
        "created_by_customer_id",
        "created_by_principal_type",
        "recorded_by_label",
        "created_by",
        "notes",
    ])
    for row in rows:
        writer.writerow([
            row.id,
            row.transaction_reference,
            row.external_reference or "",
            row.customer_id,
            row.user_id or "",
            row.amount,
            row.currency,
            row.payment_method,
            row.payment_status,
            row.paid_at.isoformat() if row.paid_at else "",
            row.created_at.isoformat() if row.created_at else "",
            row.created_by_staff_id or "",
            row.created_by_customer_id or "",
            row.created_by_principal_type or "",
            row.recorded_by_label or "",
            row.created_by or "",
            row.notes or "",
        ])
    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="payments-export.csv"'},
    )


@router.get("/{payment_id}", response_model=PaymentResponse)
def get_payment(
    payment_id: int,
    db: Session = Depends(get_db),
    organization: OrganizationContext = Depends(get_organization_context),
) -> PaymentTransaction:
    return _payment_or_404(payment_id, db, organization.id)


@router.post("", response_model=PaymentResponse, status_code=status.HTTP_201_CREATED)
def create_payment(
    payload: PaymentCreate,
    db: Session = Depends(get_db),
    organization: OrganizationContext = Depends(get_organization_context),
) -> PaymentTransaction:
    _validate_customer_and_user(payload, db, organization.id)
    duplicate = (
        db.query(PaymentTransaction)
        .filter(
            PaymentTransaction.organization_id == organization.id,
            PaymentTransaction.transaction_reference == payload.transaction_reference,
        )
        .first()
    )
    if duplicate:
        raise conflict("duplicate_transaction_reference", "A payment with this transaction reference already exists.")

    paid_at = payload.paid_at
    if payload.payment_status == "paid" and paid_at is None:
        paid_at = _utc_now()

    payment = PaymentTransaction(
        organization_id=organization.id,
        customer_id=payload.customer_id,
        user_id=payload.user_id,
        transaction_reference=payload.transaction_reference,
        external_reference=payload.external_reference,
        amount=payload.amount,
        currency=payload.currency,
        payment_method=payload.payment_method,
        payment_status=payload.payment_status,
        paid_at=paid_at,
        created_by_staff_id=payload.created_by_staff_id,
        created_by_customer_id=payload.created_by_customer_id,
        created_by_principal_type=payload.created_by_principal_type or ("organization_staff" if payload.created_by_staff_id else "customer" if payload.created_by_customer_id else "system"),
        recorded_by_label=payload.recorded_by_label,
        created_by=payload.created_by,
        notes=payload.notes,
    )

    try:
        db.add(payment)
        if payment.user_id and payment.payment_status == "paid":
            account = (
                db.query(BillingAccount)
                .filter(BillingAccount.user_id == payment.user_id, BillingAccount.organization_id == organization.id)
                .first()
            )
            if account:
                account.last_payment_at = payment.paid_at
        record_audit(
            db,
            organization_id=organization.id,
            actor=payload.created_by or payload.recorded_by_label or "internal-admin",
            actor_type=payment.created_by_principal_type or "organization_staff",
            actor_id=str(payment.created_by_staff_id or payment.created_by_customer_id or "internal-admin"),
            actor_label=payload.recorded_by_label or payload.created_by,
            action="payment.created",
            target_type="payment",
            target_id=payload.transaction_reference,
            new_value={
                "customer_id": payload.customer_id,
                "user_id": payload.user_id,
                "amount": str(payload.amount),
                "currency": payload.currency,
                "payment_status": payload.payment_status,
            },
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise conflict("payment_conflict", "Payment transaction conflicts with an existing record.") from exc
    except Exception:
        db.rollback()
        raise

    db.refresh(payment)
    return payment
