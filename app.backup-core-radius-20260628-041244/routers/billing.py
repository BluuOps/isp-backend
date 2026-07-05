from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import BillingAccount, User
from app.schemas import BillingAccountCreate, BillingAccountResponse, BillingAccountUpdate

router = APIRouter(prefix="/billing", tags=["Billing"])


def get_user_or_404(user_id: int, db: Session) -> User:
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Subscriber not found")
    return user


def get_billing_account_or_404(user_id: int, db: Session) -> BillingAccount:
    account = db.query(BillingAccount).filter(BillingAccount.user_id == user_id).first()
    if not account:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Billing account not found")
    return account


@router.get("/accounts", response_model=List[BillingAccountResponse])
def list_billing_accounts(db: Session = Depends(get_db)) -> list[BillingAccount]:
    return db.query(BillingAccount).order_by(BillingAccount.id.asc()).all()


@router.post("/accounts", response_model=BillingAccountResponse, status_code=status.HTTP_201_CREATED)
def create_billing_account(payload: BillingAccountCreate, db: Session = Depends(get_db)) -> BillingAccount:
    get_user_or_404(payload.user_id, db)

    existing = db.query(BillingAccount).filter(BillingAccount.user_id == payload.user_id).first()
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Billing account already exists")

    account = BillingAccount(**payload.model_dump())
    db.add(account)
    db.commit()
    db.refresh(account)
    return account


@router.get("/users/{user_id}", response_model=BillingAccountResponse)
def get_user_billing_account(user_id: int, db: Session = Depends(get_db)) -> BillingAccount:
    return get_billing_account_or_404(user_id, db)


@router.put("/users/{user_id}", response_model=BillingAccountResponse)
def update_user_billing_account(
    user_id: int,
    payload: BillingAccountUpdate,
    db: Session = Depends(get_db),
) -> BillingAccount:
    account = get_billing_account_or_404(user_id, db)

    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(account, field, value)

    db.commit()
    db.refresh(account)
    return account
