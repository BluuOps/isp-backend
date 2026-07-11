from sqlalchemy import Column, DateTime, ForeignKey, ForeignKeyConstraint, Integer, String, UniqueConstraint
from sqlalchemy.sql import func

from app.database import Base


class CustomerPortalAccount(Base):
    __tablename__ = "customer_portal_accounts"
    __table_args__ = (
        UniqueConstraint("organization_id", "customer_id", name="uq_customer_portal_org_customer"),
        UniqueConstraint("organization_id", "email", name="uq_customer_portal_org_email"),
        UniqueConstraint("organization_id", "phone", name="uq_customer_portal_org_phone"),
        ForeignKeyConstraint(
            ["organization_id", "customer_id"],
            ["customers.organization_id", "customers.id"],
            name="fk_customer_portal_same_org_customer",
            onupdate="CASCADE",
            ondelete="RESTRICT",
        ),
    )

    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    customer_id = Column(String(100), nullable=False, index=True)
    email = Column(String(150), nullable=False, index=True)
    phone = Column(String(50), nullable=True, index=True)
    password_hash = Column(String(500), nullable=False)
    status = Column(String(50), nullable=False, default="invited", index=True)
    email_verified_at = Column(DateTime(timezone=True), nullable=True)
    phone_verified_at = Column(DateTime(timezone=True), nullable=True)
    last_login_at = Column(DateTime(timezone=True), nullable=True)
    password_changed_at = Column(DateTime(timezone=True), nullable=True)
    failed_login_count = Column(Integer, nullable=False, default=0)
    locked_until = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now(), nullable=True)
