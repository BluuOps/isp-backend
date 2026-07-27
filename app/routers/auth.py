import base64
import hashlib
import hmac
import json
import secrets
import time
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
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
from app.core.principal import ORGANIZATION_BRIDGE_PERMISSIONS, PLATFORM_PERMISSIONS
from app.core.tenant_host import request_hostname, resolve_tenant_from_request
from app.database import get_db
from app.models.organization import Organization
from app.models.organization_staff import OrganizationStaff
from app.services.audit import record_audit
from app.services.security import verify_password


router = APIRouter(prefix="/auth", tags=["auth"])
platform_auth_router = APIRouter(prefix="/platform/auth", tags=["platform-auth"])

TOKEN_TTL_SECONDS = 12 * 60 * 60


class LoginRequest(BaseModel):
    email: str = Field(min_length=1)
    password: str = Field(min_length=1)
    tenant_id: str | None = None


class AuthUser(BaseModel):
    id: str
    email: str
    fullName: str
    role: str
    tenantId: str
    principalType: str
    organizationSlug: str | None = None
    organizationId: int | None = None
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


def _platform_host(request: Request) -> bool:
    hostname = request_hostname(request)
    return any(hostname == f"app.{domain}" for domain in settings.tenant_allowed_domains)


def _require_platform_auth_config() -> tuple[str, str, str]:
    if not settings.platform_admin_email or not settings.platform_admin_password or not settings.jwt_secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Platform authentication is not configured",
        )
    return settings.platform_admin_email, settings.platform_admin_password, settings.jwt_secret


def _require_internal_auth_config() -> tuple[str, str, str]:
    if not settings.internal_admin_email or not settings.internal_admin_password or not settings.jwt_secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Internal authentication bridge is not configured",
        )
    return settings.internal_admin_email, settings.internal_admin_password, settings.jwt_secret


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(raw: str) -> bytes:
    return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))


def _sign(payload: str, secret: str) -> str:
    return _b64url_encode(hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).digest())


def _create_token(payload: dict[str, Any], secret: str) -> str:
    encoded_payload = _b64url_encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    return f"{encoded_payload}.{_sign(encoded_payload, secret)}"


def _decode_token(token: str, secret: str) -> dict[str, Any]:
    try:
        encoded_payload, signature = token.split(".", 1)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token") from exc

    expected = _sign(encoded_payload, secret)
    if not secrets.compare_digest(signature, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    try:
        payload = json.loads(_b64url_decode(encoded_payload))
    except (ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token") from exc

    expires_at = int(payload.get("exp", 0))
    if expires_at < int(time.time()):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token expired")
    return payload


def _get_organization_from_claims(db: Session, organization_id: object, organization_slug: object) -> Organization:
    try:
        normalized_organization_id = int(organization_id)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid organization claims") from exc

    normalized_slug = str(organization_slug or "")
    organization = db.query(Organization).filter(Organization.id == normalized_organization_id).first()
    if not organization:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid tenant")
    if organization.status != "active":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Tenant is not active")
    if organization.slug != normalized_slug:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Stale organization token")
    return organization


def _permissions(permissions: frozenset[str]) -> dict[str, bool]:
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


def _organization_auth_response(
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
            principalType="organization_staff",
            organizationSlug=organization.slug,
            organizationId=organization.id,
            permissions=_permissions(permissions),
        ),
        branding=TenantBranding(
            tenantId=organization.slug,
            ispName=organization.name,
            logoUrl=organization.logo,
            primaryColor="#2563eb",
        ),
    )


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
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    return staff


def _staff_password_valid(staff: OrganizationStaff, password: str) -> tuple[bool, str]:
    if verify_password(password, staff.password_hash):
        return True, "password"
    if (
        settings.internal_admin_email
        and settings.internal_admin_password
        and secrets.compare_digest(staff.email.lower(), settings.internal_admin_email.lower())
        and secrets.compare_digest(password, settings.internal_admin_password)
    ):
        return True, "internal_bridge"
    return False, "password"


def _platform_auth_response(email: str, token: str) -> AuthResponse:
    return AuthResponse(
        token=token,
        user=AuthUser(
            id="platform-admin",
            email=email,
            fullName="Platform Administrator",
            role="super_admin",
            tenantId="platform",
            principalType="platform_admin",
            organizationSlug=None,
            organizationId=None,
            permissions={permission: True for permission in PLATFORM_PERMISSIONS},
        ),
        branding=TenantBranding(
            tenantId="platform",
            ispName="RadiusFiber",
            logoUrl="/branding/radiusfiber-logo.svg",
            primaryColor="#2563eb",
        ),
    )


def _issue_platform_session(payload: LoginRequest) -> AuthResponse:
    configured_email, configured_password, secret = _require_platform_auth_config()
    if not secrets.compare_digest(payload.email.lower(), configured_email.lower()) or not secrets.compare_digest(
        payload.password,
        configured_password,
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    now = int(time.time())
    token = _create_token(
        {
            "sub": "platform-admin",
            "principal_type": "platform_admin",
            "email": configured_email,
            "role": "super_admin",
            "roles": ["platform_admin"],
            "permissions": PLATFORM_PERMISSIONS,
            "iat": now,
            "exp": now + TOKEN_TTL_SECONDS,
        },
        secret,
    )
    return _platform_auth_response(configured_email, token)


def _platform_session_from_authorization(authorization: str | None) -> AuthResponse:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token")
    if not settings.jwt_secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication is not configured",
        )

    token = authorization.split(" ", 1)[1].strip()
    payload = _decode_token(token, settings.jwt_secret)
    if payload.get("principal_type") != "platform_admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Platform administrator access is required",
        )
    return _platform_auth_response(str(payload.get("email", settings.platform_admin_email)), token)


