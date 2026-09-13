from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.sql import func

from app.database import Base


class OrganizationAdminInvitation(Base):
    __tablename__ = "organization_admin_invitations"
    __table_args__ = (
        CheckConstraint(
            "purpose IN ('bootstrap', 'invite', 'recovery')",
            name="ck_admin_invitation_purpose",
        ),
        CheckConstraint(
            "max_attempts >= 1 AND max_attempts <= 10 AND "
            "attempt_count >= 0 AND attempt_count <= max_attempts",
            name="ck_admin_invitation_attempts",
        ),
        CheckConstraint(
            "NOT (used_at IS NOT NULL AND revoked_at IS NOT NULL)",
            name="ck_admin_invitation_terminal_state",
        ),
        Index(
            "ix_admin_invitation_org_active",
            "organization_id",
            "purpose",
            "used_at",
            "revoked_at",
            "expires_at",
        ),
        Index(
            "ix_admin_invitation_org_email",
            "organization_id",
            "email",
            "purpose",
        ),
    )

    id = Column(Integer, primary_key=True)
    organization_id = Column(
        Integer,
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    email = Column(String(255), nullable=False)
    token_hash = Column(String(64), nullable=False, unique=True, index=True)
    purpose = Column(String(20), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)
    used_at = Column(DateTime(timezone=True), nullable=True)
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    attempt_count = Column(Integer, nullable=False, default=0, server_default="0")
    max_attempts = Column(Integer, nullable=False, default=5, server_default="5")
    created_by_principal_type = Column(String(32), nullable=False)
    created_by_principal_id = Column(String(255), nullable=False)
    reason = Column(String(500), nullable=False)
    correlation_id = Column(String(64), nullable=False, unique=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=True, onupdate=func.now())


class OrganizationAdminInvitationRateLimit(Base):
    __tablename__ = "organization_admin_invitation_rate_limits"
    __table_args__ = (
        UniqueConstraint("scope", "key_hash", "window_started_at", name="uq_admin_invitation_rate_bucket"),
        CheckConstraint("attempt_count >= 1", name="ck_admin_invitation_rate_attempts"),
        Index("ix_admin_invitation_rate_expiry", "expires_at", "id"),
    )

    id = Column(Integer, primary_key=True)
    scope = Column(String(32), nullable=False)
    key_hash = Column(String(64), nullable=False)
    window_started_at = Column(DateTime(timezone=True), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    attempt_count = Column(Integer, nullable=False, default=1, server_default="1")
