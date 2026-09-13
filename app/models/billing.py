from sqlalchemy import Column, DateTime, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.sql import func

from app.database import Base


class BillingAccount(Base):
    __tablename__ = "billing_accounts"

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), unique=True, index=True, nullable=False)
    billing_status = Column(String(50), nullable=False, default="current")
    billing_cycle = Column(String(50), nullable=False, default="monthly")
    balance = Column(Numeric(12, 2), nullable=False, default=0)
    next_due_date = Column(DateTime(timezone=True), nullable=True)
    last_payment_at = Column(DateTime(timezone=True), nullable=True)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
