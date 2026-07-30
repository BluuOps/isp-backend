from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, JSON, Numeric, String, Text, UniqueConstraint, text
from sqlalchemy.sql import func

from app.database import Base


class PaymentTransaction(Base):
    __tablename__ = "payment_transactions"
    __table_args__ = (
        UniqueConstraint("organization_id", "transaction_reference", name="uq_payment_org_transaction_reference"),
        UniqueConstraint("organization_id", "idempotency_key", name="uq_payment_org_idempotency_key"),
        UniqueConstraint("quote_reference", name="uq_payment_quote_reference"),
        Index("ix_payment_org_gateway_reference", "organization_id", "gateway", "gateway_reference"),
        Index("ix_payment_org_status", "organization_id", "payment_status"),
        Index("ix_payment_transactions_selected_plan_id", "selected_plan_id"),
        Index("ix_payment_transactions_fulfillment_status", "fulfillment_status"),
        Index(
            "uq_payment_gateway_reference",
            "gateway",
            "gateway_reference",
            unique=True,
            postgresql_where=text("gateway_reference IS NOT NULL"),
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    customer_id = Column(String(100), ForeignKey("customers.id", onupdate="CASCADE", ondelete="RESTRICT"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id", onupdate="CASCADE", ondelete="SET NULL"), nullable=True, index=True)
    transaction_reference = Column(String(120), nullable=False, index=True)
    external_reference = Column(String(255), nullable=True, index=True)
    amount = Column(Numeric(12, 2), nullable=False)
    currency = Column(String(3), nullable=False, default="NGN")
    payment_method = Column(String(50), nullable=False)
    payment_status = Column(String(30), nullable=False, default="pending", index=True)
    payment_purpose = Column(String(50), nullable=False, default="manual_payment", index=True)
    gateway = Column(String(50), nullable=True, index=True)
    gateway_reference = Column(String(255), nullable=True, index=True)
    authorization_url = Column(Text, nullable=True)
    access_code = Column(String(255), nullable=True)
    idempotency_key = Column(String(120), nullable=True, index=True)
    initiated_at = Column(DateTime(timezone=True), nullable=True, index=True)
    verified_at = Column(DateTime(timezone=True), nullable=True, index=True)
    failed_at = Column(DateTime(timezone=True), nullable=True)
    abandoned_at = Column(DateTime(timezone=True), nullable=True)
    reversed_at = Column(DateTime(timezone=True), nullable=True)
    raw_gateway_status = Column(String(100), nullable=True)
    gateway_metadata = Column(JSON, nullable=True)
    renewal_processed_at = Column(DateTime(timezone=True), nullable=True, index=True)
    renewal_cycles = Column(Integer, nullable=False, default=1)
    selected_plan_id = Column(Integer, ForeignKey("service_plans.id", ondelete="SET NULL"), nullable=True)
    billing_periods = Column(Integer, nullable=False, default=1)
    quote_reference = Column(String(120), nullable=True)
    fulfillment_status = Column(String(40), nullable=False, default="not_applicable")
    previous_plan_name = Column(String(100), nullable=True)
    resulting_plan_name = Column(String(100), nullable=True)
    expected_amount = Column(Numeric(12, 2), nullable=True)
    expected_currency = Column(String(3), nullable=True)
    old_expiration_date = Column(DateTime(timezone=True), nullable=True)
    new_expiration_date = Column(DateTime(timezone=True), nullable=True)
    paid_at = Column(DateTime(timezone=True), nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now(), nullable=True)
    created_by_staff_id = Column(Integer, ForeignKey("organization_staff.id", onupdate="CASCADE", ondelete="SET NULL"), nullable=True, index=True)
    created_by_customer_id = Column(String(100), ForeignKey("customers.id", onupdate="CASCADE", ondelete="SET NULL"), nullable=True, index=True)
    created_by_principal_type = Column(String(50), nullable=True, index=True)
    recorded_by_label = Column(String(255), nullable=True)
    created_by = Column(String(255), nullable=True)
    notes = Column(Text, nullable=True)
