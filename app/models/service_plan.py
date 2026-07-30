from sqlalchemy import BigInteger, Boolean, Column, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.sql import func

from app.database import Base


class ServicePlan(Base):
    __tablename__ = "service_plans"
    __table_args__ = (
        UniqueConstraint("organization_id", "name", name="uq_service_plan_org_name"),
        Index("ix_service_plans_customer_catalog", "organization_id", "status", "customer_visible"),
    )

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=True, index=True)
    name = Column(String(100), index=True, nullable=False)
    rate_limit = Column(String(100), nullable=False)
    price = Column(String(50), nullable=True)
    price_minor = Column(BigInteger, nullable=True)
    currency = Column(String(3), nullable=False, default="NGN")
    duration_days = Column(Integer, nullable=False, default=30)
    billing_interval = Column(String(30), nullable=False, default="monthly")
    customer_visible = Column(Boolean, nullable=False, default=False)
    description = Column(String(255), nullable=True)
    status = Column(String(50), nullable=False, default="active")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
