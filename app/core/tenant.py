from dataclasses import dataclass

from fastapi import Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.authorization import (
    AuthenticatedPrincipal,
    get_authenticated_principal,
)
from app.database import get_db
from app.models.organization import Organization


@dataclass(frozen=True)
class OrganizationContext:
    id: int
    slug: str
    name: str


def get_organization_context(
    principal: AuthenticatedPrincipal = Depends(get_authenticated_principal),
    db: Session = Depends(get_db),
) -> OrganizationContext:
    if (
        principal.organization_id is None
        or principal.organization_slug is None
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Verified organization membership is required",
        )

    organization = (
        db.query(Organization)
        .filter(
            Organization.id == principal.organization_id,
            Organization.slug == principal.organization_slug,
            Organization.status == "active",
        )
        .first()
    )
    if not organization:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Organization membership is inactive",
        )
    return OrganizationContext(
        id=organization.id,
        slug=organization.slug,
        name=organization.name,
    )
