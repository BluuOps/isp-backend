from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer
from sqlalchemy.sql import func

from app.database import Base


class NotificationSetting(Base):
    __tablename__ = "notification_settings"

    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, unique=True)
    email_enabled = Column(Boolean, nullable=False, default=False)
    sms_enabled = Column(Boolean, nullable=False, default=False)
    expiration_alerts = Column(Boolean, nullable=False, default=True)
    payment_alerts = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
