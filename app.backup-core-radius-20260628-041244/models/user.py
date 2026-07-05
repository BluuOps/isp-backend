from sqlalchemy import Column, ForeignKey, DateTime, Integer, String
from sqlalchemy.sql import func

from app.database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(100), unique=True, index=True, nullable=False)
    password = Column(String(255), nullable=False)
    customer_id = Column(
        String(100),
        ForeignKey("customers.id", onupdate="CASCADE", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    service_plan = Column(String(100), nullable=False)
    zone = Column(String(100), nullable=True)
    status = Column(String(50), nullable=False, default="active")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
