from sqlalchemy import Boolean, CheckConstraint, Column, DateTime, ForeignKeyConstraint, Index, Integer, String, UniqueConstraint, true
from sqlalchemy.dialects.postgresql import INET
from sqlalchemy.sql import func

from app.database import Base


class OltCredentialReference(Base):
    __tablename__ = "olt_credential_references"
    __table_args__ = (
        UniqueConstraint("id", "organization_id", name="uq_olt_credential_references_id_org"),
        UniqueConstraint("organization_id", "reference_name", name="uq_olt_credential_references_org_name"),
        CheckConstraint("provider IN ('environment_file', 'vault_reference')", name="ck_olt_credential_references_provider"),
        CheckConstraint("validation_status IN ('not_validated', 'valid', 'invalid', 'unavailable')", name="ck_olt_credential_references_validation_status"),
        ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="RESTRICT"),
    )

    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, nullable=False, index=True)
    reference_name = Column(String(150), nullable=False)
    provider = Column(String(32), nullable=False)
    external_secret_id = Column(String(255), nullable=False)
    enabled = Column(Boolean, nullable=False, default=True, server_default=true())
    validation_status = Column(String(32), nullable=False, default="not_validated", server_default="not_validated")
    last_validated_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

class OltDevice(Base):
    __tablename__ = "olt_devices"
    __table_args__ = (
        UniqueConstraint("id", "organization_id", name="uq_olt_devices_id_org"),
        UniqueConstraint("organization_id", "name", name="uq_olt_devices_org_name"),
        UniqueConstraint("organization_id", "management_address", name="uq_olt_devices_org_address"),
        CheckConstraint("adapter_key IN ('null')", name="ck_olt_devices_adapter_key"),
        CheckConstraint("transport IN ('disabled')", name="ck_olt_devices_transport"),
        CheckConstraint("status IN ('active', 'disabled')", name="ck_olt_devices_status"),
        CheckConstraint("cache_status IN ('empty', 'fresh', 'stale', 'unavailable')", name="ck_olt_devices_cache_status"),
        Index("ix_olt_devices_org_status", "organization_id", "status"),
        ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["credential_reference_id", "organization_id"],
            ["olt_credential_references.id", "olt_credential_references.organization_id"],
            ondelete="RESTRICT",
        ),
    )

    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, nullable=False, index=True)
    credential_reference_id = Column(Integer, nullable=True)
    name = Column(String(150), nullable=False)
    management_address = Column(INET, nullable=False)
    adapter_key = Column(String(50), nullable=False, default="null", server_default="null")
    transport = Column(String(32), nullable=False, default="disabled", server_default="disabled")
    status = Column(String(32), nullable=False, default="disabled", server_default="disabled")
    vendor = Column(String(100))
    model = Column(String(100))
    hardware_version = Column(String(100))
    software_version = Column(String(150))
    serial_number = Column(String(150))
    cache_status = Column(String(32), nullable=False, default="empty", server_default="empty")
    observed_at = Column(DateTime(timezone=True))
    last_poll_attempt_at = Column(DateTime(timezone=True))
    last_poll_success_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
