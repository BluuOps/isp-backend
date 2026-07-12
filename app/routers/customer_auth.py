from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.principal import bearer_payload, create_principal_token
from app.core.tenant_host import resolve_tenant_from_request
from app.database import get_db
from app.models import Customer, CustomerPortalAccount, Organization
from app.schemas.customer_auth import (
    CustomerAuthLoginRequest,
    CustomerAuthResponse,
    CustomerTenantResponse,
    CustomerAuthUser,
    CustomerPasswordChangeRequest,
)
from app.services.audit import record_audit
from app.services.security import hash_password, verify_password


router = APIRouter(prefix="/customer-auth", tags=["Customer Authentication"])

LOCKOUT_THRESHOLD = 5
LOCKOUT_WINDOW = timedelta(minutes=15)
ACTIVE_STATUS = "active"


def normalize_identifier(value: str) -> str:
    return value.strip().lower()


def _auth_user(account: CustomerPortalAccount, organization: Organization) -> CustomerAuthUser:
    return CustomerAuthUser(
        id=account.id,
        organization_id=account.organization_id,
        organization_slug=organization.slug,
        customer_id=account.customer_id,
        email=account.email,
        phone=account.phone,
        status=account.status,
    )


def _token(account: CustomerPortalAccount, organization: Organization) -> str:
    return create_principal_token(
        {
            "sub": str(account.id),
            "principal_type": "customer",
            "organization_id": account.organization_id,
            "organization_slug": organization.slug,
            "customer_id": account.customer_id,
        }
    )


def _account_from_token(db: Session, authorization: str | None) -> tuple[CustomerPortalAccount, Organization]:
    payload = bearer_payload(authorization)
    if not payload or payload.get("principal_type") != "customer":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing customer token")

    account = (
        db.query(CustomerPortalAccount)
        .filter(
            CustomerPortalAccount.id == int(payload["sub"]),
            CustomerPortalAccount.customer_id == payload.get("customer_id"),
            CustomerPortalAccount.organization_id == int(payload["organization_id"]),
        )
        .first()
    )
    if not account or account.status != ACTIVE_STATUS:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid customer token")

    organization = db.query(Organization).filter(Organization.id == account.organization_id).first()
    if not organization:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid customer organization")
    return account, organization


@router.post("/login", response_model=CustomerAuthResponse)
def login(payload: CustomerAuthLoginRequest, request: Request, db: Session = Depends(get_db)) -> CustomerAuthResponse:
    identifier = normalize_identifier(payload.identifier)
    tenant_context = resolve_tenant_from_request(request, db)
    organization = tenant_context.organization

    # Temporary staging compatibility only. The hostname resolver remains the
    # authoritative source; frontend-provided tenant fields are ignored unless
    # ALLOW_STAGING_TENANT_FALLBACK explicitly enabled the fallback path.
    if tenant_context.source == "staging_fallback":
        requested_slug = payload.organization_slug or payload.tenant_id
        if requested_slug and requested_slug != organization.slug:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    query = db.query(CustomerPortalAccount).filter(
        CustomerPortalAccount.organization_id == organization.id,
        or_(CustomerPortalAccount.email == identifier, CustomerPortalAccount.phone == identifier),
    )
    matches = query.limit(2).all()
    account = matches[0] if len(matches) == 1 else None

    if not account or not organization or not verify_password(payload.password, account.password_hash):
        if account:
            account.failed_login_count += 1
            if account.failed_login_count >= LOCKOUT_THRESHOLD:
                account.status = "locked"
                account.locked_until = datetime.now(timezone.utc) + LOCKOUT_WINDOW
            record_audit(
                db,
                organization_id=account.organization_id,
                actor_type="customer",
                actor_id=str(account.id),
                actor_label=account.email,
                actor=account.email,
                action="login.failed",
                target_type="customer_portal_account",
                target_id=str(account.id),
                success=False,
            )
            db.commit()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    now = datetime.now(timezone.utc)
    if account.status != ACTIVE_STATUS or (account.locked_until and account.locked_until > now):
        record_audit(
            db,
            organization_id=account.organization_id,
            actor_type="customer",
            actor_id=str(account.id),
            actor_label=account.email,
            actor=account.email,
            action="login.failed",
            target_type="customer_portal_account",
            target_id=str(account.id),
                new_value={"status": account.status, "locked": bool(account.locked_until and account.locked_until > now)},
            success=False,
        )
        db.commit()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    account.failed_login_count = 0
    account.last_login_at = now
    record_audit(
        db,
        organization_id=account.organization_id,
        actor_type="customer",
        actor_id=str(account.id),
        actor_label=account.email,
        actor=account.email,
        action="login.success",
        target_type="customer_portal_account",
        target_id=str(account.id),
    )
    db.commit()
    return CustomerAuthResponse(token=_token(account, organization), user=_auth_user(account, organization))


@router.get("/me", response_model=CustomerAuthResponse)
def me(authorization: str | None = Header(default=None), db: Session = Depends(get_db)) -> CustomerAuthResponse:
    account, organization = _account_from_token(db, authorization)
    return CustomerAuthResponse(token=authorization.split(" ", 1)[1], user=_auth_user(account, organization))


@router.get("/tenant", response_model=CustomerTenantResponse)
def tenant(request: Request, db: Session = Depends(get_db)) -> CustomerTenantResponse:
    context = resolve_tenant_from_request(request, db)
    organization = context.organization
    return CustomerTenantResponse(
        id=organization.id,
        name=organization.name,
        slug=organization.slug,
        logo=organization.logo,
        currency=organization.currency,
        timezone=organization.timezone,
        resolution_source=context.source,
    )


@router.post("/logout")
def logout() -> dict[str, str]:
    return {"status": "ok"}


@router.post("/change-password")
def change_password(
    payload: CustomerPasswordChangeRequest,
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    account, _ = _account_from_token(db, authorization)
    if not verify_password(payload.current_password, account.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    account.password_hash = hash_password(payload.new_password)
    account.password_changed_at = datetime.now(timezone.utc)
    record_audit(
        db,
        organization_id=account.organization_id,
        actor_type="customer",
        actor_id=str(account.id),
        actor_label=account.email,
        actor=account.email,
        action="customer.password_changed",
        target_type="customer_portal_account",
        target_id=str(account.id),
    )
    db.commit()
    return {"status": "ok"}
