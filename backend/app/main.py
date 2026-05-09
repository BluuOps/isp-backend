from fastapi import FastAPI

app = FastAPI()

@app.get("/")
def root():
    return {"message": "RadiusFiber Backend is Running"}


from fastapi import FastAPI, Depends
from sqlalchemy.orm import Session
from database import SessionLocal
from models import User
from passlib.context import CryptContext

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

app = FastAPI()

# Health check
@app.get("/")
def root():
    return {"message": "RadiusFiber Backend is Running"}

# DB session handler
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# Create user
@app.post("/users")
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

# Get all users
@app.get("/users")
def list_users(db: Session = Depends(get_db)):
    return db.query(User).all()
