from sqlalchemy import Boolean, Column, DateTime, Integer, String
from sqlalchemy.sql import func

from app.database import Base


class Platform(Base):
    __tablename__ = "platform"

    id = Column(Integer, primary_key=True)
    name = Column(String(150), nullable=False)
    version = Column(String(50), nullable=False, default="1.0")
    support_email = Column(String(255))
    support_phone = Column(String(50))
    default_currency = Column(String(3), nullable=False, default="NGN")
    default_timezone = Column(String(100), nullable=False, default="Africa/Lagos")
    maintenance_mode = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
