from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    Index,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.sql import func

from app.database import Base


class ExpiryScanRun(Base):
    __tablename__ = "expiry_scan_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running','completed','completed_with_errors','failed')",
            name="ck_expiry_scan_run_status",
        ),
        UniqueConstraint("correlation_id", name="uq_expiry_scan_run_correlation"),
        Index("ix_expiry_scan_runs_completed_at", "completed_at"),
    )

    id = Column(BigInteger, primary_key=True)
    correlation_id = Column(String(120), nullable=False, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="SET NULL"), index=True)
    component = Column(String(100), nullable=False, default="expiry-scan-worker")
    status = Column(String(40), nullable=False)
    dry_run = Column(Boolean, nullable=False, default=True)
    evaluated_count = Column(Integer, nullable=False, default=0)
    newly_expired_count = Column(Integer, nullable=False, default=0)
    active_session_count = Column(Integer, nullable=False, default=0)
    jobs_queued_count = Column(Integer, nullable=False, default=0)
    error_count = Column(Integer, nullable=False, default=0)
    started_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    completed_at = Column(DateTime(timezone=True))
    duration_ms = Column(Integer)
    last_error = Column(Text)


class RadiusRejectOwnership(Base):
    __tablename__ = "radius_reject_ownerships"
    __table_args__ = (
        ForeignKeyConstraint(
            ["user_id", "organization_id"],
            ["users.id", "users.organization_id"],
            name="fk_radius_reject_ownership_user_tenant",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "owner = 'radiusfiber_access_policy'",
            name="ck_radius_reject_ownership_owner",
        ),
        UniqueConstraint(
            "organization_id",
            "user_id",
            "reason_code",
            name="uq_radius_reject_ownership_user_tenant_reason",
        ),
        UniqueConstraint("radcheck_id", name="uq_radius_reject_ownership_radcheck"),
    )

    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    organization_id = Column(Integer, nullable=False, index=True)
    user_id = Column(Integer, nullable=False, index=True)
    radcheck_id = Column(
        Integer,
        ForeignKey("radcheck.id", ondelete="CASCADE"),
        nullable=False,
    )
    username = Column(String(100), nullable=False)
    owner = Column(String(64), nullable=False, default="radiusfiber_access_policy")
    reason_code = Column(String(50), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class ExpiryDisconnectJob(Base):
    __tablename__ = "expiry_disconnect_jobs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["user_id", "organization_id"],
            ["users.id", "users.organization_id"],
            name="fk_expiry_disconnect_job_user_tenant",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["nas_id", "organization_id"],
            ["network_access_servers.id", "network_access_servers.organization_id"],
            name="fk_expiry_disconnect_job_nas_tenant",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "status IN ('pending','processing','succeeded','retryable_failure','terminal_failure','cancelled','stale')",
            name="ck_expiry_disconnect_job_status",
        ),
        CheckConstraint(
            "attempt_count >= 0 AND max_attempts BETWEEN 1 AND 10",
            name="ck_expiry_disconnect_attempts",
        ),
        UniqueConstraint("correlation_id", name="uq_expiry_disconnect_job_correlation"),
        UniqueConstraint(
            "organization_id",
            "user_id",
            "session_identity",
            "requested_expiration_at",
            name="uq_expiry_disconnect_event_session",
        ),
        Index(
            "ix_expiry_disconnect_jobs_due",
            "next_attempt_at",
            "id",
            postgresql_where=text("status IN ('pending','retryable_failure')"),
        ),
    )

    id = Column(BigInteger, primary_key=True)
    organization_id = Column(Integer, nullable=False, index=True)
    customer_id = Column(String(100), nullable=False, index=True)
    user_id = Column(Integer, nullable=False, index=True)
    session_radacct_id = Column(BigInteger, nullable=False)
    session_identity = Column(String(255), nullable=False)
    nas_id = Column(Integer)
    reason_code = Column(String(50), nullable=False)
    requested_expiration_at = Column(DateTime(timezone=True), nullable=False)
    status = Column(String(40), nullable=False, default="pending", index=True)
    attempt_count = Column(Integer, nullable=False, default=0)
    max_attempts = Column(Integer, nullable=False, default=3)
    next_attempt_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    correlation_id = Column(String(120), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())
    processing_started_at = Column(DateTime(timezone=True))
    completed_at = Column(DateTime(timezone=True))
    last_error = Column(Text)
