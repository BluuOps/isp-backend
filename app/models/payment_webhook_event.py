from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.sql import func

from app.database import Base


class PaymentWebhookEvent(Base):
    __tablename__ = "payment_webhook_events"
    __table_args__ = (
        UniqueConstraint("provider", "deduplication_key", name="uq_payment_webhook_provider_deduplication_key"),
        Index("ix_payment_webhook_provider", "provider"),
        Index("ix_payment_webhook_provider_event_id", "provider_event_id"),
        Index("ix_payment_webhook_transaction_reference", "transaction_reference"),
        Index("ix_payment_webhook_gateway_reference", "gateway_reference"),
        Index("ix_payment_webhook_processing_status", "processing_status"),
        Index("ix_payment_webhook_next_retry_at", "next_retry_at"),
        Index("ix_payment_webhook_created_at", "created_at"),
        Index("ix_payment_webhook_organization_id", "organization_id"),
        Index("ix_payment_webhook_payment_id", "payment_id"),
    )

    id = Column(Integer, primary_key=True, index=True)
    provider = Column(String(50), nullable=False)
    event_type = Column(String(100), nullable=False)
    provider_event_id = Column(String(255), nullable=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", onupdate="CASCADE", ondelete="SET NULL"), nullable=True)
    payment_id = Column(Integer, ForeignKey("payment_transactions.id", onupdate="CASCADE", ondelete="SET NULL"), nullable=True)
    transaction_reference = Column(String(120), nullable=True)
    gateway_reference = Column(String(255), nullable=True)
    signature_valid = Column(Boolean, nullable=False, default=False)
    payload_hash = Column(String(64), nullable=False)
    deduplication_key = Column(String(255), nullable=False)
    raw_payload = Column(JSON, nullable=False)
    request_headers = Column(JSON, nullable=True)
    processing_status = Column(String(30), nullable=False, default="received", index=True)
    processing_attempts = Column(Integer, nullable=False, default=0)
    next_retry_at = Column(DateTime(timezone=True), nullable=True)
    processing_started_at = Column(DateTime(timezone=True), nullable=True)
    processed_at = Column(DateTime(timezone=True), nullable=True)
    dead_lettered_at = Column(DateTime(timezone=True), nullable=True)
    last_error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now(), nullable=True)
