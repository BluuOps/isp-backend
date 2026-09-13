from sqlalchemy import CheckConstraint, Column, DateTime, ForeignKeyConstraint, Index, Integer, String, UniqueConstraint
from sqlalchemy.sql import func

from app.database import Base


class OltPollRun(Base):
    __tablename__ = "olt_poll_runs"
    __table_args__ = (
        UniqueConstraint("id", "organization_id", name="uq_olt_poll_runs_id_org"),
        CheckConstraint("status IN ('requested', 'running', 'succeeded', 'partial', 'failed', 'disabled')", name="ck_olt_poll_runs_status"),
        CheckConstraint("trigger IN ('manual', 'test_fixture')", name="ck_olt_poll_runs_trigger"),
        CheckConstraint("completed_at IS NULL OR started_at IS NULL OR completed_at >= started_at", name="ck_olt_poll_runs_timestamp_order"),
        Index("ix_olt_poll_runs_org_created", "organization_id", "created_at"),
        ForeignKeyConstraint(
            ["olt_device_id", "organization_id"],
            ["olt_devices.id", "olt_devices.organization_id"],
            ondelete="CASCADE",
        ),
    )

    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, nullable=False, index=True)
    olt_device_id = Column(Integer, nullable=False, index=True)
    correlation_id = Column(String(120), nullable=False, unique=True)
    trigger = Column(String(32), nullable=False, default="manual", server_default="manual")
    status = Column(String(32), nullable=False, default="disabled", server_default="disabled")
    error_code = Column(String(100))
    started_at = Column(DateTime(timezone=True))
    completed_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
