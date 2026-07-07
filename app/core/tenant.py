from dataclasses import dataclass

from fastapi import Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.config import settings
from app.database import get_db
from app.models.organization import Organization


@dataclass(frozen=True)
class OrganizationContext:
    id: int
    slug: str
    name: str


def get_organization_context(db: Session = Depends(get_db)) -> OrganizationContext:
    organization = (
        db.query(Organization)
        .filter(
            Organization.slug == settings.default_organization_slug,
            Organization.status == "active",
        )
        .first()
    )
    if not organization:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Default organization is not configured",
        )
    return OrganizationContext(
        id=organization.id,
        slug=organization.slug,
        name=organization.name,
    )
