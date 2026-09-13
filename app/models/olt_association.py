from sqlalchemy import CheckConstraint, Column, DateTime, ForeignKeyConstraint, Index, Integer, String, UniqueConstraint, text
from sqlalchemy.sql import func

from app.database import Base


class OltServiceAssociation(Base):
    __tablename__ = "olt_service_associations"
    __table_args__ = (
        UniqueConstraint("id", "organization_id", name="uq_olt_service_associations_id_org"),
        CheckConstraint("status IN ('active', 'inactive')", name="ck_olt_service_associations_status"),
        CheckConstraint("match_source IN ('manual', 'imported', 'verified')", name="ck_olt_service_associations_match_source"),
        CheckConstraint("confidence >= 0 AND confidence <= 100", name="ck_olt_service_associations_confidence"),
        ForeignKeyConstraint(
            ["onu_id", "organization_id"],
            ["olt_onus.id", "olt_onus.organization_id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["customer_id", "organization_id"],
            ["customers.id", "customers.organization_id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["user_id", "organization_id"],
            ["users.id", "users.organization_id"],
            ondelete="RESTRICT",
        ),
        Index(
            "uq_olt_service_associations_active_onu",
            "onu_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
        Index(
            "uq_olt_service_associations_active_user",
            "user_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
        Index("ix_olt_service_associations_org_customer", "organization_id", "customer_id"),
    )

    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, nullable=False, index=True)
    onu_id = Column(Integer, nullable=False, index=True)
    customer_id = Column(String(100), nullable=False, index=True)
    user_id = Column(Integer, nullable=False, index=True)
    status = Column(String(32), nullable=False, default="active", server_default="active")
    match_source = Column(String(32), nullable=False, default="manual", server_default="manual")
    confidence = Column(Integer, nullable=False, default=100, server_default="100")
    verified_by = Column(String(255))
    verified_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
