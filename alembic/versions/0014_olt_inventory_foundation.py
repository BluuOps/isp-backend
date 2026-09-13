"""Add tenant-scoped vendor-neutral OLT inventory foundation."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0014_olt_inventory_foundation"
down_revision = "0013_auth_token_revocations"
branch_labels = None
depends_on = None


def _timestamps() -> tuple[sa.Column, sa.Column]:
    return (
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )


def upgrade() -> None:
    op.create_table(
        "olt_credential_references",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("reference_name", sa.String(length=150), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("external_secret_id", sa.String(length=255), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("validation_status", sa.String(length=32), nullable=False, server_default="not_validated"),
        sa.Column("last_validated_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("id", "organization_id", name="uq_olt_credential_references_id_org"),
        sa.UniqueConstraint("organization_id", "reference_name", name="uq_olt_credential_references_org_name"),
        sa.CheckConstraint("provider IN ('environment_file', 'vault_reference')", name="ck_olt_credential_references_provider"),
        sa.CheckConstraint("validation_status IN ('not_validated', 'valid', 'invalid', 'unavailable')", name="ck_olt_credential_references_validation_status"),
    )
    op.create_index("ix_olt_credential_references_organization_id", "olt_credential_references", ["organization_id"])

    op.create_table(
        "olt_devices",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("credential_reference_id", sa.Integer(), nullable=True),
        sa.Column("name", sa.String(length=150), nullable=False),
        sa.Column("management_address", postgresql.INET(), nullable=False),
        sa.Column("adapter_key", sa.String(length=50), nullable=False, server_default="null"),
        sa.Column("transport", sa.String(length=32), nullable=False, server_default="disabled"),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="disabled"),
        sa.Column("vendor", sa.String(length=100), nullable=True),
        sa.Column("model", sa.String(length=100), nullable=True),
        sa.Column("hardware_version", sa.String(length=100), nullable=True),
        sa.Column("software_version", sa.String(length=150), nullable=True),
        sa.Column("serial_number", sa.String(length=150), nullable=True),
        sa.Column("cache_status", sa.String(length=32), nullable=False, server_default="empty"),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_poll_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_poll_success_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["credential_reference_id", "organization_id"],
            ["olt_credential_references.id", "olt_credential_references.organization_id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("id", "organization_id", name="uq_olt_devices_id_org"),
        sa.UniqueConstraint("organization_id", "name", name="uq_olt_devices_org_name"),
        sa.UniqueConstraint("organization_id", "management_address", name="uq_olt_devices_org_address"),
        sa.CheckConstraint("adapter_key IN ('null')", name="ck_olt_devices_adapter_key"),
        sa.CheckConstraint("transport IN ('disabled')", name="ck_olt_devices_transport"),
        sa.CheckConstraint("status IN ('active', 'disabled')", name="ck_olt_devices_status"),
        sa.CheckConstraint("cache_status IN ('empty', 'fresh', 'stale', 'unavailable')", name="ck_olt_devices_cache_status"),
    )
    op.create_index("ix_olt_devices_organization_id", "olt_devices", ["organization_id"])
    op.create_index("ix_olt_devices_org_status", "olt_devices", ["organization_id", "status"])

    op.create_table(
        "olt_cards",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("olt_device_id", sa.Integer(), nullable=False),
        sa.Column("vendor_key", sa.String(length=150), nullable=False),
        sa.Column("slot", sa.String(length=50), nullable=False),
        sa.Column("card_type", sa.String(length=100)),
        sa.Column("administrative_status", sa.String(length=32)),
        sa.Column("operational_status", sa.String(length=32)),
        sa.Column("serial_number", sa.String(length=150)),
        sa.Column("hardware_version", sa.String(length=100)),
        sa.Column("software_version", sa.String(length=150)),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["olt_device_id", "organization_id"], ["olt_devices.id", "olt_devices.organization_id"], ondelete="CASCADE"),
        sa.UniqueConstraint("id", "organization_id", name="uq_olt_cards_id_org"),
        sa.UniqueConstraint("olt_device_id", "vendor_key", name="uq_olt_cards_device_vendor_key"),
    )
    op.create_index("ix_olt_cards_organization_id", "olt_cards", ["organization_id"])
    op.create_index("ix_olt_cards_olt_device_id", "olt_cards", ["olt_device_id"])

    op.create_table(
        "olt_uplinks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("olt_device_id", sa.Integer(), nullable=False),
        sa.Column("vendor_key", sa.String(length=150), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("administrative_status", sa.String(length=32)),
        sa.Column("operational_status", sa.String(length=32)),
        sa.Column("speed_bps", sa.BigInteger()),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["olt_device_id", "organization_id"], ["olt_devices.id", "olt_devices.organization_id"], ondelete="CASCADE"),
        sa.UniqueConstraint("id", "organization_id", name="uq_olt_uplinks_id_org"),
        sa.UniqueConstraint("olt_device_id", "vendor_key", name="uq_olt_uplinks_device_vendor_key"),
    )
    op.create_index("ix_olt_uplinks_organization_id", "olt_uplinks", ["organization_id"])
    op.create_index("ix_olt_uplinks_olt_device_id", "olt_uplinks", ["olt_device_id"])

    op.create_table(
        "olt_pon_ports",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("olt_device_id", sa.Integer(), nullable=False),
        sa.Column("vendor_key", sa.String(length=150), nullable=False),
        sa.Column("slot", sa.String(length=50), nullable=False),
        sa.Column("port", sa.String(length=50), nullable=False),
        sa.Column("pon_type", sa.String(length=50)),
        sa.Column("administrative_status", sa.String(length=32)),
        sa.Column("operational_status", sa.String(length=32)),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["olt_device_id", "organization_id"], ["olt_devices.id", "olt_devices.organization_id"], ondelete="CASCADE"),
        sa.UniqueConstraint("id", "organization_id", name="uq_olt_pon_ports_id_org"),
        sa.UniqueConstraint("olt_device_id", "vendor_key", name="uq_olt_pon_ports_device_vendor_key"),
    )
    op.create_index("ix_olt_pon_ports_organization_id", "olt_pon_ports", ["organization_id"])
    op.create_index("ix_olt_pon_ports_olt_device_id", "olt_pon_ports", ["olt_device_id"])

    op.create_table(
        "olt_onus",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("olt_device_id", sa.Integer(), nullable=False),
        sa.Column("pon_port_id", sa.Integer(), nullable=False),
        sa.Column("vendor_key", sa.String(length=150), nullable=False),
        sa.Column("serial_number", sa.String(length=150)),
        sa.Column("onu_identifier", sa.String(length=100)),
        sa.Column("model", sa.String(length=100)),
        sa.Column("administrative_status", sa.String(length=32)),
        sa.Column("operational_status", sa.String(length=32)),
        sa.Column("inventory_status", sa.String(length=32), nullable=False, server_default="observed"),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["olt_device_id", "organization_id"], ["olt_devices.id", "olt_devices.organization_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["pon_port_id", "organization_id"], ["olt_pon_ports.id", "olt_pon_ports.organization_id"], ondelete="CASCADE"),
        sa.UniqueConstraint("id", "organization_id", name="uq_olt_onus_id_org"),
        sa.UniqueConstraint("olt_device_id", "vendor_key", name="uq_olt_onus_device_vendor_key"),
        sa.CheckConstraint("inventory_status IN ('observed', 'missing', 'unavailable')", name="ck_olt_onus_inventory_status"),
    )
    op.create_index("ix_olt_onus_organization_id", "olt_onus", ["organization_id"])
    op.create_index("ix_olt_onus_olt_device_id", "olt_onus", ["olt_device_id"])
    op.create_index("ix_olt_onus_pon_port_id", "olt_onus", ["pon_port_id"])
    op.create_index("ix_olt_onus_org_serial", "olt_onus", ["organization_id", "serial_number"])

    op.create_table(
        "olt_service_associations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("onu_id", sa.Integer(), nullable=False),
        sa.Column("customer_id", sa.String(length=100), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
        sa.Column("match_source", sa.String(length=32), nullable=False, server_default="manual"),
        sa.Column("confidence", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("verified_by", sa.String(length=255)),
        sa.Column("verified_at", sa.DateTime(timezone=True)),
        *_timestamps(),
        sa.ForeignKeyConstraint(["onu_id", "organization_id"], ["olt_onus.id", "olt_onus.organization_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["customer_id", "organization_id"], ["customers.id", "customers.organization_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["user_id", "organization_id"], ["users.id", "users.organization_id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("id", "organization_id", name="uq_olt_service_associations_id_org"),
        sa.CheckConstraint("status IN ('active', 'inactive')", name="ck_olt_service_associations_status"),
        sa.CheckConstraint("match_source IN ('manual', 'imported', 'verified')", name="ck_olt_service_associations_match_source"),
        sa.CheckConstraint("confidence >= 0 AND confidence <= 100", name="ck_olt_service_associations_confidence"),
    )
    op.create_index("ix_olt_service_associations_organization_id", "olt_service_associations", ["organization_id"])
    op.create_index("ix_olt_service_associations_onu_id", "olt_service_associations", ["onu_id"])
    op.create_index("ix_olt_service_associations_customer_id", "olt_service_associations", ["customer_id"])
    op.create_index("ix_olt_service_associations_user_id", "olt_service_associations", ["user_id"])
    op.create_index("ix_olt_service_associations_org_customer", "olt_service_associations", ["organization_id", "customer_id"])
    op.create_index("uq_olt_service_associations_active_onu", "olt_service_associations", ["onu_id"], unique=True, postgresql_where=sa.text("status = 'active'"))
    op.create_index("uq_olt_service_associations_active_user", "olt_service_associations", ["user_id"], unique=True, postgresql_where=sa.text("status = 'active'"))

    op.create_table(
        "olt_poll_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("olt_device_id", sa.Integer(), nullable=False),
        sa.Column("correlation_id", sa.String(length=120), nullable=False),
        sa.Column("trigger", sa.String(length=32), nullable=False, server_default="manual"),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="disabled"),
        sa.Column("error_code", sa.String(length=100)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["olt_device_id", "organization_id"], ["olt_devices.id", "olt_devices.organization_id"], ondelete="CASCADE"),
        sa.UniqueConstraint("id", "organization_id", name="uq_olt_poll_runs_id_org"),
        sa.UniqueConstraint("correlation_id", name="uq_olt_poll_runs_correlation_id"),
        sa.CheckConstraint("status IN ('requested', 'running', 'succeeded', 'partial', 'failed', 'disabled')", name="ck_olt_poll_runs_status"),
        sa.CheckConstraint("trigger IN ('manual', 'test_fixture')", name="ck_olt_poll_runs_trigger"),
        sa.CheckConstraint("completed_at IS NULL OR started_at IS NULL OR completed_at >= started_at", name="ck_olt_poll_runs_timestamp_order"),
    )
    op.create_index("ix_olt_poll_runs_organization_id", "olt_poll_runs", ["organization_id"])
    op.create_index("ix_olt_poll_runs_olt_device_id", "olt_poll_runs", ["olt_device_id"])
    op.create_index("ix_olt_poll_runs_org_created", "olt_poll_runs", ["organization_id", "created_at"])


def downgrade() -> None:
    op.drop_table("olt_poll_runs")
    op.drop_table("olt_service_associations")
    op.drop_table("olt_onus")
    op.drop_table("olt_pon_ports")
    op.drop_table("olt_uplinks")
    op.drop_table("olt_cards")
    op.drop_table("olt_devices")
    op.drop_table("olt_credential_references")
