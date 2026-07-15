from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from app.core.principal import require_organization_staff_principal
from app.database import get_db
from app.models.organization import Organization


@dataclass(frozen=True)
class OrganizationContext:
    id: int
    slug: str
    name: str
    principal_id: str
    roles: tuple[str, ...]
    permissions: tuple[str, ...]


def _tenant_error(http_status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=http_status, detail={"error": code, "message": message})


def _claim_list(value: object) -> tuple[str, ...]:
    if isinstance(value, list):
        return tuple(str(item) for item in value)
    if isinstance(value, tuple):
        return tuple(str(item) for item in value)
    return ()


def get_organization_context(
    db: Session = Depends(get_db),
    authorization: str | None = Header(default=None),
) -> OrganizationContext:
    principal = require_organization_staff_principal(authorization)
    organization_id = principal.get("organization_id")
    organization_slug = str(principal.get("organization_slug") or "")
    try:
        normalized_organization_id = int(organization_id)
    except (TypeError, ValueError) as exc:
        raise _tenant_error(
            status.HTTP_401_UNAUTHORIZED,
            "invalid_organization_claims",
            "Organization staff token has invalid organization claims.",
        ) from exc

    organization = db.query(Organization).filter(Organization.id == normalized_organization_id).first()
    if not organization:
        raise _tenant_error(
            status.HTTP_403_FORBIDDEN,
            "organization_not_available",
            "Organization is not available for this token.",
        )
    if organization.status != "active":
        raise _tenant_error(
            status.HTTP_403_FORBIDDEN,
            "organization_inactive",
            "Organization is not active.",
        )
    if organization.slug != organization_slug:
        raise _tenant_error(
            status.HTTP_401_UNAUTHORIZED,
            "stale_organization_token",
            "Organization token does not match the current organization.",
        )

    return OrganizationContext(
        id=organization.id,
        slug=organization.slug,
        name=organization.name,
        principal_id=str(principal.get("sub") or ""),
        roles=_claim_list(principal.get("roles")),
        permissions=_claim_list(principal.get("permissions")),
    )
