from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.principal import require_organization_staff_principal
from app.core.tenant import OrganizationContext, get_organization_context
from app.database import get_db
from app.models import BillingAccount, User
from app.schemas import BillingAccountCreate, BillingAccountResponse, BillingAccountUpdate


router = APIRouter(prefix="/billing", tags=["Billing"], dependencies=[Depends(require_organization_staff_principal)])


def get_user_or_404(user_id: int, db: Session, organization_id: int) -> User:
    user = db.query(User).filter(User.id == user_id, User.organization_id == organization_id).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Subscriber not found")
    return user


def get_billing_account_or_404(user_id: int, db: Session, organization_id: int) -> BillingAccount:
    account = db.query(BillingAccount).filter(
        BillingAccount.user_id == user_id,
        BillingAccount.organization_id == organization_id,
    ).first()
    if not account:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Billing account not found")
    return account


@router.get("/accounts", response_model=List[BillingAccountResponse])
def list_billing_accounts(db: Session = Depends(get_db), organization: OrganizationContext = Depends(get_organization_context)) -> list[BillingAccount]:
    return db.query(BillingAccount).filter(BillingAccount.organization_id == organization.id).order_by(BillingAccount.id.asc()).all()


@router.post("/accounts", response_model=BillingAccountResponse, status_code=status.HTTP_201_CREATED)
def create_billing_account(payload: BillingAccountCreate, db: Session = Depends(get_db), organization: OrganizationContext = Depends(get_organization_context)) -> BillingAccount:
    get_user_or_404(payload.user_id, db, organization.id)

    existing = db.query(BillingAccount).filter(BillingAccount.user_id == payload.user_id, BillingAccount.organization_id == organization.id).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Billing account already exists",
        )

    account = BillingAccount(**payload.model_dump(), organization_id=organization.id)
    db.add(account)

    db.commit()
    db.refresh(account)
    return account


@router.get("/users/{user_id}", response_model=BillingAccountResponse)
def get_user_billing_account(user_id: int, db: Session = Depends(get_db), organization: OrganizationContext = Depends(get_organization_context)) -> BillingAccount:
    return get_billing_account_or_404(user_id, db, organization.id)


@router.put("/users/{user_id}", response_model=BillingAccountResponse)
def update_user_billing_account(
    user_id: int,
    payload: BillingAccountUpdate,
    db: Session = Depends(get_db),
    organization: OrganizationContext = Depends(get_organization_context),
) -> BillingAccount:
    account = get_billing_account_or_404(user_id, db, organization.id)

    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(account, field, value)

    db.commit()
    db.refresh(account)
    return account
