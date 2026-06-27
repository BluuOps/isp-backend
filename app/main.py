from fastapi import FastAPI

from app.database import Base, SessionLocal, engine
from app.routers import billing, customers, plans, users
from app.seed import seed_default_service_plans


app = FastAPI(
    title="ISP OSS/BSS API",
    description="Backend API for ISP subscriber and RADIUS provisioning workflows.",
    version="0.1.0",
)


app.include_router(users.router)
app.include_router(plans.router)
app.include_router(billing.router)
app.include_router(customers.router)


@app.on_event("startup")
def startup() -> None:
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        seed_default_service_plans(db)
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
