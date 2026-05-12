from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from passlib.context import CryptContext

from app.database import SessionLocal
from app.models import User

router = APIRouter(
    prefix="/users",
    tags=["Users"]
)

pwd_context = CryptContext(
    schemes=["bcrypt"],
    deprecated="auto"
)

# Database session
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# Create user
@router.post("/")
def create_user(
    username: str,
    password: str,
    service_plan: str,
    zone: str,
    db: Session = Depends(get_db)
):
    hashed_password = pwd_context.hash(password)

    user = User(
        username=username,
        password=hashed_password,
        service_plan=service_plan,
        zone=zone,
        status="active"
    )

    db.add(user)
    db.commit()
    db.refresh(user)

    return user

# List users
@router.get("/")
def list_users(db: Session = Depends(get_db)):
    return db.query(User).all()
