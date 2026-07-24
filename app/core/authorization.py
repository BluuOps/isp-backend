from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, TypeVar

from fastapi import Depends, Header, HTTPException
from sqlalchemy.orm import Query, Session

from app.core.config import settings
from app.database import get_db
from app.models.organization import Organization
from app.models.organization_staff import OrganizationStaff


class PrincipalType(str, Enum):
    PLATFORM_ADMIN = "platform_admin"
    ORGANIZATION_STAFF = "organization_staff"
    CUSTOMER = "customer"
    SERVICE = "service"


class Permission:
    ORGANIZATION_PROFILE_READ = "organization.profile.read"
    ORGANIZATION_PROFILE_UPDATE = "organization.profile.update"
    ORGANIZATION_STAFF_READ = "organization.staff.read"
    ORGANIZATION_STAFF_MANAGE = "organization.staff.manage"
    ORGANIZATION_SETTINGS_READ = "organization.settings.read"
    ORGANIZATION_SETTINGS_UPDATE = "organization.settings.update"
    ORGANIZATION_SUBSCRIPTION_READ = "organization.subscription.read"
    ORGANIZATION_FEATURE_FLAGS_READ = "organization.feature_flags.read"
    ORGANIZATION_FEATURE_FLAGS_MANAGE = "organization.feature_flags.manage"
    ORGANIZATION_AUDIT_LOGS_READ = "organization.audit_logs.read"
    CUSTOMERS_READ = "customers.read"
    CUSTOMERS_CREATE = "customers.create"
    CUSTOMERS_UPDATE = "customers.update"
    CUSTOMERS_DELETE = "customers.delete"
    SUBSCRIBERS_READ = "subscribers.read"
    SUBSCRIBERS_CREATE = "subscribers.create"
    SUBSCRIBERS_UPDATE = "subscribers.update"
    SUBSCRIBERS_DELETE = "subscribers.delete"
    SUBSCRIBERS_SUSPEND = "subscribers.suspend"
    SUBSCRIBERS_RECONNECT = "subscribers.reconnect"
    SUBSCRIBERS_RECHARGE = "subscribers.recharge"
    SUBSCRIBERS_PASSWORD_CHANGE = "subscribers.password.change"
    SUBSCRIBERS_PLAN_CHANGE = "subscribers.plan.change"
    RADIUS_SESSIONS_READ = "radius.sessions.read"
    RADIUS_SESSIONS_DISCONNECT = "radius.sessions.disconnect"
    PLANS_READ = "plans.read"
    PLANS_CREATE = "plans.create"
    PLANS_UPDATE = "plans.update"
    PLANS_DELETE = "plans.delete"
    BILLING_ACCOUNTS_READ = "billing.accounts.read"
    BILLING_ACCOUNTS_CREATE = "billing.accounts.create"
    BILLING_ACCOUNTS_UPDATE = "billing.accounts.update"
    BILLING_ACCOUNTS_DELETE = "billing.accounts.delete"
    PAYMENTS_READ = "payments.read"
    PAYMENTS_CREATE = "payments.create"
    PAYMENTS_UPDATE = "payments.update"
    PAYMENTS_VERIFY = "payments.verify"
    PAYMENTS_EXPORT = "payments.export"
    NETWORK_NAS_READ = "network.nas.read"
    NETWORK_NAS_CREATE = "network.nas.create"
    NETWORK_NAS_UPDATE = "network.nas.update"
    NETWORK_NAS_DELETE = "network.nas.delete"
    NETWORK_ZONES_READ = "network.zones.read"
    NETWORK_ZONES_CREATE = "network.zones.create"
    NETWORK_ZONES_UPDATE = "network.zones.update"
    NETWORK_ZONES_DELETE = "network.zones.delete"


ALL_ORGANIZATION_PERMISSIONS = frozenset(
    value
    for name, value in vars(Permission).items()
    if name.isupper() and isinstance(value, str)
)

