import secrets
from datetime import timezone

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.authorization import AuthenticatedPrincipal, PrincipalType
from app.core.config import settings
from app.core.principal import bearer_payload
from app.database import get_db
from app.services.token_revocation import ensure_token_not_revoked

PLATFORM_ADMIN_INVITATIONS_PERMISSION = "platform.organization_admins.manage"


def require_platform_principal(
    authorization: str | None = Header(default=None),
    x_platform_admin_key: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> AuthenticatedPrincipal:
    payload = bearer_payload(authorization)
    if payload and payload.get("principal_type") == "platform_admin":
        ensure_token_not_revoked(db, payload)
        return AuthenticatedPrincipal(
            subject_id=str(payload.get("sub", "platform-admin")),
            principal_type=PrincipalType.PLATFORM_ADMIN,
            authentication_method="bearer",
            active=True,
            platform_authority=True,
            organization_id=None,
            organization_slug=None,
            organization_role=None,
            effective_permissions=frozenset(
                str(item) for item in payload.get("permissions", [])
            ),
            actor_label=str(payload.get("email", "platform-admin")),
        )
    if payload:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Platform administrator access is required")

    expected = settings.platform_admin_api_key
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Platform administration is not configured",
        )
    if not x_platform_admin_key or not secrets.compare_digest(x_platform_admin_key, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid platform key")
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


require_platform_admin = require_platform_principal


def require_recent_platform_admin(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> AuthenticatedPrincipal:
    """Require a recent typed bearer principal for credential administration.

    The legacy platform API key intentionally cannot authorize this operation.
    Database time is authoritative for the recent-authentication decision.
    """
    payload = bearer_payload(authorization)
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Recent platform administrator authentication is required",
        )
    if payload.get("principal_type") != "platform_admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Platform administrator access is required",
        )
    ensure_token_not_revoked(db, payload)
    permissions = frozenset(str(item) for item in payload.get("permissions", []))
    if PLATFORM_ADMIN_INVITATIONS_PERMISSION not in permissions:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permission")
    try:
        issued_at = int(payload["iat"])
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid bearer token") from exc
    database_now = db.execute(select(func.current_timestamp())).scalar_one()
    if database_now.tzinfo is None:
        database_now = database_now.replace(tzinfo=timezone.utc)
    if issued_at < int(database_now.timestamp()) - settings.admin_invitation_recent_auth_seconds:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Recent platform administrator authentication is required",
        )
    return AuthenticatedPrincipal(
        subject_id=str(payload.get("sub", "platform-admin")),
        principal_type=PrincipalType.PLATFORM_ADMIN,
        authentication_method="bearer",
        active=True,
        platform_authority=True,
        organization_id=None,
        organization_slug=None,
        organization_role=None,
        effective_permissions=permissions,
        correlation_id=str(payload.get("jti")),
        actor_label=str(payload.get("email", "platform-admin")),
    )
