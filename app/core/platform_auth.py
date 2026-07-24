import secrets

from fastapi import Header, HTTPException, status

from app.core.authorization import AuthenticatedPrincipal, PrincipalType
from app.core.config import settings
from app.core.principal import bearer_payload


def require_platform_principal(
    authorization: str | None = Header(default=None),
    x_platform_admin_key: str | None = Header(default=None),
) -> AuthenticatedPrincipal:
    payload = bearer_payload(authorization)
    if payload and payload.get("principal_type") == "platform_admin":
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