READ_ONLY_PERMISSIONS = frozenset(
    {
        Permission.ORGANIZATION_PROFILE_READ,
        Permission.ORGANIZATION_SUBSCRIPTION_READ,
        Permission.CUSTOMERS_READ,
        Permission.SUBSCRIBERS_READ,
        Permission.RADIUS_SESSIONS_READ,
        Permission.PLANS_READ,
        Permission.NETWORK_NAS_READ,
        Permission.NETWORK_ZONES_READ,
    }
)

ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "Organization Admin": ALL_ORGANIZATION_PERMISSIONS,
    "NOC": frozenset(
        {
            Permission.ORGANIZATION_PROFILE_READ,
            Permission.ORGANIZATION_SUBSCRIPTION_READ,
            Permission.CUSTOMERS_READ,
            Permission.SUBSCRIBERS_READ,
            Permission.SUBSCRIBERS_CREATE,
            Permission.SUBSCRIBERS_UPDATE,
            Permission.SUBSCRIBERS_SUSPEND,
            Permission.SUBSCRIBERS_RECONNECT,
            Permission.SUBSCRIBERS_RECHARGE,
            Permission.SUBSCRIBERS_PASSWORD_CHANGE,
            Permission.SUBSCRIBERS_PLAN_CHANGE,
            Permission.RADIUS_SESSIONS_READ,
            Permission.RADIUS_SESSIONS_DISCONNECT,
            Permission.PLANS_READ,
            Permission.NETWORK_NAS_READ,
            Permission.NETWORK_NAS_CREATE,
            Permission.NETWORK_NAS_UPDATE,
            Permission.NETWORK_ZONES_READ,
            Permission.NETWORK_ZONES_CREATE,
            Permission.NETWORK_ZONES_UPDATE,
        }
    ),
    "Billing": frozenset(
        {
            Permission.ORGANIZATION_PROFILE_READ,
            Permission.ORGANIZATION_SUBSCRIPTION_READ,
            Permission.CUSTOMERS_READ,
            Permission.SUBSCRIBERS_READ,
            Permission.PLANS_READ,
            Permission.BILLING_ACCOUNTS_READ,
            Permission.BILLING_ACCOUNTS_CREATE,
            Permission.BILLING_ACCOUNTS_UPDATE,
            Permission.PAYMENTS_READ,
            Permission.PAYMENTS_CREATE,
            Permission.PAYMENTS_EXPORT,
            Permission.NETWORK_NAS_READ,
            Permission.NETWORK_ZONES_READ,
        }
    ),
    "Support": frozenset(
        {
            Permission.ORGANIZATION_PROFILE_READ,
            Permission.CUSTOMERS_READ,
            Permission.CUSTOMERS_UPDATE,
            Permission.SUBSCRIBERS_READ,
            Permission.RADIUS_SESSIONS_READ,
            Permission.PLANS_READ,
            Permission.NETWORK_NAS_READ,
            Permission.NETWORK_ZONES_READ,
        }
    ),
    "Field Engineer": frozenset(
        {
            Permission.ORGANIZATION_PROFILE_READ,
            Permission.CUSTOMERS_READ,
            Permission.SUBSCRIBERS_READ,
            Permission.RADIUS_SESSIONS_READ,
            Permission.PLANS_READ,
            Permission.NETWORK_NAS_READ,
            Permission.NETWORK_ZONES_READ,
        }
    ),
    "Read Only": READ_ONLY_PERMISSIONS,
    "Customer": frozenset(),
}

ASSIGNABLE_ORGANIZATION_ROLES = frozenset(ROLE_PERMISSIONS)


@dataclass(frozen=True)
class AuthenticatedPrincipal:
    subject_id: str
    principal_type: PrincipalType
    authentication_method: str
    active: bool
    platform_authority: bool
    organization_id: int | None
    organization_slug: str | None
    organization_role: str | None
    effective_permissions: frozenset[str]
    customer_id: str | None = None
    correlation_id: str | None = None
    actor_label: str = "system"

    def has_permission(self, permission: str) -> bool:
        return self.platform_authority or permission in self.effective_permissions


def role_permissions(role: str) -> frozenset[str]:
    return ROLE_PERMISSIONS.get(role, frozenset())


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(raw: str) -> bytes:
    return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))


def _sign(payload: str, secret: str) -> str:
    return _b64url_encode(
        hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest()
    )


def create_access_token(claims: dict[str, Any], secret: str) -> str:
    encoded = _b64url_encode(
        json.dumps(claims, separators=(",", ":"), sort_keys=True).encode()
    )
    return f"{encoded}.{_sign(encoded, secret)}"


