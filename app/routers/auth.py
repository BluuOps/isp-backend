import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.principal import ORGANIZATION_BRIDGE_PERMISSIONS, PLATFORM_PERMISSIONS
from app.core.tenant_host import request_hostname, resolve_tenant_from_request
from app.database import get_db
from app.models.organization import Organization
from app.services.audit import record_audit


router = APIRouter(prefix="/auth", tags=["auth"])

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


def _permissions() -> dict[str, bool]:
    return {permission: True for permission in ORGANIZATION_BRIDGE_PERMISSIONS}


def _organization_auth_response(email: str, organization: Organization, token: str) -> AuthResponse:
    return AuthResponse(
        token=token,
        user=AuthUser(
            id="internal-admin",
            email=email,
            fullName="Internal Administrator",
            role="tenant_admin",
            tenantId=organization.slug,
            principalType="organization_staff",
            organizationSlug=organization.slug,
            organizationId=organization.id,
            permissions=_permissions(),
        ),
        branding=TenantBranding(
            tenantId=organization.slug,
            ispName=organization.name,
            logoUrl=organization.logo,
            primaryColor="#2563eb",
        ),
    )


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


@router.post("/login", response_model=AuthResponse)
def login(payload: LoginRequest, request: Request, db: Session = Depends(get_db)) -> AuthResponse:
    if _platform_host(request):
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

    # Temporary RC1.1 internal auth bridge. Replace with full organization/customer auth in Phase 3.
    configured_email, configured_password, secret = _require_internal_auth_config()
    if not secrets.compare_digest(payload.email.lower(), configured_email.lower()) or not secrets.compare_digest(
        payload.password,
        configured_password,
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    tenant_context = resolve_tenant_from_request(request, db)
    organization = tenant_context.organization

    # Body-provided tenant identifiers are ignored for authorization. They are
    # accepted only as a staging compatibility hint when hostname fallback is
    # explicitly enabled, and even then they must match the resolved tenant.
    if tenant_context.source == "staging_fallback" and payload.tenant_id and payload.tenant_id != organization.slug:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid tenant")

    now = int(time.time())
    token = _create_token(
        {
            "sub": "internal-admin",
            "principal_type": "organization_staff",
            "email": configured_email,
            "organization_id": organization.id,
            "organization_slug": organization.slug,
            "tenant_id": organization.slug,
            "role": "tenant_admin",
            "roles": ["Organization Admin"],
            "permissions": ORGANIZATION_BRIDGE_PERMISSIONS,
            "iat": now,
            "exp": now + TOKEN_TTL_SECONDS,
        },
        secret,
    )
    record_audit(
        db,
        organization_id=organization.id,
        actor=configured_email,
        actor_type="organization_staff",
        actor_id="internal-admin",
        actor_label=configured_email,
        action="auth.login",
        target_type="auth",
        target_id="internal-admin",
        new_value={"tenant_id": organization.slug, "mode": "temporary_internal_bridge"},
    )
    db.commit()
    return _organization_auth_response(configured_email, organization, token)


@router.get("/me", response_model=AuthResponse)
def me(authorization: str | None = Header(default=None), db: Session = Depends(get_db)) -> AuthResponse:
    secret = settings.jwt_secret
    if not secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="JWT signing secret is not configured",
        )
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token")
    payload = _decode_token(authorization.split(" ", 1)[1], secret)
    token = authorization.split(" ", 1)[1]
    if payload.get("principal_type") == "platform_admin":
        return _platform_auth_response(str(payload.get("email", settings.platform_admin_email)), token)
    organization = _get_organization_from_claims(db, payload.get("organization_id"), payload.get("organization_slug"))
    return _organization_auth_response(str(payload.get("email", settings.internal_admin_email)), organization, token)


@router.post("/logout")
def logout() -> dict[str, str]:
    return {"status": "ok"}
