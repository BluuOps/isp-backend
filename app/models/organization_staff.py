from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.sql import func

from app.database import Base


class OrganizationStaff(Base):
    __tablename__ = "organization_staff"
    __table_args__ = (UniqueConstraint("organization_id", "email", name="uq_staff_org_email"),)

    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    name = Column(String(200), nullable=False)
    email = Column(String(255), nullable=False)
    password_hash = Column(String(500), nullable=False)
    role = Column(String(100), nullable=False)
    status = Column(String(50), nullable=False, default="active")
    is_temporary_password = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
