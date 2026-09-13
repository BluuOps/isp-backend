from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.sql import func

from app.database import Base


class Organization(Base):
    __tablename__ = "organizations"
    __table_args__ = (
        UniqueConstraint("slug", name="organizations_slug_key"),
        Index("ix_organizations_slug", "slug", unique=True),
    )

    id = Column(Integer, primary_key=True)
    platform_id = Column(Integer, ForeignKey("platform.id"), nullable=False)
    name = Column(String(200), nullable=False)
    slug = Column(String(100), nullable=False)
    status = Column(String(50), nullable=False, default="active")
    company_email = Column(String(255))
    company_phone = Column(String(50))
    website = Column(String(255))
    country = Column(String(2), nullable=False, default="NG")
    timezone = Column(String(100), nullable=False, default="Africa/Lagos")
    currency = Column(String(3), nullable=False, default="NGN")
    logo = Column(String(500))
    subscription_plan = Column(String(100))
    subscription_status = Column(String(50), nullable=False, default="active")
    subscription_started_at = Column(DateTime(timezone=True))
    subscription_expires_at = Column(DateTime(timezone=True))
    trial_expires_at = Column(DateTime(timezone=True))
    customer_limit = Column(Integer)
    staff_limit = Column(Integer)
    nas_limit = Column(Integer)
    olt_limit = Column(Integer)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
