from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.models import Customer, FeatureFlag, Organization, OrganizationStaff


def enforce_limit(db: Session, organization: Organization, resource: str) -> None:
    flag = db.query(FeatureFlag).filter(
        FeatureFlag.organization_id == organization.id,
        FeatureFlag.key == "enforce_limits",
        FeatureFlag.enabled.is_(True),
    ).first()
    if not flag:
        return
    mapping = {
        "customers": (organization.customer_limit, Customer),
        "staff": (organization.staff_limit, OrganizationStaff),
    }
    limit, model = mapping[resource]
    if limit is not None and db.query(model).filter(model.organization_id == organization.id).count() >= limit:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Organization {resource} limit reached")
