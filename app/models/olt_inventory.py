from sqlalchemy import BigInteger, CheckConstraint, Column, DateTime, ForeignKeyConstraint, Index, Integer, String, UniqueConstraint
from sqlalchemy.sql import func

from app.database import Base


DEVICE_PARENT_FK = lambda: ForeignKeyConstraint(
    ["olt_device_id", "organization_id"],
    ["olt_devices.id", "olt_devices.organization_id"],
    ondelete="CASCADE",
)


class OltCard(Base):
    __tablename__ = "olt_cards"
    __table_args__ = (
        UniqueConstraint("id", "organization_id", name="uq_olt_cards_id_org"),
        UniqueConstraint("olt_device_id", "vendor_key", name="uq_olt_cards_device_vendor_key"),
        DEVICE_PARENT_FK(),
    )

    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, nullable=False, index=True)
    olt_device_id = Column(Integer, nullable=False, index=True)
    vendor_key = Column(String(150), nullable=False)
    slot = Column(String(50), nullable=False)
    card_type = Column(String(100))
    administrative_status = Column(String(32))
    operational_status = Column(String(32))
    serial_number = Column(String(150))
    hardware_version = Column(String(100))
    software_version = Column(String(150))
    observed_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())


class OltUplink(Base):
    __tablename__ = "olt_uplinks"
    __table_args__ = (
        UniqueConstraint("id", "organization_id", name="uq_olt_uplinks_id_org"),
        UniqueConstraint("olt_device_id", "vendor_key", name="uq_olt_uplinks_device_vendor_key"),
        DEVICE_PARENT_FK(),
    )

    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, nullable=False, index=True)
    olt_device_id = Column(Integer, nullable=False, index=True)
    vendor_key = Column(String(150), nullable=False)
    name = Column(String(100), nullable=False)
    administrative_status = Column(String(32))
    operational_status = Column(String(32))
    speed_bps = Column(BigInteger)
    observed_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())


class OltPonPort(Base):
    __tablename__ = "olt_pon_ports"
    __table_args__ = (
        UniqueConstraint("id", "organization_id", name="uq_olt_pon_ports_id_org"),
        UniqueConstraint("olt_device_id", "vendor_key", name="uq_olt_pon_ports_device_vendor_key"),
        DEVICE_PARENT_FK(),
    )

    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, nullable=False, index=True)
    olt_device_id = Column(Integer, nullable=False, index=True)
    vendor_key = Column(String(150), nullable=False)
    slot = Column(String(50), nullable=False)
    port = Column(String(50), nullable=False)
    pon_type = Column(String(50))
    administrative_status = Column(String(32))
    operational_status = Column(String(32))
    observed_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())


class OltOnu(Base):
    __tablename__ = "olt_onus"
    __table_args__ = (
        UniqueConstraint("id", "organization_id", name="uq_olt_onus_id_org"),
        UniqueConstraint("olt_device_id", "vendor_key", name="uq_olt_onus_device_vendor_key"),
        CheckConstraint("inventory_status IN ('observed', 'missing', 'unavailable')", name="ck_olt_onus_inventory_status"),
        Index("ix_olt_onus_org_serial", "organization_id", "serial_number"),
        DEVICE_PARENT_FK(),
        ForeignKeyConstraint(
            ["pon_port_id", "organization_id"],
            ["olt_pon_ports.id", "olt_pon_ports.organization_id"],
            ondelete="CASCADE",
        ),
    )

    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, nullable=False, index=True)
    olt_device_id = Column(Integer, nullable=False, index=True)
    pon_port_id = Column(Integer, nullable=False, index=True)
    vendor_key = Column(String(150), nullable=False)
    serial_number = Column(String(150))
    onu_identifier = Column(String(100))
    model = Column(String(100))
    administrative_status = Column(String(32))
    operational_status = Column(String(32))
    inventory_status = Column(String(32), nullable=False, default="observed", server_default="observed")
    observed_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
