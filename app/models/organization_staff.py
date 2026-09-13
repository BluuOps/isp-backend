from sqlalchemy import Boolean, CheckConstraint, Column, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.sql import func

from app.database import Base


class OrganizationStaff(Base):
    __tablename__ = "organization_staff"
    __table_args__ = (
        UniqueConstraint("organization_id", "email", name="uq_staff_org_email"),
        CheckConstraint(
            "NOT is_uat_fixture OR "
            "(role = 'Read Only' AND uat_fixture_id IS NOT NULL AND uat_expires_at IS NOT NULL)",
            name="ck_organization_staff_uat_read_only_expiring",
        ),
        CheckConstraint(
            "credential_version >= 1",
            name="ck_organization_staff_credential_version",
        ),
    )

    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    name = Column(String(200), nullable=False)
    email = Column(String(255), nullable=False)
    password_hash = Column(String(500), nullable=False)
    role = Column(String(100), nullable=False)
    status = Column(String(50), nullable=False, default="active")
    is_temporary_password = Column(Boolean, nullable=False, default=True)
    is_uat_fixture = Column(Boolean, nullable=False, default=False, server_default="false", index=True)
    uat_fixture_id = Column(String(64), nullable=True, unique=True, index=True)
    uat_expires_at = Column(DateTime(timezone=True), nullable=True, index=True)
    uat_revoked_at = Column(DateTime(timezone=True), nullable=True)
    credential_version = Column(Integer, nullable=False, default=1, server_default="1")
    credentials_revoked_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