def decode_access_token(
    token: str,
    secret: str,
    now: int | None = None,
) -> dict[str, Any]:
    try:
        encoded, signature = token.split(".", 1)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="Invalid token") from exc
    if not secrets.compare_digest(signature, _sign(encoded, secret)):
        raise HTTPException(status_code=401, detail="Invalid token")
    try:
        payload = json.loads(_b64url_decode(encoded))
    except (
        ValueError,
        json.JSONDecodeError,
        binascii.Error,
        UnicodeDecodeError,
    ) as exc:
        raise HTTPException(status_code=401, detail="Invalid token") from exc

    required = {
        "sub",
        "principal_type",
        "token_type",
        "staff_id",
        "organization_id",
        "organization_slug",
        "iat",
        "exp",
        "iss",
        "aud",
        "jti",
    }
    if not isinstance(payload, dict) or required - payload.keys():
        raise HTTPException(status_code=401, detail="Token claims are incomplete")
    if payload["token_type"] != "access":
        raise HTTPException(status_code=401, detail="Invalid token type")
    if payload["principal_type"] != PrincipalType.ORGANIZATION_STAFF.value:
        raise HTTPException(status_code=401, detail="Invalid principal type")
    if (
        payload["iss"] != settings.auth_token_issuer
        or payload["aud"] != settings.auth_token_audience
    ):
        raise HTTPException(status_code=401, detail="Invalid token audience")

    current_time = int(time.time()) if now is None else now
    try:
        issued_at = int(payload["iat"])
        expires_at = int(payload["exp"])
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=401, detail="Invalid token timestamps") from exc
    if issued_at > current_time + 60 or expires_at <= current_time:
        raise HTTPException(status_code=401, detail="Token expired or not yet valid")
    return payload


def _bearer_token(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Missing bearer token")
    return token


def get_authenticated_principal(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> AuthenticatedPrincipal:
    if not settings.jwt_secret:
        raise HTTPException(status_code=503, detail="Authentication is not configured")
    payload = decode_access_token(_bearer_token(authorization), settings.jwt_secret)
    try:
        staff_id = int(payload["staff_id"])
        organization_id = int(payload["organization_id"])
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=401, detail="Invalid identity claims") from exc

    staff = (
        db.query(OrganizationStaff)
        .filter(
            OrganizationStaff.id == staff_id,
            OrganizationStaff.organization_id == organization_id,
        )
        .first()
    )
    organization = (
        db.query(Organization)
        .filter(
            Organization.id == organization_id,
            Organization.slug == str(payload["organization_slug"]),
        )
        .first()
    )
    if not staff or str(payload["sub"]) != f"staff:{staff.id}":
        raise HTTPException(status_code=401, detail="Identity is no longer available")
    if staff.status != "active":
        raise HTTPException(status_code=401, detail="Identity is disabled")
    if not organization or organization.status != "active":
        raise HTTPException(
            status_code=403,
            detail="Organization membership is inactive",
        )

    return AuthenticatedPrincipal(
        subject_id=f"staff:{staff.id}",
        principal_type=PrincipalType.ORGANIZATION_STAFF,
        authentication_method=str(payload.get("auth_method", "password")),
        active=True,
        platform_authority=False,
        organization_id=organization.id,
        organization_slug=organization.slug,
        organization_role=staff.role,
        effective_permissions=role_permissions(staff.role),
        correlation_id=str(payload["jti"]),
        actor_label=staff.email,
    )


def require_permission(
    permission: str,
) -> Callable[..., AuthenticatedPrincipal]:
    def dependency(
        principal: AuthenticatedPrincipal = Depends(get_authenticated_principal),
    ) -> AuthenticatedPrincipal:
        if not principal.has_permission(permission):
            raise HTTPException(status_code=403, detail="Insufficient permission")
        return principal

    dependency.__name__ = f"require_{permission.replace('.', '_')}"
    setattr(dependency, "required_permission", permission)
    return dependency


def require_platform_principal(
    x_platform_admin_key: str | None = Header(default=None),
) -> AuthenticatedPrincipal:
    expected = settings.platform_admin_api_key
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="Platform administration is not configured",
        )
    if not x_platform_admin_key or not secrets.compare_digest(
        x_platform_admin_key,
        expected,
    ):
        raise HTTPException(status_code=401, detail="Invalid platform credential")
    return AuthenticatedPrincipal(
        subject_id="platform-api-key",
        principal_type=PrincipalType.PLATFORM_ADMIN,
        authentication_method="api_key",
        active=True,
        platform_authority=True,
        organization_id=None,
        organization_slug=None,
        organization_role=None,
        effective_permissions=frozenset(),
        actor_label="platform-admin",
    )


ModelT = TypeVar("ModelT")


def scope_query_to_tenant(
    query: Query,
    model: type[ModelT],
    organization_id: int,
) -> Query:
    return query.filter(model.organization_id == organization_id)


def load_tenant_object_or_404(
    db: Session,
    model: type[ModelT],
    object_id: Any,
    organization_id: int,
) -> ModelT:
    object_row = (
        db.query(model)
        .filter(
            model.id == object_id,
            model.organization_id == organization_id,
        )
        .first()
    )
    if not object_row:
        raise HTTPException(status_code=404, detail="Object not found")
    return object_row
