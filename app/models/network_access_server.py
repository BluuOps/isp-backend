from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import INET
from sqlalchemy.sql import func

from app.database import Base


class NetworkAccessServer(Base):
    __tablename__ = "network_access_servers"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "nas_ip_address",
            name="uq_network_access_servers_org_ip",
        ),
    )

    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False, index=True)
    zone_id = Column(Integer, ForeignKey("zones.id", ondelete="RESTRICT"), nullable=False, index=True)
    nas_ip_address = Column(INET, nullable=False, index=True)
    display_name = Column(String(150), nullable=False)
    short_name = Column(String(100))
    device_type = Column(String(100))
    status = Column(String(50), nullable=False, default="active")
    description = Column(Text)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
