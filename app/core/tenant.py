from dataclasses import dataclass

from fastapi import Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.authorization import (
    AuthenticatedPrincipal,
    PrincipalType,
    get_authenticated_principal,
)
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
    principal: AuthenticatedPrincipal = Depends(get_authenticated_principal),
) -> OrganizationContext:
    if (
        principal.principal_type != PrincipalType.ORGANIZATION_STAFF
        or principal.organization_id is None
        or not principal.organization_slug
    ):
        raise _tenant_error(
            status.HTTP_403_FORBIDDEN,
            "wrong_principal_type",
            "Organization staff access is required.",
        )

    organization = db.query(Organization).filter(Organization.id == principal.organization_id).first()
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
    if organization.slug != principal.organization_slug:
        raise _tenant_error(
            status.HTTP_401_UNAUTHORIZED,
            "stale_organization_token",
            "Organization token does not match the current organization.",
        )

    return OrganizationContext(
        id=organization.id,
        slug=organization.slug,
        name=organization.name,
        principal_id=principal.subject_id,
        roles=(principal.organization_role,) if principal.organization_role else (),
        permissions=tuple(sorted(principal.effective_permissions)),
    )
