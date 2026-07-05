from sqlalchemy import Boolean, Column, DateTime, Float, Integer, String
from sqlalchemy.sql import func

from app.database import Base


class Customer(Base):
    __tablename__ = "customers"

    id = Column(String(100), primary_key=True, index=True)
    tenant_id = Column(String(100), nullable=False, index=True)
    name = Column(String(150), nullable=False, index=True)
    customer_type = Column(String(50), nullable=False, default="individual")
    email = Column(String(150), nullable=False, index=True)
    phone = Column(String(50), nullable=False)
    address = Column(String(255), nullable=False)
    latitude = Column(Float, nullable=False)
    longitude = Column(Float, nullable=False)
    mst_id = Column(String(100), nullable=True)
    splitter_port = Column(Integer, nullable=True)
    fibre_core_id = Column(String(100), nullable=True)
    onu_serial = Column(String(100), nullable=False, default="")
    olt_name = Column(String(100), nullable=False, default="")
    pon_port = Column(String(100), nullable=False, default="")
    rx_signal = Column(Float, nullable=False, default=-20)
    tx_signal = Column(Float, nullable=False, default=2)
    account_status = Column(String(50), nullable=False, default="active")
    online = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now(), nullable=True)
