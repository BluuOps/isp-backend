"""Add bounded staging UAT fixture lifecycle metadata."""

from alembic import op
import sqlalchemy as sa


revision = "0016_staging_uat_fixtures"
down_revision = "0015_expiry_reject_ownership"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "organization_staff",
        sa.Column("is_uat_fixture", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("organization_staff", sa.Column("uat_fixture_id", sa.String(length=64), nullable=True))
    op.add_column("organization_staff", sa.Column("uat_expires_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("organization_staff", sa.Column("uat_revoked_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_organization_staff_is_uat_fixture", "organization_staff", ["is_uat_fixture"])
    op.create_index(
        "ix_organization_staff_uat_expires_at", "organization_staff", ["uat_expires_at"]
    )
    op.create_index(
        "ix_organization_staff_uat_fixture_id",
        "organization_staff",
        ["uat_fixture_id"],
        unique=True,
    )
    op.create_check_constraint(
        "ck_organization_staff_uat_read_only_expiring",
        "organization_staff",
        "NOT is_uat_fixture OR "
        "(role = 'Read Only' AND uat_fixture_id IS NOT NULL AND uat_expires_at IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_organization_staff_uat_read_only_expiring",
        "organization_staff",
        type_="check",
    )
    op.drop_index("ix_organization_staff_uat_fixture_id", table_name="organization_staff")
    op.drop_index("ix_organization_staff_uat_expires_at", table_name="organization_staff")
    op.drop_index("ix_organization_staff_is_uat_fixture", table_name="organization_staff")
    op.drop_column("organization_staff", "uat_revoked_at")
    op.drop_column("organization_staff", "uat_expires_at")
    op.drop_column("organization_staff", "uat_fixture_id")
    op.drop_column("organization_staff", "is_uat_fixture")
