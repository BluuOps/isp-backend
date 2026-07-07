from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models import Organization, Subscription


def find_expired_subscriptions(db: Session, now: datetime | None = None) -> list[Subscription]:
    current = now or datetime.now(timezone.utc)
    return db.query(Subscription).filter(
        Subscription.expires_at.is_not(None),
        Subscription.expires_at <= current,
        Subscription.status.in_(["trial", "active"]),
    ).all()


def find_organizations_to_suspend(db: Session) -> list[Organization]:
    return db.query(Organization).filter(Organization.subscription_status == "expired").all()


def enqueue_notification(*_args, **_kwargs) -> None:
    """Scheduling/queue integration intentionally deferred."""
