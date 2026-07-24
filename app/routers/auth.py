import secrets
import time
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.authorization import (
    AuthenticatedPrincipal,
    PrincipalType,
    create_access_token,
    get_authenticated_principal,
    role_permissions,
)
from app.core.config import settings
from app.database import get_db
from app.models.organization import Organization
from app.models.organization_staff import OrganizationStaff
from app.services.audit import record_audit
from app.services.security import verify_password


router = APIRouter(prefix="/auth", tags=["auth"])

TOKEN_TTL_SECONDS = 12 * 60 * 60


class LoginRequest(BaseModel):
    email: str = Field(min_length=1)
    password: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)


class AuthUser(BaseModel):
    id: str
    email: str
    fullName: str
    role: str
    tenantId: str
    permissions: dict[str, bool]


class TenantBranding(BaseModel):
    tenantId: str
    ispName: str
    logoUrl: str | None = None
    primaryColor: str | None = None


class AuthResponse(BaseModel):
    token: str
    user: AuthUser
    branding: TenantBranding


def _require_jwt_secret() -> str:
    if not settings.jwt_secret:
        raise HTTPException(
            status_code=503,
            detail="Authentication is not configured",
        )
    return settings.jwt_secret


def _active_organization(
    db: Session,
    tenant_id: str,
) -> Organization:
    organization = (
        db.query(Organization)
        .filter(
            Organization.slug == tenant_id,
            Organization.status == "active",
        )
        .first()
    )
    if not organization:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    return organization


def _active_staff(
    db: Session,
    organization_id: int,
    email: str,
) -> OrganizationStaff:
    staff = (
        db.query(OrganizationStaff)
        .filter(
            OrganizationStaff.organization_id == organization_id,
            OrganizationStaff.email == email,
            OrganizationStaff.status == "active",
        )
        .first()
    )
    if not staff:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    return staff


def _password_valid(
    staff: OrganizationStaff,
    password: str,
) -> tuple[bool, str]:
    if verify_password(password, staff.password_hash):
        return True, "password"

    if (
        settings.internal_admin_email
        and settings.internal_admin_password
        and secrets.compare_digest(
            staff.email.lower(),
            settings.internal_admin_email.lower(),
        )
        and secrets.compare_digest(password, settings.internal_admin_password)
    ):
        return True, "internal_bridge"

    return False, "password"


def _permission_flags(permissions: frozenset[str]) -> dict[str, bool]:
    flags = {permission: True for permission in sorted(permissions)}
    flags.update(
        {
            "radius_access": "radius.sessions.read" in permissions,
            "disconnect_user": "radius.sessions.disconnect" in permissions,
            "create_pppoe": "subscribers.create" in permissions,
            "view_customers": "customers.read" in permissions,
            "delete_customer": "customers.delete" in permissions,
            "billing_access": "billing.accounts.read" in permissions,
            "settings_access": "organization.settings.read" in permissions,
        }
    )
    return flags


def _auth_response(
    staff: OrganizationStaff,
    organization: Organization,
    token: str,
) -> AuthResponse:
    permissions = role_permissions(staff.role)
    return AuthResponse(
        token=token,
        user=AuthUser(
            id=f"staff:{staff.id}",
            email=staff.email,
            fullName=staff.name,
            role=staff.role,
            tenantId=organization.slug,
            permissions=_permission_flags(permissions),
        ),
        branding=TenantBranding(
            tenantId=organization.slug,
            ispName=organization.name,
            logoUrl=organization.logo,
            primaryColor="#2563eb",
        ),
    )


@router.post("/login", response_model=AuthResponse)
def login(
    payload: LoginRequest,
    db: Session = Depends(get_db),
) -> AuthResponse:
    organization = _active_organization(db, payload.tenant_id)
    staff = _active_staff(
        db,
        organization.id,
        payload.email.strip().lower(),
    )
    valid, authentication_method = _password_valid(staff, payload.password)
    if not valid:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    now = int(time.time())
    token = create_access_token(
        {
            "sub": f"staff:{staff.id}",
            "principal_type": PrincipalType.ORGANIZATION_STAFF.value,
            "token_type": "access",
            "staff_id": staff.id,
            "organization_id": organization.id,
            "organization_slug": organization.slug,
            "auth_method": authentication_method,
            "iat": now,
            "exp": now + TOKEN_TTL_SECONDS,
            "iss": settings.auth_token_issuer,
            "aud": settings.auth_token_audience,
            "jti": uuid.uuid4().hex,
        },
        _require_jwt_secret(),
    )
    record_audit(
        db,
        organization_id=organization.id,
        actor=staff.email,
        action="auth.login",
        target_type="auth",
        target_id=f"staff:{staff.id}",
        new_value={
            "tenant_id": organization.slug,
            "authentication_method": authentication_method,
        },
    )
    db.commit()
    return _auth_response(staff, organization, token)


@router.get("/me", response_model=AuthResponse)
def me(
    authorization: str | None = Header(default=None),
    principal: AuthenticatedPrincipal = Depends(get_authenticated_principal),
    db: Session = Depends(get_db),
) -> AuthResponse:
    if (
        principal.principal_type != PrincipalType.ORGANIZATION_STAFF
        or principal.organization_id is None
    ):
        raise HTTPException(status_code=403, detail="Organization staff access required")

    staff_id = int(principal.subject_id.split(":", 1)[1])
    staff = (
        db.query(OrganizationStaff)
        .filter(
            OrganizationStaff.id == staff_id,
            OrganizationStaff.organization_id == principal.organization_id,
            OrganizationStaff.status == "active",
        )
        .first()
    )
    organization = (
        db.query(Organization)
        .filter(
            Organization.id == principal.organization_id,
            Organization.status == "active",
        )
        .first()
    )
    if not staff or not organization:
        raise HTTPException(status_code=401, detail="Identity is no longer available")
    token = authorization.split(" ", 1)[1].strip() if authorization else ""
    return _auth_response(staff, organization, token)


@router.post("/logout")
def logout(
    _principal: AuthenticatedPrincipal = Depends(get_authenticated_principal),
) -> dict[str, str]:
    return {"status": "ok"}
