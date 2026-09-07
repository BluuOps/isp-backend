"""Add explicit ownership for RadiusFiber-managed RADIUS rejects."""

from alembic import op
import sqlalchemy as sa


revision = "0015_expiry_reject_ownership"
down_revision = "0014_olt_inventory_foundation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "radius_reject_ownerships",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("radcheck_id", sa.Integer(), nullable=False),
        sa.Column("username", sa.String(length=100), nullable=False),
        sa.Column(
            "owner",
            sa.String(length=64),
            nullable=False,
            server_default="radiusfiber_access_policy",
        ),
        sa.Column("reason_code", sa.String(length=50), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "owner = 'radiusfiber_access_policy'",
            name="ck_radius_reject_ownership_owner",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "organization_id"],
            ["users.id", "users.organization_id"],
            name="fk_radius_reject_ownership_user_tenant",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["radcheck_id"], ["radcheck.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "organization_id",
            "user_id",
            "reason_code",
            name="uq_radius_reject_ownership_user_tenant_reason",
        ),
        sa.UniqueConstraint("radcheck_id", name="uq_radius_reject_ownership_radcheck"),
    )
    op.create_index(
        "ix_radius_reject_ownerships_organization_id",
        "radius_reject_ownerships",
        ["organization_id"],
    )
    op.create_index(
        "ix_radius_reject_ownerships_user_id",
        "radius_reject_ownerships",
        ["user_id"],
    )


def downgrade() -> None:
    # Reject rows remain fail-closed; only RadiusFiber's ownership metadata is removed.
    op.drop_index("ix_radius_reject_ownerships_user_id", table_name="radius_reject_ownerships")
    op.drop_index("ix_radius_reject_ownerships_organization_id", table_name="radius_reject_ownerships")
    op.drop_table("radius_reject_ownerships")
