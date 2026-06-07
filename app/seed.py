from sqlalchemy.orm import Session

from app.models import ServicePlan


DEFAULT_SERVICE_PLANS = (
    {"name": "Bronze", "rate_limit": "10M/10M", "description": "Entry-level residential plan"},
    {"name": "Silver", "rate_limit": "20M/20M", "description": "Standard residential plan"},
    {"name": "Gold", "rate_limit": "50M/50M", "description": "High-speed residential plan"},
    {"name": "SME", "rate_limit": "100M/100M", "description": "Small business plan"},
    {"name": "Enterprise", "rate_limit": "200M/200M", "description": "Enterprise access plan"},
)


def seed_default_service_plans(db: Session) -> None:
    for plan in DEFAULT_SERVICE_PLANS:
        existing = db.query(ServicePlan).filter(ServicePlan.name == plan["name"]).first()
        if existing:
            continue

        db.add(
            ServicePlan(
                name=plan["name"],
                rate_limit=plan["rate_limit"],
                description=plan["description"],
                status="active",
            )
        )

