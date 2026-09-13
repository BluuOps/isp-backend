"""Add tenant-scoped application NAS metadata."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0009_network_access_servers"
down_revision = "0008_webhook_inbox"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "network_access_servers",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("zone_id", sa.Integer(), nullable=False),
        sa.Column("nas_ip_address", postgresql.INET(), nullable=False),
        sa.Column("display_name", sa.String(length=150), nullable=False),
        sa.Column("short_name", sa.String(length=100), nullable=True),
        sa.Column("device_type", sa.String(length=100), nullable=True),
        sa.Column("status", sa.String(length=50), nullable=False, server_default="active"),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('active','disabled')", name="ck_network_access_servers_status"),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["zone_id"], ["zones.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("organization_id", "nas_ip_address", name="uq_network_access_servers_org_ip"),
    )
    op.create_index("ix_network_access_servers_organization_id", "network_access_servers", ["organization_id"])
    op.create_index("ix_network_access_servers_zone_id", "network_access_servers", ["zone_id"])
    op.create_index("ix_network_access_servers_nas_ip_address", "network_access_servers", ["nas_ip_address"])


def downgrade() -> None:
    op.drop_index("ix_network_access_servers_nas_ip_address", table_name="network_access_servers")
    op.drop_index("ix_network_access_servers_zone_id", table_name="network_access_servers")
    op.drop_index("ix_network_access_servers_organization_id", table_name="network_access_servers")
    op.drop_table("network_access_servers")