@platform_auth_router.post("/login", response_model=AuthResponse)
def platform_login(payload: LoginRequest) -> AuthResponse:
    """Issue a Platform Administrator session without inferring tenant authority from Host."""
    return _issue_platform_session(payload)


@platform_auth_router.get("/me", response_model=AuthResponse)
def platform_me(authorization: str | None = Header(default=None)) -> AuthResponse:
    return _platform_session_from_authorization(authorization)


@platform_auth_router.post("/logout")
def platform_logout() -> dict[str, str]:
    return {"status": "ok"}


@router.post("/login", response_model=AuthResponse)
def login(payload: LoginRequest, request: Request, db: Session = Depends(get_db)) -> AuthResponse:
    if _platform_host(request):
        return _issue_platform_session(payload)

    tenant_context = resolve_tenant_from_request(request, db)
    organization = tenant_context.organization

    # Body-provided tenant identifiers are ignored for authorization. They are
    # accepted only as a staging compatibility hint when hostname fallback is
    # explicitly enabled, and even then they must match the resolved tenant.
    if tenant_context.source == "staging_fallback" and payload.tenant_id and payload.tenant_id != organization.slug:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid tenant")

    staff = _active_staff(db, organization.id, payload.email.strip().lower())
    valid, authentication_method = _staff_password_valid(staff, payload.password)
    if not valid:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    secret = settings.jwt_secret
    if not secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication is not configured",
        )
    now = int(time.time())
    token = create_access_token(
        {
            "sub": f"staff:{staff.id}",
            "principal_type": "organization_staff",
            "token_type": "access",
            "staff_id": staff.id,
            "organization_id": organization.id,
            "organization_slug": organization.slug,
            "tenant_id": organization.slug,
            "auth_method": authentication_method,
            "iat": now,
            "exp": now + TOKEN_TTL_SECONDS,
            "iss": settings.auth_token_issuer,
            "aud": settings.auth_token_audience,
            "jti": uuid.uuid4().hex,
        },
        secret,
    )
    record_audit(
        db,
        organization_id=organization.id,
        actor=staff.email,
        actor_type="organization_staff",
        actor_id=str(staff.id),
        actor_label=staff.email,
        action="auth.login",
        target_type="auth",
        target_id=f"staff:{staff.id}",
        new_value={
            "tenant_id": organization.slug,
            "authentication_method": authentication_method,
        },
    )
    db.commit()
    return _organization_auth_response(staff, organization, token)


@router.get("/me", response_model=AuthResponse)
def me(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> AuthResponse:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    if not settings.jwt_secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication is not configured",
        )
    payload = _decode_token(token, settings.jwt_secret)
    token = authorization.split(" ", 1)[1]
    if payload.get("principal_type") == "platform_admin":
        return _platform_session_from_authorization(authorization)
    principal = get_authenticated_principal(authorization, db)
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
    organization = _get_organization_from_claims(
        db,
        principal.organization_id,
        principal.organization_slug,
    )
    if not staff:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Identity is no longer available")
    return _organization_auth_response(staff, organization, token)


@router.post("/logout")
def logout() -> dict[str, str]:
    return {"status": "ok"}
