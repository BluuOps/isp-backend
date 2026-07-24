from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, JSON, String
from sqlalchemy.sql import func

from app.database import Base


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=True, index=True)
    actor_type = Column(String(50), nullable=False, default="system", index=True)
    actor_id = Column(String(255), index=True)
    actor_label = Column(String(255))
    actor = Column(String(255), nullable=False, default="system")
    action = Column(String(100), nullable=False, index=True)
    target_type = Column(String(100), nullable=False)
    target_id = Column(String(255))
    old_value = Column(JSON)
    new_value = Column(JSON)
    success = Column(Boolean, nullable=False, default=True)
    ip_address = Column(String(45))
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)
