"""Add PostgreSQL-backed access-token revocations."""

from alembic import op
import sqlalchemy as sa


revision = "0013_auth_token_revocations"
down_revision = "0012_expiry_enforcement"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "auth_token_revocations",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("jti_hash", sa.String(length=64), nullable=False),
        sa.Column("principal_type", sa.String(length=32), nullable=False),
        sa.Column("subject_id", sa.String(length=255), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason", sa.String(length=64), nullable=False, server_default="logout"),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_auth_token_revocations_jti_hash", "auth_token_revocations", ["jti_hash"], unique=True)
    op.create_index("ix_auth_token_revocations_principal_type", "auth_token_revocations", ["principal_type"])
    op.create_index("ix_auth_token_revocations_subject_id", "auth_token_revocations", ["subject_id"])
    op.create_index("ix_auth_token_revocations_organization_id", "auth_token_revocations", ["organization_id"])
    op.create_index(
        "ix_auth_token_revocations_cleanup",
        "auth_token_revocations",
        ["expires_at", "id"],
    )


def downgrade() -> None:
    op.drop_index("ix_auth_token_revocations_cleanup", table_name="auth_token_revocations")
    op.drop_index("ix_auth_token_revocations_organization_id", table_name="auth_token_revocations")
    op.drop_index("ix_auth_token_revocations_subject_id", table_name="auth_token_revocations")
    op.drop_index("ix_auth_token_revocations_principal_type", table_name="auth_token_revocations")
    op.drop_index("ix_auth_token_revocations_jti_hash", table_name="auth_token_revocations")
    op.drop_table("auth_token_revocations")
