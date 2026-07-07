from app.database import Base, engine
from app.database import SessionLocal
from app.models import RadCheck, RadReply, ServicePlan, User
from app.seed import seed_default_service_plans


def init_db() -> None:
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

    print("Database tables created successfully")


if __name__ == "__main__":
    init_db()
