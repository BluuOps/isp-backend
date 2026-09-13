import secrets
import time
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.core.authorization import (
    AuthenticatedPrincipal,
    PrincipalType,
    create_access_token,
    decode_access_token,
    get_authenticated_principal,
    staff_permissions,
)
from app.core.config import settings
from app.core.principal import (
    ORGANIZATION_BRIDGE_PERMISSIONS,
    PLATFORM_PERMISSIONS,
    create_principal_token,
    decode_principal_token,
)
from app.core.tenant_host import request_hostname, resolve_tenant_from_request
from app.database import get_db
from app.models.organization import Organization
from app.models.organization_staff import OrganizationStaff
from app.services.audit import record_audit
from app.services.security import verify_password
from app.services.token_revocation import ensure_token_not_revoked, revoke_token, token_fingerprint
from app.services.admin_invitations import accept_admin_invitation


router = APIRouter(prefix="/auth", tags=["auth"])

class LoginRequest(BaseModel):
    email: str = Field(min_length=1)
    password: str = Field(min_length=1)
    tenant_id: str | None = None


class AdminInvitationAcceptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    invitation_token: str = Field(min_length=32, max_length=256)
    new_password: str = Field(min_length=12, max_length=128)


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
    permissions = staff_permissions(staff)
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
            or_(
                OrganizationStaff.is_uat_fixture.is_(False),
                (
                    OrganizationStaff.is_uat_fixture.is_(True)
                    & (OrganizationStaff.role == "Read Only")
                    & OrganizationStaff.uat_revoked_at.is_(None)
                    & (OrganizationStaff.uat_expires_at > func.current_timestamp())
                ),
            ),
        )
        .first()
    )
    if not staff:
        fixture = (
            db.query(OrganizationStaff)
            .filter(
                OrganizationStaff.organization_id == organization_id,
                OrganizationStaff.email == email,
                OrganizationStaff.is_uat_fixture.is_(True),
            )
            .first()
        )
        if fixture:
            _record_fixture_authentication(
                db,
                fixture,
                action="uat.read_only_fixture.authentication_rejected",
                success=False,
                reason="inactive_or_expired",
            )
            db.commit()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    return staff


def _record_fixture_authentication(
    db: Session,
    staff: OrganizationStaff,
    *,
    action: str,
    success: bool,
    reason: str,
) -> None:
    record_audit(
        db,
        organization_id=staff.organization_id,
        actor=staff.email,
        actor_type="organization_staff",
        actor_id=str(staff.id),
        actor_label=staff.email,
        action=action,
        target_type="organization_staff",
        target_id=str(staff.id),
        success=success,
        new_value={
            "fixture_id": staff.uat_fixture_id,
            "outcome": reason,
        },
    )


def _staff_password_valid(staff: OrganizationStaff, password: str) -> tuple[bool, str]:
    if verify_password(password, staff.password_hash):
        return True, "password"
    if (
        getattr(staff, "credentials_revoked_at", None) is None
        and settings.internal_admin_email
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


@router.post("/admin-invitations/accept")
def accept_organization_admin_invitation(
    payload: AdminInvitationAcceptRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, str]:
    client_host = request.client.host if request.client else "unknown"
    try:
        invitation, _staff = accept_admin_invitation(
            db,
            token=payload.invitation_token,
            new_password=payload.new_password,
            rate_identity=client_host,
        )
        db.commit()
    except HTTPException:
        if db.in_transaction():
            db.rollback()
        raise
    except Exception:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invitation is invalid or unavailable",
        )
    return {"status": "accepted", "purpose": invitation.purpose}


@router.post("/login", response_model=AuthResponse)
def login(payload: LoginRequest, request: Request, db: Session = Depends(get_db)) -> AuthResponse:
    if _platform_host(request):
        configured_email, configured_password, _ = _require_platform_auth_config()
        if not secrets.compare_digest(payload.email.lower(), configured_email.lower()) or not secrets.compare_digest(
            payload.password,
            configured_password,
        ):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

        token = create_principal_token(
            {
                "sub": "platform-admin",
                "principal_type": "platform_admin",
                "email": configured_email,
                "role": "super_admin",
                "roles": ["platform_admin"],
                "permissions": PLATFORM_PERMISSIONS,
            },
        )
        return _platform_auth_response(configured_email, token)

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
        if staff.is_uat_fixture:
            _record_fixture_authentication(
                db,
                staff,
                action="uat.read_only_fixture.authentication_rejected",
                success=False,
                reason="invalid_password",
            )
            db.commit()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    secret = settings.jwt_secret
    if not secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication is not configured",
        )
    now = int(time.time())
    token_expires_at = now + settings.auth_access_token_ttl_seconds
    if staff.is_uat_fixture:
        if staff.uat_expires_at is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid UAT fixture")
        token_expires_at = min(token_expires_at, int(staff.uat_expires_at.timestamp()))
    jti = uuid.uuid4().hex
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
            "exp": token_expires_at,
            "iss": settings.auth_token_issuer,
            "aud": settings.auth_token_audience,
            "jti": jti,
            "credential_version": getattr(staff, "credential_version", None) or 1,
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
        action=(
            "uat.read_only_fixture.authentication_succeeded"
            if staff.is_uat_fixture
            else "auth.login"
        ),
        target_type="auth",
        target_id=f"staff:{staff.id}",
        new_value={
            "tenant_id": organization.slug,
            "authentication_method": authentication_method,
            "correlation_id": token_fingerprint({"jti": jti}),
            **(
                {
                    "uat_fixture_id": staff.uat_fixture_id,
                    "uat_expires_at": staff.uat_expires_at.isoformat(),
                }
                if staff.is_uat_fixture
                else {}
            ),
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
    payload = decode_principal_token(token)
    token = authorization.split(" ", 1)[1]
    if payload.get("principal_type") == "platform_admin":
        ensure_token_not_revoked(db, payload)
        return _platform_auth_response(str(payload.get("email", settings.platform_admin_email)), token)
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
def logout(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token")
    if not settings.jwt_secret:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Authentication is not configured")
    token = authorization.split(" ", 1)[1].strip()
    payload = decode_principal_token(token)
    principal_type = payload.get("principal_type")
    if principal_type == PrincipalType.ORGANIZATION_STAFF.value:
        payload = decode_access_token(token, settings.jwt_secret)
    elif principal_type != PrincipalType.PLATFORM_ADMIN.value:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Unsupported principal type")

    created = revoke_token(db, payload)
    if created:
        record_audit(
            db,
            organization_id=payload.get("organization_id"),
            actor_type=str(principal_type),
            actor_id=str(payload.get("sub")),
            actor_label=str(payload.get("email") or payload.get("sub")),
            actor=str(payload.get("email") or payload.get("sub")),
            action="auth.logout",
            target_type="auth_token",
            target_id=token_fingerprint(payload),
            new_value={"reason": "logout"},
        )
    db.commit()
    return {"status": "ok"}
